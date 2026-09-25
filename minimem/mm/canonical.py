"""Канонизация, идентификаторы хода и сериализация записи журнала.

Основание: ТЗ v1.7 §4.4 (формат записи), §5 (content_hash, event_id),
§5.1 (ревизии), §6 (ограничения размеров), §10 (П-01, П-09).
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping

#: Версия формата записи (§4.4 п.3).
RECORD_FMT = 1

#: Маркеры записи (§4.4 п.1-2).
BEGIN_MARKER = "<!-- mm:begin -->"
END_MARKER = "<!-- mm:end -->"

#: Секции тела (§4.4 п.7).
USER_SECTION = "### User"
ASSISTANT_SECTION = "### Assistant"

#: Порядок метаданных фиксирован (§4.4 п.3).
METADATA_ORDER: tuple[str, ...] = (
    "fmt",
    "project",
    "project_path",
    "session_id",
    "task_id",
    "turn",
    "turn_source",
    "event_id",
    "revision",
    "supersedes",
    "content_hash",
    "redaction_set",
    "body_status",
    "truncated",
    "sender_id",
    "platform",
)

#: Закрытые перечисления (§4.4 п.3).
TRUNCATED_VALUES = ("none", "user", "assistant", "both")
BODY_STATUS_VALUES = ("full", "withheld")
TURN_SOURCE_VALUES = ("hermes", "derived")

#: Литерал `turn` при производном ключе (П-09).
DERIVED_TURN = "?"

CONTENT_SEPARATOR = "\n---\n"


class MetadataError(ValueError):
    """Недопустимое значение закрытого перечисления метаданных (§4.4 п.3)."""


def normalize_text(text: str) -> str:
    """Unicode NFC, переводы строк в \\n, без концевых пробелов (§5)."""

    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines)


def canonical_content(user_utterance: str, assistant_answer: str) -> str:
    """Каноническое полное содержимое после redaction и до обрезки (§5)."""

    return (
        normalize_text(user_utterance) + CONTENT_SEPARATOR + normalize_text(assistant_answer)
    )


def _strip_outer_blank_lines(text: str) -> str:
    lines = text.split("\n")
    start = 0
    end = len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return "\n".join(lines[start:end])


def content_hash(user_utterance: str, assistant_answer: str) -> str:
    """`c_` + 32 hex-символа sha256 полного отредактированного содержимого (§5).

    Хеш всегда по полному содержимому до обрезки: обрезанный текст в хеш
    не попадает (MM-63, MM-64).
    """

    canonical = _strip_outer_blank_lines(canonical_content(user_utterance, assistant_answer))
    return "c_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def event_id(project: str, session_id: str, task_id: str, turn: str) -> str:
    """`e_` + 32 hex-символа sha256(project, session_id, task_id, turn) (§5).

    Идентификатор не зависит от содержимого хода: повторная доставка того же
    логического хода даёт тот же event_id (MM-101).
    """

    payload = "\n".join((project, session_id, task_id, str(turn)))
    return "e_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def derived_turn_key(session_id: str, timestamp: str, content: str) -> str:
    """Производный ключ хода при отсутствии turn_id (П-09, §10)."""

    payload = f"{session_id}\n{timestamp}\n{content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def timestamp_utc(moment: datetime) -> str:
    """Нормализованное время UTC для служебного слоя (§4.1)."""

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def escape_body_line(line: str) -> str:
    """Экранирование строки тела при записи (§4.4 п.8)."""

    if line.startswith("<") or line.startswith("\\"):
        return "\\" + line
    if line in (USER_SECTION, ASSISTANT_SECTION):
        return "\\" + line
    return line


def unescape_body_line(line: str) -> str:
    """Снятие одного начального `\\` при чтении (§4.4 п.9)."""

    if line.startswith("\\"):
        return line[1:]
    return line


def _serialize_body(user_text: str, assistant_text: str) -> str:
    user_lines = [escape_body_line(line) for line in normalize_text(user_text).split("\n")]
    assistant_lines = [
        escape_body_line(line) for line in normalize_text(assistant_text).split("\n")
    ]
    return "\n".join([USER_SECTION, *user_lines, ASSISTANT_SECTION, *assistant_lines])


def render_record(
    metadata: Mapping[str, Any],
    user_text: str,
    assistant_text: str,
    body_status: str = "full",
) -> str:
    """Сериализует запись журнала в виде одной строки-блока (§4.4).

    Между записями ставится одна пустая строка; переводы строк — LF.
    `body_status = withheld` оставляет тело пустым (П-08).
    """

    if body_status not in BODY_STATUS_VALUES:
        raise MetadataError(f"Недопустимый body_status: {body_status!r}")
    truncated = metadata.get("truncated", "none")
    if truncated not in TRUNCATED_VALUES:
        raise MetadataError(f"Недопустимый truncated: {truncated!r}")
    turn_source = metadata.get("turn_source", "hermes")
    if turn_source not in TURN_SOURCE_VALUES:
        raise MetadataError(f"Недопустимый turn_source: {turn_source!r}")

    values = dict(metadata)
    values.setdefault("fmt", RECORD_FMT)
    values.setdefault("body_status", body_status)
    values.setdefault("truncated", truncated)

    lines = [BEGIN_MARKER]
    timestamp = values.pop("timestamp", "")
    lines.append(f"## {timestamp} · turn {values.get('turn', '')}")
    for key in METADATA_ORDER:
        if key == "fmt" and values.get("fmt") is None:
            continue
        lines.append(f"- {key}: {values.get(key, '')}")
    for key, value in values.items():
        if key in METADATA_ORDER or key == "timestamp":
            continue
        lines.append(f"- {key}: {value}")  # forward-compat: неизвестные поля (§4.4 п.3)

    if body_status == "full":
        lines.append(_serialize_body(user_text, assistant_text))
    lines.append(END_MARKER)
    return "\n".join(lines)

