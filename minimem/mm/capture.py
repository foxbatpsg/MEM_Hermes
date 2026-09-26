"""Захват: превращение завершённого хода в запись журнала.

Основание: ТЗ v1.7 §3.1 (последовательность захвата), §10 (хук
post_llm_call), §5.1 (идемпотентность и ревизии), §6 (лимиты размера),
§7 (redaction и fail-политика), П-01, П-02, П-08, П-09, П-18.

Порядок операций (§10 п.1-13) соблюдается буквально: очистка реплики от
блока памяти, redaction, канонизация полного содержимого, `content_hash`,
обрезка, `max_memory_record`, сериализация, `event_id`, проверка идемпотентности,
атомарный append, обновление гарда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from . import canonical, insert, journal, paths, redaction
from .log import Logger
from .project import DEFAULT_PROJECT, resolve_project
from .store import Store, StoreUnavailable

#: Префикс сессии при отсутствии session_id (П-09).
UNKNOWN_SESSION_PREFIX = "unknown-"


@dataclass
class TurnData:
    """Данные хода из события post_llm_call (§26.2)."""

    user_message: str = ""
    assistant_response: str = ""
    session_id: str = ""
    task_id: str = ""
    turn_id: str | None = None
    cwd: str | None = None
    sender_id: str = ""
    platform: str = ""

    @property
    def has_turn_id(self) -> bool:
        return bool(self.turn_id and str(self.turn_id).strip())

    @property
    def has_session_id(self) -> bool:
        return bool(self.session_id and self.session_id.strip())

    @property
    def is_empty(self) -> bool:
        return not self.user_message.strip() and not self.assistant_response.strip()


@dataclass
class CaptureResult:
    """Итог захвата одного хода."""

    status: str = "ok"
    operation: str = "capture_record_written"
    event_id: str = ""
    content_hash: str = ""
    journal_file: str = ""
    journal_offset: int = -1
    revision: int = 1
    turn_source: str = "hermes"
    truncated: str = "none"
    body_status: str = "full"
    redaction_count: int = 0
    truncated_fields: list[str] = field(default_factory=list)
    guard_available: bool = True

    @property
    def written(self) -> bool:
        return self.status == "ok" and self.journal_offset >= 0


def _truncate_field(text: str, limit: int) -> tuple[str, bool]:
    """Обрезает поле по лимиту; лимит в символах, как в конфигурации."""

    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def apply_limits(
    user_text: str,
    assistant_text: str,
    metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    render: Callable[[Mapping[str, Any], str, str, str], str] = canonical.render_record,
) -> tuple[str, str, dict[str, Any], list[str], bool]:
    """Обрезка полей и контроль `max_memory_record` (§6).

    Порядок: сначала лимиты полей, затем при необходимости
    `assistant_answer`, затем `user_utterance`; метаданные сохраняются
    полностью, `truncated` обновляется (MM-105).
    """

    user_limit = int(config["max_user_text"])
    assistant_limit = int(config["max_assistant_text"])
    record_limit = int(config["max_memory_record"])

    user_text, user_cut = _truncate_field(user_text, user_limit)
    assistant_text, assistant_cut = _truncate_field(assistant_text, assistant_limit)
    truncated_fields: list[str] = []
    if user_cut:
        truncated_fields.append("user")
    if assistant_cut:
        truncated_fields.append("assistant")

    def flags(user: bool, assistant: bool) -> str:
        if user and assistant:
            return "both"
        if user:
            return "user"
        if assistant:
            return "assistant"
        return "none"

    metadata = dict(metadata)
    metadata["truncated"] = flags(user_cut, assistant_cut)

    def serialized(user: str, assistant: str) -> int:
        return len(render(metadata, user, assistant, "full").encode("utf-8"))

    if serialized(user_text, assistant_text) > record_limit:
        # Сначала урезается ответ, затем реплика; детерминированный поиск
        # максимальной длины, сохраняющей лимит (MM-105).
        low, high = 0, len(assistant_text)
        while low < high:
            middle = (low + high + 1) // 2
            if serialized(user_text, assistant_text[:middle]) <= record_limit:
                low = middle
            else:
                high = middle - 1
        assistant_text = assistant_text[:low]
        if "assistant" not in truncated_fields:
            truncated_fields.append("assistant")
        metadata["truncated"] = flags(user_cut, True)

        if serialized(user_text, assistant_text) > record_limit:
            low, high = 0, len(user_text)
            while low < high:
                middle = (low + high + 1) // 2
                if serialized(user_text[:middle], assistant_text) <= record_limit:
                    low = middle
                else:
                    high = middle - 1
            user_text = user_text[:low]
            if "user" not in truncated_fields:
                truncated_fields.append("user")
            metadata["truncated"] = flags(True, "assistant" in truncated_fields)

    return user_text, assistant_text, metadata, truncated_fields, bool(truncated_fields)


def derive_turn(
    data: TurnData,
    timestamp: str,
    content_hash: str,
) -> tuple[str, str, str]:
    """Определяет `turn`, `turn_source` и компоненту `event_id` (§10, П-09).

    Отсутствие turn_id не приводит к потере хода: в поле `turn` пишется
    литерал `?`, `turn_source = derived`, а производный ключ —
    первые 16 hex-символов sha256(session_id + timestamp + content_hash) —
    используется вместо turn при вычислении event_id.
    """

    if data.has_turn_id:
        turn = str(data.turn_id)
        return turn, "hermes", turn
    key = canonical.derived_turn_key(data.session_id, timestamp, content_hash)
    return canonical.DERIVED_TURN, "derived", key


def _open_store(config: Mapping[str, Any], memory_root: Path) -> Store | None:
    """Открывает служебный слой; при отказе возвращает None (MM-102)."""

    try:
        store = Store(paths.sqlite_path(memory_root, config["sqlite_filename"]))
        store.create_schema()
        return store
    except StoreUnavailable:
        return None


#: Префикс служебного ключа последнего отправителя сессии (П-18).
SENDER_KEY_PREFIX = "sender:"


def note_sender_change(
    store: Store | None,
    logger: Logger,
    project: str,
    session_id: str,
    sender_id: str,
) -> None:
    """Фиксирует смену отправителя в пределах сессии (MM-161, §22).

    Пустой `sender_id` события не порождает: Hermes передаёт его не всегда.
    Без доступного служебного слоя состояние не хранится, и событие не
    пишется — как и остальные best-effort операции захвата (§5.1).
    """

    if store is None or not sender_id:
        return
    key = f"{SENDER_KEY_PREFIX}{project}/{session_id}"
    previous = store.get_meta(key)
    if previous and previous != sender_id:
        logger.log(
            "capture",
            "sender_changed",
            status="ok",
            session_id=session_id,
            project=project,
            hook_event="post_llm_call",
            error_detail_code="sender_changed",
        )
    try:
        with store.connection:
            store.set_meta(key, sender_id)
    except Exception:  # noqa: BLE001 - состояние отправителя не критично
        return


def capture_turn(
    data: TurnData,
    config: Mapping[str, Any],
    logger: Logger,
    memory_root: Path,
    store: Store | None = None,
    moment: datetime | None = None,
) -> CaptureResult:
    """Выполняет захват одного завершённого хода (§3.1, §10).

    Ошибка записи не поднимается наружу: результат отражается в
    `CaptureResult` и в логе, Hermes продолжает работу (§19.2).
    """

    now = moment if moment is not None else datetime.now().astimezone()
    timestamp = now.isoformat(timespec="seconds")
    timezone_name = config["journal_timezone"]
    result = CaptureResult()

    if not config["mode_capture"]:
        result.status = "skipped"
        result.operation = "capture_disabled"
        logger.log("capture", "capture_disabled", status="skipped", mode="capture")
        return result

    if data.is_empty:
        result.status = "skipped"
        result.operation = "capture_invalid_event"
        logger.log(
            "capture",
            "capture_invalid_event",
            status="skipped",
            error_code="empty_turn",
            turns_seen=1,
            turns_captured=0,
            turns_skipped=1,
        )
        return result

    # 3. Очистка реплики от блока памяти MiniMem (П-01).
    strip_result = insert.strip_memory_block(data.user_message)
    clean_user = strip_result.text
    log_fields: dict[str, Any] = {
        "session_id": data.session_id,
        "hook_event": "post_llm_call",
        "turns_seen": 1,
        "turns_captured": 1,
        "turns_skipped": 0,
        "turns_derived": 0,
    }
    if strip_result.blocks:
        log_fields["insert_stripped_count"] = strip_result.blocks
        log_fields["insert_stripped_chars"] = strip_result.chars
        logger.log("capture", "insert_stripped", status="ok", **log_fields)

    # 4. Определение проекта (§8).
    project, project_path = resolve_project(data.cwd, config["project_mapping"])
    if not project_path:
        project = DEFAULT_PROJECT
    session_id = session_key(data, timezone_name, now)
    if not data.has_session_id:
        log_fields["error_detail_code"] = "session_id_derived"

    # 5. Redaction; сбой или таймаут приводит к withheld (П-08).
    body_status = "full"
    try:
        user_redacted, assistant_redacted = redaction.redact_pair(
            clean_user,
            data.assistant_response,
            timeout_ms=int(config["redaction_timeout_ms"]),
        )
        redacted_user = user_redacted.text
        redacted_assistant = assistant_redacted.text
        redaction_count = user_redacted.total + assistant_redacted.total
        redaction_set = user_redacted.redaction_set
    except Exception as exc:  # noqa: BLE001 - fail-политика redaction (П-08)
        body_status = "withheld"
        redacted_user = ""
        redacted_assistant = ""
        redaction_count = 0
        redaction_set = redaction.redaction_set_hash()
        result.status = "error"
        logger.log(
            "capture",
            "record_body_withheld_redaction_failed",
            status="error",
            error_code="redaction_failed",
            error_detail_code=type(exc).__name__,
            redaction_count=0,
            project=project,
            **log_fields,
        )

    # 6-7. Канонизация полного содержимого и content_hash (до обрезки).
    full_hash = canonical.content_hash(redacted_user, redacted_assistant)
    turn, turn_source, event_turn = derive_turn(data, timestamp, full_hash)
    log_fields["turns_derived"] = 1 if turn_source == "derived" else 0
    event = canonical.event_id(project, session_id, data.task_id, event_turn)

    metadata: dict[str, Any] = {
        "timestamp": timestamp,
        "fmt": canonical.RECORD_FMT,
        "project": project,
        "project_path": project_path,
        "session_id": session_id,
        "task_id": data.task_id,
        "turn": turn,
        "turn_source": turn_source,
        "event_id": event,
        "revision": 1,
        "supersedes": "",
        "content_hash": full_hash,
        "redaction_set": redaction_set,
        "body_status": body_status,
        "truncated": "none",
        "sender_id": data.sender_id,
        "platform": data.platform,
    }

    # 8-9. Обрезка полей и контроль max_memory_record.
    user_stored, assistant_stored, metadata, truncated_fields, was_truncated = apply_limits(
        redacted_user, redacted_assistant, metadata, config
    )
    result.truncated = metadata["truncated"]
    result.truncated_fields = truncated_fields
    result.redaction_count = redaction_count
    result.body_status = body_status
    result.turn_source = turn_source
    result.content_hash = full_hash
    if was_truncated:
        logger.log(
            "capture",
            "record_truncated_to_limit",
            status="ok",
            truncation_fields=",".join(truncated_fields),
            project=project,
            **log_fields,
        )
    if turn_source == "derived":
        logger.log("capture", "capture_turn_derived", status="ok", project=project, **log_fields)

    journal_file = journal_file_for(memory_root, project, now, timezone_name)

    # 11. Проверка идемпотентности и конфликта по event_id (§5.1).
    owned_store = store is None
    if store is None:
        store = _open_store(config, memory_root)
        result.guard_available = store is not None
    if store is None:
        logger.log(
            "capture",
            "capture_idempotency_guard_unavailable",
            status="error",
            error_code="store_unavailable",
            project=project,
            event_id=event,
            **log_fields,
        )
    else:
        note_sender_change(store, logger, project, session_id, data.sender_id)
        existing = store.guard_get(event)
        if existing is not None:
            if existing["content_hash"] == full_hash:
                result.status = "duplicate"
                result.operation = "capture_duplicate_ignored"
                logger.log(
                    "capture",
                    "capture_duplicate_ignored",
                    status="duplicate",
                    project=project,
                    event_id=event,
                    **log_fields,
                )
                if owned_store:
                    store.close()
                return result
            metadata["revision"] = _next_revision(journal_file, event)
            metadata["supersedes"] = event
            logger.log(
                "capture",
                "capture_event_conflict",
                status="conflict",
                project=project,
                event_id=event,
                error_detail_code="content_hash_changed",
                **log_fields,
            )
            logger.log(
                "capture",
                "capture_event_revision_added",
                status="ok",
                project=project,
                event_id=event,
                **log_fields,
            )

    # 12. Атомарный append записи под блокировкой (§4.5).
    metadata["revision"] = int(metadata["revision"])
    record_text = canonical.render_record(metadata, user_stored, assistant_stored, body_status)
    try:
        offset = journal.append_record(
            journal_file, record_text, fsync=bool(config["journal_fsync"])
        )
    except journal.JournalLockTimeout:
        result.status = "error"
        result.operation = "journal_lock_timeout"
        logger.error("journal", "journal_lock_timeout", "journal_lock_timeout", **log_fields)
        if owned_store and store is not None:
            store.close()
        return result
    except OSError as exc:
        result.status = "error"
        result.operation = "journal_append_failed"
        logger.error(
            "journal",
            "journal_append_failed",
            type(exc).__name__,
            project=project,
            event_id=event,
            **log_fields,
        )
        if owned_store and store is not None:
            store.close()
        return result

    result.journal_file = journal_file.as_posix()
    result.journal_offset = offset
    result.event_id = event
    result.revision = int(metadata["revision"])

    # 13. Обновление гарда при доступном SQLite.
    if store is not None:
        try:
            store.guard_put(
                event,
                full_hash,
                journal_file.as_posix(),
                offset,
                timestamp,
            )
            store.set_meta_many_if_changed(
                {
                    "config_hash": getattr(config, "hash", "") or config.get("config_hash", ""),
                    "minimem_version": config["minimem_version"],
                    "project_mapping_hash": getattr(config, "mapping_hash", "")
                    or config.get("project_mapping_hash", ""),
                }
            )
        except Exception as exc:  # noqa: BLE001 - гард работает в режиме best-effort
            logger.error(
                "capture",
                "capture_idempotency_guard_unavailable",
                type(exc).__name__,
                project=project,
                event_id=event,
                **log_fields,
            )
        if owned_store:
            store.close()

    logger.log(
        "capture",
        result.operation,
        status="ok",
        project=project,
        event_id=event,
        redaction_count=result.redaction_count,
        truncation_fields=",".join(result.truncated_fields) or None,
        journal_offset=offset,
        **log_fields,
    )
    return result


def journal_file_for(
    memory_root: Path,
    project: str,
    moment: datetime,
    timezone_name: str,
) -> Path:
    """Дневной файл журнала для проекта и момента времени (§4.1)."""

    day = paths.local_date(timezone_name, moment)
    return paths.journal_file(memory_root, project, day)


def _next_revision(journal_file: Path, event_id: str) -> int:
    """Номер следующей ревизии по уже записанным в журнал записям (§5.1)."""

    if not journal_file.is_file():
        return 2
    try:
        records, _ = journal.read_records(journal_file)
    except (OSError, UnicodeDecodeError):
        return 2
    revisions = [record.revision for record in records if record.event_id == event_id]
    return max(revisions, default=1) + 1


def session_key(data: TurnData, journal_timezone: str, moment: datetime) -> str:
    """session_id хода; при отсутствии — `unknown-<дата>` (П-09)."""

    if data.has_session_id:
        return data.session_id
    return UNKNOWN_SESSION_PREFIX + paths.local_date(journal_timezone, moment).isoformat()

