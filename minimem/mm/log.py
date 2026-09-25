"""Структурированный технический лог MiniMem.

Основание: ТЗ v1.7 §22 (состав полей и операции), §19.5 (контракт
измеримости, П-10), §7.1 п.4-6 (в лог не пишутся секреты, фрагменты
секретов и текст исключений с traceback).

Формат строки — JSON без переводов строк внутри значений, одна запись на
строку, UTF-8, LF. Запись в лог никогда не прерывает работу модуля: ошибка
логирования молча возвращается вызывающему коду.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

#: Базовые поля структурированного лога (ТЗ v1.7 §22).
BASE_FIELDS: tuple[str, ...] = (
    "timestamp",
    "module",
    "operation",
    "session_id",
    "project",
    "event_id",
    "status",
    "duration_ms",
    "error_code",
    "mode",
    "hook_event",
    "deadline_remaining_ms",
    "records_scanned",
    "records_indexed",
    "records_suppressed",
    "digest_chars",
    "search_terms",
    "search_hits",
    "search_returned",
    "redaction_count",
    "truncation_fields",
    "error_detail_code",
)

#: Дополнительные поля контракта измеримости (ТЗ v1.7 §19.5, П-10).
EXTRA_FIELDS: tuple[str, ...] = (
    "search_query_mode",
    "returned_event_ids",
    "top5_scores",
    "threshold_applied",
    "hist_msgs",
    "hist_chars",
    "compaction_decision",
    "turns_seen",
    "turns_captured",
    "turns_skipped",
    "turns_derived",
)

#: Поля, которые никогда не попадают в лог: пользовательский текст и секреты.
FORBIDDEN_FIELD_HINTS: tuple[str, ...] = (
    "user_message",
    "user_utterance",
    "assistant_answer",
    "assistant_response",
    "content",
    "text",
    "body",
    "secret",
    "password",
    "token",
    "api_key",
    "traceback",
    "exception",
)


def _is_forbidden(key: str) -> bool:
    lowered = key.casefold()
    return any(hint in lowered for hint in FORBIDDEN_FIELD_HINTS)


def timestamp_utc() -> str:
    """Текущее время UTC в ISO-8601 с секундами."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_record(
    module: str,
    operation: str,
    status: str = "ok",
    **fields: Any,
) -> dict[str, Any]:
    """Собирает запись лога: базовые поля плюс переданные значения.

    Запрещённые поля отбрасываются, значения приводятся к JSON-совместимым
    типам, `None` опускается.
    """

    record: dict[str, Any] = {
        "timestamp": timestamp_utc(),
        "module": module,
        "operation": operation,
        "status": status,
    }
    for key, value in fields.items():
        if _is_forbidden(key):
            continue
        if value is None:
            continue
        record[key] = value
    return record


def format_record(record: Mapping[str, Any]) -> str:
    """Одна строка лога: JSON с гарантированным отсутствием переводов строк."""

    line = json.dumps(record, ensure_ascii=False, default=str, sort_keys=False)
    return line.replace("\r", " ").replace("\n", " ")


class Logger:
    """Запись логов в `<memory_root>/logs/minimem.log` (ТЗ v1.7 §22)."""

    def __init__(self, log_file: Path, enabled: bool = True) -> None:
        self.log_file = Path(log_file)
        self.enabled = enabled
        self.last_error: str | None = None

    def log(
        self,
        module: str,
        operation: str,
        status: str = "ok",
        **fields: Any,
    ) -> dict[str, Any]:
        """Пишет одну запись; при любом отказе возвращает её молча."""

        record = build_record(module, operation, status, **fields)
        if self.enabled:
            self._append(record)
        return record

    def error(
        self,
        module: str,
        operation: str,
        error_code: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Запись об ошибке: только код, без текста исключения (§7.1 п.4)."""

        return self.log(module, operation, status="error", error_code=error_code, **fields)

    def _append(self, record: Mapping[str, Any]) -> None:
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(format_record(record) + "\n")
        except OSError as exc:
            # Отказ логирования не должен останавливать Hermes (§19.2).
            self.last_error = type(exc).__name__


class Timer:
    """Считает `duration_ms` для модулей, у которых есть замеренная операция."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)


class NullLogger(Logger):
    """Лог без записи на диск; используется в тестах и в `--no-log`."""

    def __init__(self) -> None:
        super().__init__(Path(os.devnull), enabled=False)

    def _append(self, record: Mapping[str, Any]) -> None:  # pragma: no cover
        return
