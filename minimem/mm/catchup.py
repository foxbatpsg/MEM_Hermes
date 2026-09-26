"""Catch-up: подготовка предыдущей сессии на первом ходе новой.

Основание: ТЗ v1.7 §18 (жизненный цикл хуков), §18.1 (catch-up), §17
(состояние сессии), §19.1.1 (суббюджеты, П-06), §19.2 (изоляция ошибок),
§19.5 и §22 (логирование), §26.2 (формат обмена, П-17), §3.5 и §3.7.7
(уплотнение), §11.1 (выбор предыдущей сессии, П-07), §14 (дайджест).

Модуль не читает журнал: данные берутся из производного слоя SQLite и из
файлов дайджестов, как и у Дайджеста и Уплотнения. Catch-up не является
функцией Возврата: он выполняет собственные шаги, имеет собственный бюджет
(40 % от `hook_deadline_ms`, ограниченный `catch_up_budget_seconds`) и не
заимствует `return_min_budget_ms`, который зарезервирован под вставку памяти.

Порядок (§18.1): определить целевую сессию -> проверить доступность состояния
сессий -> проверить бюджет -> уплотнить (если `mode_compaction`) -> построить
дайджест (если `mode_return`) -> снять `pending_catch_up`. Любой отказ
перехватывается здесь, наружу не выходит и не мешает Возврату (§19.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import compaction, digest, ret, session
from .config import catch_up_budget_ms, digest_budget_ms
from .log import Logger
from .store import Store

#: Операции catch-up в логе (§22: перечень открыт для новых операций).
OPERATION_CATCH_UP = "catch_up"
OPERATION_TIMEOUT = "catch_up_timeout"
OPERATION_STATE_UNAVAILABLE = "catch_up_state_unavailable"

#: Статусы результата catch-up.
STATUS_DONE = "done"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"
STATUS_STATE_UNAVAILABLE = "state_unavailable"
STATUS_FAILED = "failed"

#: Причины в `error_detail_code`.
REASON_NOT_FIRST_TURN = "not_first_turn"
REASON_NO_PREVIOUS_SESSION = "no_previous_session"
REASON_NOT_PENDING = "not_pending"
REASON_ATTEMPTS_EXHAUSTED = "catch_up_attempts_exhausted"
REASON_MODES_DISABLED = "modes_disabled"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_SESSION_STATE_LOST = "session_state_lost"
REASON_NO_TARGET = "no_target"
REASON_COMPACTION_FAILED = "compaction_failed"
REASON_DIGEST_UP_TO_DATE = "digest_up_to_date"
REASON_DIGEST_CREATED = "digest_created"
REASON_DIGEST_FAILED = "digest_failed"
REASON_DONE = "done"


@dataclass
class CatchUpResult:
    """Итог одной попытки catch-up на ходе."""

    status: str = STATUS_SKIPPED
    reason: str = ""
    session_id: str = ""
    project: str = ""
    target_session_id: str = ""
    attempts: int = 0
    pending: bool = False
    compaction: compaction.CompactionResult | None = None
    digest: digest.DigestResult | None = None

    @property
    def completed(self) -> bool:
        """Catch-up доведён до конца: `pending_catch_up` можно снять."""

        return self.status == STATUS_DONE


def select_previous_session(
    store: Store,
    memory_root: Path,
    project: str,
    current_session_id: str,
    logger: Logger,
) -> str:
    """Предыдущая сессия проекта для catch-up (§18.1 п.3, П-07).

    Кандидат — та же сессия, которую выбрало бы правило Возврата (§11.1), чтобы
    catch-up построил дайджест для той же сессии, которую затем прочитает
    Возврат. Если валидных дайджестов нет — последняя сессия проекта, отличная
    от текущей, по `last_seen_at DESC`, затем `session_id DESC`.
    """

    candidate = ret.select_last_digest(memory_root, project, current_session_id, logger)
    if candidate is not None and candidate.session_id:
        return candidate.session_id

    rows = [
        row
        for row in store.sessions_all(project)
        if str(row.get("session_id") or "") != current_session_id
    ]
    if not rows:
        return ""
    rows.sort(
        key=lambda row: (str(row.get("last_seen_at") or ""), str(row.get("session_id") or "")),
        reverse=True,
    )
    return str(rows[0].get("session_id") or "")


def session_state_available(store: Store, project: str, current_session_id: str) -> bool:
    """Доступно ли состояние сессий проекта (§18.1 п.4).

    После `rebuild-index` таблица `sessions` пуста, а записи в индексе есть:
    catch-up в этом случае не сканирует журнал, а отказывается работать.
    Проект без записей в индексе — не потерянное состояние, а отсутствие
    работы.
    """

    if not store.meta_sessions(project):
        return True
    return any(
        str(row.get("session_id") or "") != current_session_id
        for row in store.sessions_all(project)
    )


def set_catch_up_state(
    store: Store,
    session_id: str,
    project: str,
    pending: bool,
    target: str = "",
    attempts: int | None = None,
) -> None:
    """Пишет `pending_catch_up`, `catch_up_target_session_id` и
    `catch_up_attempts` текущей сессии (§17, §9.1)."""

    fields: dict[str, Any] = {"pending_catch_up": 1 if pending else 0}
    if target:
        fields["catch_up_target_session_id"] = target
    if attempts is not None:
        fields["catch_up_attempts"] = attempts
    with store.connection:
        store.session_upsert(session_id, project, **fields)


def _completion_for(row: Mapping[str, Any] | None) -> str:
    """Поле «Завершение» дайджеста по `sessions.completed` (§18.1 п.6.1, П-17).

    `completed = 0` означает прерванную сессию; такое состояние не мешает
    созданию дайджеста — правила уплотнения механические и от полноты сессии
    не зависят.
    """

    if row and int(row.get("completed") or 0):
        return digest.COMPLETION_FULL
    return digest.COMPLETION_INTERRUPTED


def _digest_up_to_date(store: Store, memory_root: Path, project: str, session_id: str) -> bool:
    """Есть ли у сессии валидный дайджест со статусом `done` (§18.1, MM-110)."""

    row = store.session_get(session_id, project)
    if not row or str(row.get("digest_status") or "") != digest.STATUS_DONE:
        return False
    return digest.read_header(digest.digest_path(memory_root, project, session_id)) is not None


def _log(
    result: CatchUpResult,
    logger: Logger,
    operation: str,
    deadline_remaining_ms: int | None,
    status: str = "skipped",
    **fields: Any,
) -> None:
    """Запись лога catch-up с обязательными полями (§19.5, §22)."""

    payload: dict[str, Any] = {
        "session_id": result.session_id,
        "project": result.project,
        "hook_event": "pre_llm_call",
        "catch_up_target_session_id": result.target_session_id or None,
        "catch_up_attempts": result.attempts,
    }
    payload.update(fields)
    if result.reason:
        payload["error_detail_code"] = result.reason
    if deadline_remaining_ms is not None:
        payload["deadline_remaining_ms"] = deadline_remaining_ms
    logger.log("catch_up", operation, status=status, **payload)


def _defer(
    store: Store,
    result: CatchUpResult,
    logger: Logger,
    deadline_remaining_ms: int | None,
    reason: str,
    operation: str = OPERATION_TIMEOUT,
) -> CatchUpResult:
    """Откладывает catch-up: `pending_catch_up` сохраняется, attempts растёт.

    Откладывание, а не обрыв по лимиту времени: Возврат выполняется по
    имеющимся данным (§18, §19.1.1).
    """

    result.status = STATUS_TIMEOUT
    result.reason = reason
    result.attempts += 1
    set_catch_up_state(
        store,
        result.session_id,
        result.project,
        pending=True,
        target=result.target_session_id,
        attempts=result.attempts,
    )
    _log(result, logger, operation, deadline_remaining_ms)
    return result


def _hold(
    store: Store,
    result: CatchUpResult,
    logger: Logger,
    deadline_remaining_ms: int | None,
    reason: str,
) -> CatchUpResult:
    """Неудачный шаг: попытка засчитывается, `pending_catch_up` сохраняется."""

    result.status = STATUS_FAILED
    result.reason = reason
    result.attempts += 1
    set_catch_up_state(
        store,
        result.session_id,
        result.project,
        pending=True,
        target=result.target_session_id,
        attempts=result.attempts,
    )
    _log(result, logger, OPERATION_CATCH_UP, deadline_remaining_ms, status="error")
    return result


def _prepare_target(
    store: Store,
    config: Mapping[str, Any],
    memory_root: Path,
    logger: Logger,
    result: CatchUpResult,
    turn: session.TurnEvent,
    deadline_remaining_ms: int | None,
) -> CatchUpResult | None:
    """Определяет целевую сессию и допустимость повтора (§18.1 п.3, «Повтор»).

    Возвращает `None`, если работать можно; иначе — готовый результат.
    """

    if not turn.is_first_turn:
        if not result.pending:
            result.reason = REASON_NOT_PENDING
            return result
        if not result.target_session_id:
            result.reason = REASON_NO_TARGET
            return result
        if result.attempts >= int(config["catch_up_retry_max_turns"]):
            result.reason = REASON_ATTEMPTS_EXHAUSTED
            _log(result, logger, OPERATION_CATCH_UP, deadline_remaining_ms)
            return result
        return None

    target = select_previous_session(store, memory_root, result.project, turn.session_id, logger)
    result.target_session_id = target
    result.attempts = 0
    result.pending = False
    if not target:
        result.reason = REASON_NO_PREVIOUS_SESSION
        set_catch_up_state(store, turn.session_id, result.project, pending=False, attempts=0)
        _log(result, logger, OPERATION_CATCH_UP, deadline_remaining_ms)
        return result
    result.pending = True
    set_catch_up_state(
        store, turn.session_id, result.project, pending=True, target=target, attempts=0
    )
    return None


def run_catch_up(
    store: Store,
    config: Mapping[str, Any],
    memory_root: Path,
    logger: Logger,
    project: str,
    turn: session.TurnEvent,
    deadline_remaining_ms: int | None = None,
) -> CatchUpResult:
    """Выполняет catch-up на ходе (§18.1, §18).

    Первый ход сессии запускает catch-up; последующие ходы — только повтор при
    `pending_catch_up` и `catch_up_attempts < catch_up_retry_max_turns`.
    Компоненты управляются режимами независимо: уплотнение требует
    `mode_compaction`, дайджест — `mode_return`; если оба режима выключены,
    автоматический catch-up не выполняется.
    """

    result = CatchUpResult(session_id=turn.session_id, project=project)
    row = store.session_get(turn.session_id, project) or {}
    result.target_session_id = str(row.get("catch_up_target_session_id") or "")
    result.attempts = int(row.get("catch_up_attempts") or 0)
    result.pending = bool(int(row.get("pending_catch_up") or 0))

    need_compaction = compaction.auto_compaction_enabled(config)
    need_digest = bool(config["mode_return"])
    if not need_compaction and not need_digest:
        result.reason = REASON_MODES_DISABLED
        _log(result, logger, OPERATION_CATCH_UP, deadline_remaining_ms)
        return result

    # Потеря состояния сессий проверяется до выбора цели: после
    # `rebuild-index` кандидата нет вообще, и catch-up не должен выглядеть
    # как «предыдущей сессии нет» (§18.1 п.4).
    if (turn.is_first_turn or result.pending) and not session_state_available(
        store, project, turn.session_id
    ):
        # Журнал не сканируется: полное восстановление — через rebuild-digest.
        result.status = STATUS_STATE_UNAVAILABLE
        result.reason = REASON_SESSION_STATE_LOST
        _log(result, logger, OPERATION_STATE_UNAVAILABLE, deadline_remaining_ms)
        return result

    stopped = _prepare_target(
        store, config, memory_root, logger, result, turn, deadline_remaining_ms
    )
    if stopped is not None:
        return stopped

    if deadline_remaining_ms is not None and deadline_remaining_ms < catch_up_budget_ms(config):
        return _defer(store, result, logger, deadline_remaining_ms, REASON_BUDGET_EXHAUSTED)

    if need_compaction:
        try:
            result.compaction = compaction.run_compaction(
                store, config, logger, session_id=result.target_session_id, project=project
            )
        except Exception:  # noqa: BLE001 - шаг изолирован, дайджест строится (§19.2)
            result.compaction = compaction.CompactionResult(status=compaction.STATUS_FAILED)
        if result.compaction.status == compaction.STATUS_FAILED:
            result.reason = REASON_COMPACTION_FAILED

    if need_digest and not _digest_up_to_date(
        store, memory_root, project, result.target_session_id
    ):
        if deadline_remaining_ms is not None and deadline_remaining_ms < digest_budget_ms(config):
            return _defer(store, result, logger, deadline_remaining_ms, REASON_BUDGET_EXHAUSTED)
        try:
            result.digest = digest.build_digest(
                store,
                config,
                memory_root,
                logger,
                result.target_session_id,
                project,
                completion=_completion_for(store.session_get(result.target_session_id, project)),
                hook_event="pre_llm_call",
                deadline_remaining_ms=deadline_remaining_ms,
            )
        except Exception:  # noqa: BLE001 - отказ шага не выходит наружу (§19.2)
            result.digest = digest.DigestResult(status="failed")
        if not result.digest.created_ok:
            return _hold(store, result, logger, deadline_remaining_ms, REASON_DIGEST_FAILED)
        result.reason = REASON_DIGEST_CREATED
    elif need_digest:
        # Повторный прогон ничего не создаёт: дайджест уже есть и валиден.
        result.reason = REASON_DIGEST_UP_TO_DATE

    if result.reason == REASON_COMPACTION_FAILED:
        # Уплотнение не удалось, но дайджест построен: работа не потеряна,
        # попытка не засчитывается, и повтор разрешён (§18.1).
        return _hold(store, result, logger, deadline_remaining_ms, REASON_COMPACTION_FAILED)

    result.status = STATUS_DONE
    set_catch_up_state(
        store, result.session_id, result.project, pending=False, target=result.target_session_id
    )
    _log(result, logger, OPERATION_CATCH_UP, deadline_remaining_ms, status="ok")
    return result
