"""Состояние сессии и разбор события хода `pre_llm_call`.

Основание: ТЗ v1.7 §26.2 (состав полей `pre_llm_call`), §17 (состояние
сессии), §11 (детекция сжатия контекста Hermes, П-04), §18 (жизненный цикл
хуков), §19.5 и §22 (логирование).

Модуль не читает журнал и не вызывает модель: он разбирает событие хода,
измеряет историю без блоков памяти MiniMem, определяет сжатие контекста и
ведёт счётчики сессии (точки отсчёта истории, кулдаун, число вставок
Возврата). Счётчики лежат в таблице `meta` под ключом
`session_state:<project>/<session_id>`: схема `sessions` из §9.1 не меняется,
а прецедент служебных ключей в `meta` уже есть (`damaged:`, П-13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from . import insert
from .log import Logger
from .store import Store

#: Префикс ключа состояния сессии в таблице `meta` (§17).
STATE_KEY_PREFIX = "session_state:"

#: Значения `compaction_decision` в логе (§11, §19.5).
DECISION_NONE = "none"
DECISION_COMPACTED = "compacted"
DECISION_COOLDOWN = "cooldown"
DECISION_LIMIT = "limit"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "да"}
    return False


@dataclass
class TurnEvent:
    """Данные хода из события pre_llm_call (§26.2)."""

    session_id: str = ""
    task_id: str = ""
    turn_id: str = ""
    user_message: str = ""
    conversation_history: list[Any] = field(default_factory=list)
    is_first_turn: bool = False
    model: str = ""
    platform: str = ""
    parent_session_id: str = ""
    sender_id: str = ""
    cwd: str | None = None

    @property
    def has_session_id(self) -> bool:
        return bool(self.session_id and self.session_id.strip())

    @property
    def turn_numeric(self) -> int | None:
        """Числовой номер хода, если Hermes передал такой (П-09)."""

        raw = self.turn_id.strip()
        return int(raw) if raw.isdigit() else None


def turn_from_event(event: Mapping[str, Any]) -> TurnEvent:
    """Разбирает полезную нагрузку `pre_llm_call` (§26.2).

    `is_first_turn` берётся готовым флагом Hermes, а не вычисляется
    (§3.2, §11): отсутствие поля означает «не первый ход».
    """

    extra = event.get("extra")
    if not isinstance(extra, Mapping):
        extra = {}
    history = extra.get("conversation_history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        history = []
    turn_id = extra.get("turn_id")
    return TurnEvent(
        session_id=str(event.get("session_id") or ""),
        task_id=str(extra.get("task_id") or ""),
        turn_id="" if turn_id is None else str(turn_id),
        user_message=str(extra.get("user_message") or ""),
        conversation_history=list(history),
        is_first_turn=_truthy(extra.get("is_first_turn")),
        model=str(extra.get("model") or ""),
        platform=str(extra.get("platform") or event.get("platform") or ""),
        parent_session_id=str(extra.get("parent_session_id") or ""),
        sender_id=str(extra.get("sender_id") or event.get("sender_id") or ""),
        cwd=event.get("cwd"),
    )


def _message_text(item: Any) -> str:
    """Текст одного сообщения истории; неизвестная структура даёт ""."""

    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        for key in ("content", "text", "message"):
            value = item.get(key)
            if isinstance(value, str):
                return value
    return ""


def history_without_minimem(history: Sequence[Any]) -> list[str]:
    """Тексты сообщений истории без блоков памяти MiniMem (§11, §17, MM-109).

    Из каждого сообщения удаляется ровно один блок памяти по делимитерам §13.
    Сообщение, опустевшее после удаления, в измерение не входит: иначе длина
    истории зависела бы от того, сколько раз память была возвращена.
    """

    texts: list[str] = []
    for item in history:
        text = _message_text(item)
        remaining = insert.strip_memory_block(text).text.strip()
        if remaining:
            texts.append(remaining)
    return texts


@dataclass
class HistoryMetrics:
    """Метрики истории текущего хода (§11)."""

    hist_msgs: int = 0
    hist_chars: int = 0

    @property
    def hist_len(self) -> int:
        """`max_history_len` из §17: длина истории без блоков MiniMem."""

        return self.hist_chars


def measure_history(history: Sequence[Any]) -> HistoryMetrics:
    """Считает `hist_msgs` и `hist_chars` по истории без блоков MiniMem."""

    texts = history_without_minimem(history)
    return HistoryMetrics(hist_msgs=len(texts), hist_chars=sum(len(text) for text in texts))


@dataclass
class CompactionDecision:
    """Решение о сжатии контекста с метриками для лога (§11, §19.5)."""

    detected: bool = False
    hist_msgs: int = 0
    hist_chars: int = 0
    max_msgs: int = 0
    max_chars: int = 0
    threshold_applied: str = ""
    in_cooldown: bool = False
    limit_reached: bool = False

    @property
    def decision(self) -> str:
        if self.limit_reached:
            return DECISION_LIMIT
        if self.in_cooldown:
            return DECISION_COOLDOWN
        return DECISION_COMPACTED if self.detected else DECISION_NONE


def detect_compaction(
    config: Mapping[str, Any],
    metrics: HistoryMetrics,
    max_msgs: int,
    max_chars: int,
) -> CompactionDecision:
    """Определяет сжатие контекста по метрикам истории (§11, П-04).

    Условия (любое из двух): заметное падение числа сообщений при
    `max_msgs - hist_msgs >= compaction_min_drop` либо падение суммарной
    длины при `max_chars - hist_chars >= compaction_min_chars_drop`.
    """

    ratio = float(config["compaction_shrink_ratio"])
    min_drop = int(config["compaction_min_drop"])
    min_chars_drop = int(config["compaction_min_chars_drop"])

    applied: list[str] = []
    if (
        max_msgs > 0
        and metrics.hist_msgs < max_msgs * ratio
        and max_msgs - metrics.hist_msgs >= min_drop
    ):
        applied.append(f"hist_msgs<{max_msgs}*{ratio} drop>={min_drop}")
    if (
        max_chars > 0
        and metrics.hist_chars < max_chars * ratio
        and max_chars - metrics.hist_chars >= min_chars_drop
    ):
        applied.append(f"hist_chars<{max_chars}*{ratio} drop>={min_chars_drop}")

    return CompactionDecision(
        detected=bool(applied),
        hist_msgs=metrics.hist_msgs,
        hist_chars=metrics.hist_chars,
        max_msgs=max_msgs,
        max_chars=max_chars,
        threshold_applied="; ".join(applied),
    )


def state_key(project: str, session_id: str) -> str:
    """Ключ состояния сессии в таблице `meta` (§17)."""

    return f"{STATE_KEY_PREFIX}{project}/{session_id}"


@dataclass
class SessionState:
    """Состояние сессии, нужное Возврату (§11, §17).

    `turns_seen` считает ходы, на которых работал хук: от него отсчитывается
    кулдаун после сжатия. `return_injections` ограничивает число вставок
    Возврата за сессию. `max_msgs` и `max_chars` — точки отсчёта истории.
    """

    turns_seen: int = 0
    max_msgs: int = 0
    max_chars: int = 0
    last_compaction_turn: int = 0
    return_injections: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "turns_seen": self.turns_seen,
                "max_msgs": self.max_msgs,
                "max_chars": self.max_chars,
                "last_compaction_turn": self.last_compaction_turn,
                "return_injections": self.return_injections,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "SessionState":
        """Разбирает сохранённое состояние; повреждённые данные не мешают (§19.2)."""

        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return cls()
        if not isinstance(data, Mapping):
            return cls()
        state = cls()
        for name in (
            "turns_seen",
            "max_msgs",
            "max_chars",
            "last_compaction_turn",
            "return_injections",
        ):
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(state, name, value)
        return state


def load_state(store: Store, project: str, session_id: str) -> SessionState:
    """Читает состояние сессии из служебного слоя (§17)."""

    try:
        raw = store.get_meta(state_key(project, session_id))
    except Exception:  # noqa: BLE001 - повреждённое состояние не мешает Hermes
        return SessionState()
    return SessionState.from_json(raw)


def save_state(
    store: Store, project: str, session_id: str, state: SessionState, logger: Logger
) -> None:
    """Сохраняет состояние сессии; отказ слоя не роняет хук (§19.2)."""

    try:
        # Транзакция обязательна: `Store.__exit__` закрывает соединение без
        # commit, и состояние сессии иначе не сохранилось бы (§19.3).
        with store.connection:
            store.set_meta(state_key(project, session_id), state.to_json())
    except Exception as exc:  # noqa: BLE001 - граница модуля
        logger.error(
            "session",
            "session_state_write_failed",
            type(exc).__name__,
            session_id=session_id,
            project=project,
        )


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def touch_session(store: Store, project: str, turn: TurnEvent) -> None:
    """Создаёт или обновляет строку `sessions` текущего хода (§9.1, §17)."""

    fields: dict[str, Any] = {"last_seen_at": _now_iso()}
    if turn.turn_id:
        fields["last_known_turn"] = turn.turn_id
    _write_session(store, project, turn.session_id, fields)


def _write_session(
    store: Store, project: str, session_id: str, fields: Mapping[str, Any]
) -> None:
    """Пишет поля строки `sessions`; отказ служебного слоя не мешает Hermes."""

    try:
        with store.connection:
            store.session_upsert(session_id, project, **dict(fields))
    except Exception:  # noqa: BLE001 - состояние не критично для вставки
        return


def save_metrics(
    store: Store, project: str, session_id: str, state: SessionState
) -> None:
    """Пишет точки отсчёта истории в `sessions` (§17, §9.1).

    `max_history_len` нормативно измеряется по истории без блоков памяти
    MiniMem, поэтому сюда попадает `max_chars` — длина в символах, а не
    `max_msgs`.
    """

    _write_session(
        store,
        project,
        session_id,
        {
            "max_history_len": state.max_chars,
            "max_msgs": state.max_msgs,
            "max_chars": state.max_chars,
        },
    )


def injection_limit_reached(config: Mapping[str, Any], state: SessionState) -> bool:
    """Исчерпан ли лимит вставок Возврата за сессию (§11, MM-124)."""

    return state.return_injections >= int(config["max_return_injections_per_session"])


def register_turn(
    store: Store,
    config: Mapping[str, Any],
    logger: Logger,
    project: str,
    turn: TurnEvent,
    deadline_remaining_ms: int | None = None,
) -> tuple[SessionState, CompactionDecision]:
    """Учитывает ход: обновляет состояние, точки отсчёта и кулдаун (§11).

    Порядок: счётчик ходов увеличивается всегда; сжатие определяется по
    точкам отсчёта предыдущих ходов, если не действует кулдаун. После
    сжатия точки отсчёта берутся заново — иначе уменьшение истории
    удовлетворяло бы условию на следующих ходах.
    """

    state = load_state(store, project, turn.session_id)
    state.turns_seen += 1
    metrics = measure_history(turn.conversation_history)

    decision = detect_compaction(config, metrics, state.max_msgs, state.max_chars)
    cooldown = int(config["compaction_cooldown_turns"])
    if state.last_compaction_turn and state.turns_seen - state.last_compaction_turn <= cooldown:
        decision.detected = False
        decision.in_cooldown = True
        decision.threshold_applied = ""
    if injection_limit_reached(config, state):
        decision.detected = False
        decision.limit_reached = True

    if decision.detected:
        state.max_msgs = metrics.hist_msgs
        state.max_chars = metrics.hist_chars
        state.last_compaction_turn = state.turns_seen
    else:
        state.max_msgs = max(state.max_msgs, metrics.hist_msgs)
        state.max_chars = max(state.max_chars, metrics.hist_chars)

    save_state(store, project, turn.session_id, state, logger)
    save_metrics(store, project, turn.session_id, state)
    log_decision(logger, turn, project, decision, deadline_remaining_ms)
    return state, decision


def log_decision(
    logger: Logger,
    turn: TurnEvent,
    project: str,
    decision: CompactionDecision,
    deadline_remaining_ms: int | None = None,
) -> None:
    """Пишет решение о сжатии с обязательными полями (§11, §19.5, MM-125)."""

    fields: dict[str, Any] = {
        "session_id": turn.session_id,
        "project": project,
        "hook_event": "pre_llm_call",
        "hist_msgs": decision.hist_msgs,
        "hist_chars": decision.hist_chars,
        "compaction_decision": decision.decision,
        "threshold_applied": decision.threshold_applied or "none",
    }
    if deadline_remaining_ms is not None:
        fields["deadline_remaining_ms"] = deadline_remaining_ms
    logger.log("session", "compaction_detected", **fields)


def note_return_injection(
    store: Store, config: Mapping[str, Any], logger: Logger, project: str, turn: TurnEvent
) -> SessionState:
    """Учитывает выполненную вставку Возврата (§11).

    Проверку лимита и лог `return_injection_limit` выполняет сам Возврат:
    здесь состояние только обновляется.
    """

    state = load_state(store, project, turn.session_id)
    state.return_injections += 1
    save_state(store, project, turn.session_id, state, logger)
    return state
