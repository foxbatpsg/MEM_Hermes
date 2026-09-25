"""Атомарная запись в журнал, блокировка и разбор дневного файла.

Основание: ТЗ v1.7 §4.1, §4.4 (формат и границы записи), §4.5
(атомарность, `msvcrt.locking`, повреждённые записи), П-13.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .canonical import (
    ASSISTANT_SECTION,
    BEGIN_MARKER,
    END_MARKER,
    USER_SECTION,
    MetadataError,
    unescape_body_line,
)

#: Размер блока, занимаемого под `msvcrt.locking` (§4.5.2).
LOCK_REGION = 1

#: Число попыток и пауза при неудачной блокировке (§4.5.2).
LOCK_RETRIES = 3
LOCK_RETRY_DELAY_S = 0.05


class JournalLockTimeout(Exception):
    """Блокировка не получена за отведённое число попыток (§4.5.2)."""


class JournalWriteError(Exception):
    """Запись не подтверждена после append (journal_append_unverified)."""


@dataclass
class ParsedRecord:
    """Запись журнала, разобранная по маркерам и секциям (§4.4)."""

    offset: int
    metadata: dict[str, str] = field(default_factory=dict)
    extra_metadata: dict[str, str] = field(default_factory=dict)
    user_utterance: str = ""
    assistant_answer: str = ""
    body_status: str = "full"
    fmt: int = 1
    damaged: bool = False
    damaged_reason: str = ""
    metadata_error: str = ""
    raw: str = ""

    @property
    def is_damaged(self) -> bool:
        return self.damaged

    @property
    def event_id(self) -> str:
        return self.metadata.get("event_id", "")

    @property
    def content_hash(self) -> str:
        return self.metadata.get("content_hash", "")

    @property
    def session_id(self) -> str:
        return self.metadata.get("session_id", "")

    @property
    def project(self) -> str:
        return self.metadata.get("project", "")

    @property
    def turn(self) -> str:
        return self.metadata.get("turn", "")

    @property
    def revision(self) -> int:
        try:
            return int(self.metadata.get("revision", "1") or 1)
        except ValueError:
            return 1


@dataclass
class DamagedEvent:
    """Повреждённая запись: одна строка лога на связку (файл, смещение, mtime)."""

    offset: int
    reason: str


def _lock(handle) -> None:
    """Занимает межпроцессную блокировку на дескрипторе журнала (§4.5.2)."""

    import time

    try:
        import msvcrt
    except ImportError:  # pragma: no cover - POSIX
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return

    for attempt in range(LOCK_RETRIES):
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, LOCK_REGION)
            return
        except OSError:
            if attempt == LOCK_RETRIES - 1:
                raise JournalLockTimeout("блокировка журнала не получена")
            time.sleep(LOCK_RETRY_DELAY_S)


def _unlock(handle) -> None:
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - POSIX
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, LOCK_REGION)
    except OSError:
        pass


def append_record(
    journal_file: Path,
    record_text: str,
    fsync: bool = True,
) -> int:
    """Дописывает одну целостную запись под блокировкой (§4.5.1).

    Порядок операций: lock → seek в конец файла → write одной записью →
    flush → fsync → unlock. Возвращает смещение записи в файле.
    Разделительная пустая строка добавляется перед записью, если файл
    непуст и не заканчивается пустой строкой.
    """

    journal_file = Path(journal_file)
    journal_file.parent.mkdir(parents=True, exist_ok=True)
    payload = record_text.replace("\r\n", "\n").replace("\r", "\n")
    if not payload.endswith("\n"):
        payload += "\n"

    with open(journal_file, "a+b") as handle:
        _lock(handle)
        try:
            handle.seek(0, os.SEEK_END)
            size_before = handle.tell()
            prefix = b""
            if size_before:
                # Между записями ровно одна пустая строка (§4.4).
                handle.seek(size_before - 1)
                last = handle.read(1)
                if last == b"\n":
                    handle.seek(size_before - 2)
                    before_last = handle.read(1) if size_before >= 2 else b"\n"
                    prefix = b"" if before_last == b"\n" else b"\n"
                else:
                    prefix = b"\n\n"
            offset = size_before + len(prefix)
            handle.seek(0, os.SEEK_END)
            handle.write(prefix + payload.encode("utf-8"))
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
            size_after = handle.tell()
        finally:
            _unlock(handle)

    written = len(prefix) + len(payload.encode("utf-8"))
    if size_before + written != size_after or not payload.endswith(END_MARKER + "\n"):
        raise JournalWriteError("запись не подтверждена после append")
    return offset


def _split_metadata(line: str) -> tuple[str, str] | None:
    """Разбирает строку метаданных `- key: value`; иначе — не метаданные."""

    if not line.startswith("- "):
        return None
    key, _, value = line[2:].partition(":")
    return key.strip(), value.strip()


def _parse_body(lines: list[str]) -> tuple[str, str, bool]:
    """Разбирает тело по секциям `### User` / `### Assistant` (§4.4 п.7, 14)."""

    user_start = lines.index(USER_SECTION) if USER_SECTION in lines else -1
    assistant_start = lines.index(ASSISTANT_SECTION) if ASSISTANT_SECTION in lines else -1
    if user_start < 0 or assistant_start < 0 or assistant_start < user_start:
        return "", "", True
    user_lines = lines[user_start + 1 : assistant_start]
    assistant_lines = lines[assistant_start + 1 :]
    return (
        "\n".join(unescape_body_line(line) for line in user_lines),
        "\n".join(unescape_body_line(line) for line in assistant_lines),
        False,
    )


KNOWN_METADATA_KEYS = (
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

_CLOSED_ENUM = {
    "truncated": ("none", "user", "assistant", "both"),
    "body_status": ("full", "withheld"),
    "turn_source": ("hermes", "derived"),
}


def parse_records(text: str, offsets: list[int] | None = None) -> tuple[list[ParsedRecord], list[DamagedEvent]]:
    """Разбирает дневной файл журнала конечным автоматом (§4.5.3).

    Повреждённой считается только нарушение структуры (маркеры, порядок
    секций). Неизвестные ключи метаданных сохраняются и повреждением не
    считаются; недопустимое значение закрытого перечисления даёт ошибку
    метаданных записи, а не journal_record_damaged (§4.4 п.3, MM-100).

    `offsets` — байтовые смещения строк; если не переданы, смещения
    считаются в символах (для синтетического текста в тестах).
    """

    records: list[ParsedRecord] = []
    damaged: list[DamagedEvent] = []
    position = 0
    state = "OUTSIDE"
    current: ParsedRecord | None = None
    buffer: list[str] = []

    for index, line in enumerate(text.split("\n")):
        line_offset = offsets[index] if offsets is not None else position
        position += len(line) + 1

        if line == BEGIN_MARKER:
            if state == "INSIDE" and current is not None:
                current.damaged = True
                current.damaged_reason = "nested_begin"
                records.append(current)
                damaged.append(DamagedEvent(current.offset, "nested_begin"))
            current = ParsedRecord(offset=line_offset)
            buffer = []
            state = "INSIDE"
            continue

        if line == END_MARKER and state == "INSIDE" and current is not None:
            _finish_record(current, buffer, records, damaged)
            state = "OUTSIDE"
            current = None
            buffer = []
            continue

        if state == "INSIDE" and current is not None:
            buffer.append(line)

    if state == "INSIDE" and current is not None:
        current.damaged = True
        current.damaged_reason = "no_end_marker"
        records.append(current)
        damaged.append(DamagedEvent(current.offset, "no_end_marker"))

    return records, damaged


def _finish_record(
    record: ParsedRecord,
    buffer: list[str],
    records: list[ParsedRecord],
    damaged: list[DamagedEvent],
) -> None:
    record.raw = "\n".join(buffer)
    body_started = False
    body_lines: list[str] = []
    for line in buffer:
        if not body_started and (line in (USER_SECTION, ASSISTANT_SECTION)):
            body_started = True
            body_lines.append(line)
            continue
        if body_started:
            body_lines.append(line)
            continue
        pair = _split_metadata(line)
        if pair is None:
            if line.startswith("## "):
                record.metadata.setdefault("_heading", line[3:].strip())
            continue
        key, value = pair
        if key in KNOWN_METADATA_KEYS:
            record.metadata[key] = value
        else:
            # forward-compat: неизвестный ключ сохраняется (§4.4 п.3, MM-100)
            record.extra_metadata[key] = value

    for key, allowed in _CLOSED_ENUM.items():
        value = record.metadata.get(key)
        if value is not None and value not in allowed:
            # Ошибка метаданных, а не повреждение структуры (§4.4 п.3, MM-100).
            record.metadata_error = f"{key}={value}"
            damaged.append(DamagedEvent(record.offset, "metadata_invalid"))
            break

    fmt_raw = record.metadata.get("fmt", "1")
    try:
        record.fmt = int(fmt_raw)
    except ValueError:
        record.fmt = 1
    record.body_status = record.metadata.get("body_status", "full")

    if record.body_status == "full":
        user_text, assistant_text, broken = _parse_body(body_lines)
        if broken:
            record.damaged = True
            record.damaged_reason = "sections_order"
            damaged.append(DamagedEvent(record.offset, "sections_order"))
        record.user_utterance = user_text
        record.assistant_answer = assistant_text

    records.append(record)


def read_records(journal_file: Path) -> tuple[list[ParsedRecord], list[DamagedEvent]]:
    """Читает и разбирает дневной файл журнала; отсутствующий файл — пусто.

    Смещения записей — байтовые: именно они хранятся в служебном слое
    как `journal_offset` (§9.1) и сверяются в `verify` (§21.5).
    """

    path = Path(journal_file)
    if not path.is_file():
        return [], []
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    offsets: list[int] = []
    position = 0
    for line in raw.split(b"\n"):
        offsets.append(position)
        position += len(line) + 1
    return parse_records(text, offsets)


def iter_records(journal_file: Path) -> Iterator[ParsedRecord]:
    """Итератор по корректным записям дневного файла."""

    records, _ = read_records(journal_file)
    for record in records:
        if not record.damaged:
            yield record

