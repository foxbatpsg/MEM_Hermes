"""Возврат: вставка выжимки без поиска и без модели.

Основание: ТЗ v1.7 §3.2, §11 (Возврат, П-04), §11.1 (детерминированный
выбор дайджеста, П-07), §13 и §13.1 (формат вставки и санитизация),
§14 (создание дайджеста при сжатии контекста), §18 (жизненный цикл хуков),
§19.5 и §22 (логирование), §6 (`max_return_chars`).

Возврат читает только файл дайджеста, не обращается к FTS5, не вызывает
модель и не увеличивает счётчик использования. При сжатии контекста файл
текущей сессии при необходимости создаёт модуль Дайджест: сам Возврат
журнал не читает.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import digest, insert, session
from .config import digest_budget_ms
from .log import Logger
from .store import Store

#: Причина выбора файла дайджеста (§11.1).
SOURCE_FIRST_TURN = "first_turn"
SOURCE_COMPACTION = "compaction"

#: Статусы результата Возврата.
STATUS_INSERTED = "inserted"
STATUS_NO_DIGEST = "no_digest"
STATUS_LIMIT = "limit"
STATUS_DISABLED = "disabled"
STATUS_FAILED = "failed"

#: Строка-предупреждение при вставке прерванного дайджеста (§14, П-17).
INTERRUPTED_WARNING = "предыдущая сессия прервана, выжимка неполная"


@dataclass
class DigestCandidate:
    """Кандидат на последний дайджест проекта (§11.1)."""

    path: Path
    header: digest.DigestHeader
    last_turn_utc: str = ""
    fallback_used: bool = False

    @property
    def session_id(self) -> str:
        return self.header.session_id

    @property
    def file(self) -> str:
        return self.path.name

    def beats(self, other: "DigestCandidate") -> bool:
        """Сравнение по §11.1: больше «Последний ход», при равенстве — меньше имя.

        Имя сравнивается на убывание, поэтому при равных метках времени
        выигрывает кандидат с лексикографически меньшим именем файла.
        """

        if self.last_turn_utc != other.last_turn_utc:
            return self.last_turn_utc > other.last_turn_utc
        return self.file < other.file


@dataclass
class ReturnResult:
    """Итог Возврата на одном ходе (§11)."""

    status: str = STATUS_NO_DIGEST
    source: str = ""
    text: str = ""
    digest_file: str = ""
    session_id: str = ""
    chars: int = 0
    truncated: bool = False
    reason: str = ""

    @property
    def inserted(self) -> bool:
        return self.status == STATUS_INSERTED and bool(self.text)


def _mtime_utc(path: Path) -> str:
    """mtime файла как ISO-8601 в UTC: fallback вместо «Последнего хода» (§11.1 п.5)."""

    from datetime import datetime, timezone

    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def read_digest_candidate(path: Path, logger: Logger) -> DigestCandidate | None:
    """Читает заголовок файла дайджеста как кандидата; `None` — повреждён (§11.1 п.6)."""

    header = digest.read_header(path)
    if header is None:
        logger.log(
            "ret",
            "return_digest_damaged",
            status="skipped",
            digest_file=path.name,
            hook_event="pre_llm_call",
            error_detail_code="damaged_digest",
        )
        return None

    last_turn_utc = header.last_turn_utc
    fallback_used = False
    if not last_turn_utc:
        # Поле «Создан» информационное, ключ выбора — «Последний ход» (П-07).
        fallback_used = True
        last_turn_utc = _mtime_utc(path)
        logger.log(
            "ret",
            "digest_created_field_fallback",
            status="ok",
            digest_file=path.name,
            hook_event="pre_llm_call",
            error_detail_code="last_turn_unreadable",
        )
    return DigestCandidate(
        path=path,
        header=header,
        last_turn_utc=last_turn_utc,
        fallback_used=fallback_used,
    )


def select_last_digest(
    memory_root: Path, project: str, current_session_id: str, logger: Logger
) -> DigestCandidate | None:
    """Последний валидный дайджест проекта кроме файла текущей сессии (§11.1).

    Порядок §11.1: кандидаты проекта без текущей сессии -> чтение поля
    «Последний ход» -> максимум `timestamp_utc` -> при равенстве меньшее
    имя файла. Повреждённые файлы пропускаются и не мешают выбору.
    """

    current_file = digest.digest_filename(current_session_id) if current_session_id else ""
    best: DigestCandidate | None = None
    for path in digest.existing_digest_files(memory_root, project):
        if current_file and path.name == current_file:
            continue
        candidate = read_digest_candidate(path, logger)
        if candidate is None:
            continue
        if best is None or candidate.beats(best):
            best = candidate
    return best


def digest_body(path: Path, logger: Logger) -> str:
    """Текст файла дайджеста для вставки; повреждённый файл даёт ""."""

    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.log(
            "ret",
            "return_digest_unreadable",
            status="error",
            digest_file=Path(path).name,
            hook_event="pre_llm_call",
            error_code="digest_read_failed",
        )
        return ""


def _body_with_warning(header: digest.DigestHeader, body: str, logger: Logger) -> str:
    """Добавляет предупреждение о прерванной сессии (§14, П-17)."""

    if header.completion != digest.COMPLETION_INTERRUPTED:
        return body
    logger.log(
        "ret",
        "digest_interrupted_fallback",
        status="ok",
        session_id=header.session_id,
        digest_file=header.path.name if header.path else "",
        hook_event="pre_llm_call",
    )
    return f"{body.rstrip()}\n\n> [!WARNING] {INTERRUPTED_WARNING}"


def build_return_text(
    candidate: DigestCandidate, config: Mapping[str, Any], logger: Logger
) -> tuple[str, bool]:
    """Собирает текст вставки из файла дайджеста в пределах `max_return_chars` (§13)."""

    body = _body_with_warning(candidate.header, digest_body(candidate.path, logger), logger)
    if not body.strip():
        return "", False
    return insert.build_memory_block(body, int(config["max_return_chars"]))


def _digest_for_current_session(
    store: Store,
    config: Mapping[str, Any],
    memory_root: Path,
    logger: Logger,
    session_id: str,
    project: str,
    deadline_remaining_ms: int | None = None,
) -> DigestCandidate | None:
    """Файл дайджеста текущей сессии; при необходимости создаётся Дайджестом (§11).

    Журнал читает Дайджест, а не Возврат: сам модуль Возврата файла не
    создаёт и в состояние сессии не вмешивается.

    Построение дайджеста при сжатии укладывается в суббюджет 30 % от
    `hook_deadline_ms` (§19.1.1, П-06). Если остатка меньше, файл не
    создаётся, причина пишется в лог, а Возврат ничего не вставляет (MM-111).
    """

    path = digest.digest_path(memory_root, project, session_id)
    header = digest.read_header(path)
    if header is None:
        if deadline_remaining_ms is not None and deadline_remaining_ms < digest_budget_ms(config):
            logger.log(
                "ret",
                "return_digest_timeout",
                status="skipped",
                session_id=session_id,
                project=project,
                hook_event="pre_llm_call",
                error_detail_code="digest_budget_exhausted",
                deadline_remaining_ms=deadline_remaining_ms,
            )
            return None
        logger.log(
            "ret",
            "return_digest_rebuild",
            status="ok",
            session_id=session_id,
            project=project,
            hook_event="pre_llm_call",
            error_detail_code="digest_missing_or_damaged",
        )
        result = digest.build_digest(
            store,
            config,
            memory_root,
            logger,
            session_id,
            project,
            hook_event="pre_llm_call",
            deadline_remaining_ms=deadline_remaining_ms,
        )
        if not result.created_ok:
            return None
        path = Path(result.path)
        header = digest.read_header(path)
        if header is None:
            return None
    return DigestCandidate(path=path, header=header, last_turn_utc=header.last_turn_utc)


def perform_return(
    store: Store,
    config: Mapping[str, Any],
    memory_root: Path,
    logger: Logger,
    project: str,
    turn: session.TurnEvent,
    state: session.SessionState,
    decision: session.CompactionDecision | None = None,
    deadline_remaining_ms: int | None = None,
) -> ReturnResult:
    """Выполняет Возврат на ходе (§11, §3.2).

    Порядок: лимит вставок -> источник (первый ход имеет приоритет) ->
    выбор дайджеста -> сборка текста -> лог. Отсутствие дайджеста не
    считается ошибкой: вставляется пустой объект, ход Hermes продолжается.
    """

    detected = bool(decision.detected) if decision is not None else False
    limit_reached = bool(decision.limit_reached) if decision is not None else False
    source = (
        SOURCE_FIRST_TURN
        if turn.is_first_turn
        else SOURCE_COMPACTION
        if detected or limit_reached
        else ""
    )
    result = ReturnResult(source=source, session_id=turn.session_id)
    if not source:
        return result

    if session.injection_limit_reached(config, state):
        # Исчерпание лимита — нормативное событие §11, а не пустой результат.
        logger.log(
            "ret",
            "return_injection_limit",
            status="skipped",
            session_id=turn.session_id,
            project=project,
            hook_event="pre_llm_call",
        )
        result.status = STATUS_LIMIT
        result.reason = "return_injection_limit"
        _log(result, logger, project, status="skipped", deadline_remaining_ms=deadline_remaining_ms)
        return result

    if turn.is_first_turn:
        candidate = select_last_digest(memory_root, project, turn.session_id, logger)
    else:
        candidate = _digest_for_current_session(
            store,
            config,
            memory_root,
            logger,
            turn.session_id,
            project,
            deadline_remaining_ms=deadline_remaining_ms,
        )

    if candidate is None:
        result.status = STATUS_NO_DIGEST
        result.reason = "digest_not_found"
        _log(result, logger, project, status="skipped", deadline_remaining_ms=deadline_remaining_ms)
        return result

    text, truncated = build_return_text(candidate, config, logger)
    if not text:
        result.status = STATUS_NO_DIGEST
        result.reason = "empty_digest"
        _log(result, logger, project, status="skipped", deadline_remaining_ms=deadline_remaining_ms)
        return result

    result.status = STATUS_INSERTED
    result.text = text
    result.chars = len(text)
    result.truncated = truncated
    result.digest_file = candidate.file
    result.session_id = candidate.session_id
    _log(result, logger, project, deadline_remaining_ms=deadline_remaining_ms)
    session.note_return_injection(store, config, logger, project, turn)
    return result


def _log(
    result: ReturnResult,
    logger: Logger,
    project: str,
    status: str = "ok",
    deadline_remaining_ms: int | None = None,
) -> None:
    """Запись лога Возврата с указанием причины (§19.5, §22)."""

    fields: dict[str, Any] = {
        "session_id": result.session_id,
        "project": project,
        "hook_event": "pre_llm_call",
        "digest_chars": result.chars,
        "truncation_fields": "return_chars" if result.truncated else None,
    }
    if result.digest_file:
        fields["digest_file"] = result.digest_file
    if result.reason:
        fields["error_detail_code"] = result.reason
    if deadline_remaining_ms is not None:
        fields["deadline_remaining_ms"] = deadline_remaining_ms
    logger.log("ret", "return_injected", status=status, **fields)
