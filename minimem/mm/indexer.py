"""Индексатор: производный SQLite/FTS5-слой из журнала.

Основание: ТЗ v1.7 §3.6 (Индексатор), §9 (FTS5 Index), §9.1 (схема),
§9.2 (инкрементальная индексация), §4.3 (восстановление), §19.4 (параллельность).

Индексатор читает журнал, а не данные Захвата (§2). Он никогда не удаляет
и не изменяет записи журнала: любая ошибка приводит к пропуску записи и
сохранению курсора на прежней позиции, чтобы запись была обработана позже.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import canonical, journal, paths
from .log import Logger
from .store import Store

#: Максимум записей за один проход (§9.2 п.12, §19.1).
BATCH_LIMIT = 500

#: Доля бюджета хука, доступная индексатору (§19.1.1).
INDEXER_SHARE = 0.40


@dataclass
class IndexResult:
    """Итог одного запуска индексатора."""

    files_scanned: int = 0
    records_scanned: int = 0
    records_indexed: int = 0
    records_suppressed: int = 0
    records_damaged: int = 0
    duplicates: int = 0
    revisions: int = 0
    cursor_resets: int = 0
    batch_limited: bool = False
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def turn_numeric(turn: str) -> int | None:
    """Числовое значение turn для сортировки; None, если не число (§9.1)."""

    text = (turn or "").strip()
    if text.isdigit():
        return int(text)
    return None


def record_to_meta(
    record: journal.ParsedRecord,
    journal_file: str,
    memory_root: Path,
) -> dict[str, Any]:
    """Собирает строку `memory_meta` из записи журнала (§9.1)."""

    timestamp = record.metadata.get("_heading", "")
    if not timestamp:
        timestamp = record.metadata.get("timestamp", "")
    stamp = timestamp.split(" · ", 1)[0].strip()
    return {
        "event_id": record.event_id,
        "content_hash": record.content_hash,
        "project": record.project,
        "session_id": record.session_id,
        "task_id": record.metadata.get("task_id", ""),
        "turn": record.turn,
        "turn_numeric": turn_numeric(record.turn),
        "timestamp": stamp,
        "timestamp_utc": _utc_from_stamp(stamp),
        "truncated": record.metadata.get("truncated", "none"),
        "journal_file": journal_file,
        "journal_offset": record.offset,
        "revision": record.revision,
        "fmt": record.fmt,
    }


def _utc_from_stamp(stamp: str) -> str:
    """Нормализованное время UTC из ISO-8601 с offset (§4.1)."""

    if not stamp:
        return ""
    try:
        return canonical.timestamp_utc(datetime.fromisoformat(stamp))
    except ValueError:
        return stamp


def _record_body(record: journal.ParsedRecord) -> tuple[str, str]:
    """Текст записи для индекса; `withheld` даёт пустой текст (П-08)."""

    if record.body_status != "full":
        return "", ""
    return record.user_utterance, record.assistant_answer


def index_record(
    store: Store,
    record: journal.ParsedRecord,
    journal_file: str,
    memory_root: Path,
) -> str:
    """Индексирует одну запись: `indexed`, `duplicate`, `replaced` или `skipped`.

    Порядок решений (§3.6, §9.2 п.9-10.1):
    - event_id отсутствует в индексе — добавить;
    - event_id есть и content_hash совпадает — физический дубль, пропустить;
    - event_id есть, content_hash отличается, revision больше — заменить единицу;
    - event_id есть, revision не больше — старая запись канонична, пропустить.
    """

    if not record.event_id or not record.content_hash:
        return "skipped"

    meta = record_to_meta(record, journal_file, memory_root)
    existing = store.meta_get(record.event_id)
    user_text, assistant_text = _record_body(record)

    if existing is None:
        store.fts_upsert(record.event_id, user_text, assistant_text)
        store.meta_upsert(meta)
        return "indexed"

    if existing["content_hash"] == record.content_hash:
        return "duplicate"

    if record.revision > int(existing["revision"] or 1):
        store.fts_upsert(record.event_id, user_text, assistant_text)
        store.meta_upsert(meta)
        return "replaced"

    return "skipped"


def _resolve_offset(
    store: Store,
    journal_path: Path,
    key: str,
    logger: Logger,
    result: IndexResult,
) -> tuple[int, bool]:
    """Возвращает `(byte_offset, reset_needed)` для файла (§9.2 п.2-5).

    Курсор сбрасывается, если файла не было в базе, файл уменьшился или
    сохранённое смещение выходит за пределы файла.
    """

    try:
        stat = journal_path.stat()
    except OSError as exc:
        result.errors.append(f"{journal_path.name}: {type(exc).__name__}")
        return 0, False

    cursor = store.cursor_get(key)
    if cursor is None:
        return 0, False

    offset = int(cursor.get("byte_offset") or 0)
    known_size = cursor.get("file_size")
    if known_size is not None and stat.st_size < int(known_size):
        logger.log("indexer", "cursor_reset", status="ok", journal_file=key, error_detail_code="file_shrunk")
        result.cursor_resets += 1
        return 0, True
    if offset > stat.st_size:
        logger.log("indexer", "cursor_reset", status="ok", journal_file=key, error_detail_code="offset_out_of_range")
        result.cursor_resets += 1
        return 0, True
    return offset, False


def _read_complete_records(
    path: Path,
    start_offset: int,
    end_offset: int,
) -> tuple[list[journal.ParsedRecord], list[journal.DamagedEvent], int]:
    """Читает диапазон байтов и возвращает записи, курсор и повреждения.

    Курсор указывает на конец последней полной записи: неполная запись в
    конце диапазона не продвигает его (§9.2 п.7-8).
    """

    with path.open("rb") as handle:
        handle.seek(start_offset)
        raw = handle.read(max(0, end_offset - start_offset))

    text = raw.decode("utf-8", errors="replace")
    offsets: list[int] = []
    position = 0
    for line in raw.split(b"\n"):
        offsets.append(start_offset + position)
        position += len(line) + 1

    records, damaged = journal.parse_records(text, offsets)
    seen: set[tuple[int, str]] = {(item.offset, item.reason) for item in damaged}
    complete: list[journal.ParsedRecord] = []
    for record in records:
        if record.damaged:
            key = (record.offset, record.damaged_reason)
            if key not in seen:
                seen.add(key)
                damaged.append(journal.DamagedEvent(record.offset, record.damaged_reason))
            continue
        if not record.metadata:
            key = (record.offset, "missing_metadata")
            if key not in seen:
                seen.add(key)
                damaged.append(journal.DamagedEvent(record.offset, "missing_metadata"))
            continue
        complete.append(record)

    return complete, damaged, records[-1].end_offset if records else start_offset


def index_journal_file(
    store: Store,
    journal_path: Path,
    memory_root: Path,
    logger: Logger,
    result: IndexResult,
    batch_left: int,
) -> int:
    """Индексирует один дневной файл; возвращает число использованных записей.

    Курсор продвигается только после успешной транзакции (§9.2 п.11), при
    сбое запись будет обработана при следующем запуске.
    """

    key = paths.relative_to_root(journal_path, memory_root)
    offset, _ = _resolve_offset(store, journal_path, key, logger, result)
    size = journal_path.stat().st_size
    if offset >= size:
        return 0

    records, damaged, _ = _read_complete_records(journal_path, offset, size)
    result.files_scanned += 1
    mtime = int(journal_path.stat().st_mtime)
    for item in damaged:
        # Одна строка лога на связку (файл, смещение, mtime) — §4.5.3 п.7, П-13.
        binding = f"{key}@{item.offset}@{mtime}"
        if store.damaged_reported(binding):
            continue
        with store.connection:
            store.mark_damaged_reported(binding)
        logger.log(
            "indexer",
            "journal_record_damaged",
            status="skipped",
            journal_file=key,
            error_detail_code=item.reason,
        )
        result.records_damaged += 1

    used = 0
    processed_end = offset
    connection = store.connection
    for record in records:
        if used >= batch_left:
            result.batch_limited = True
            break
        result.records_scanned += 1
        try:
            with connection:
                outcome = index_record(store, record, key, memory_root)
        except sqlite3.Error as exc:
            logger.error("indexer", "index_record_failed", type(exc).__name__, journal_file=key)
            result.errors.append(f"{key}: {type(exc).__name__}")
            break
        used += 1
        processed_end = record.end_offset
        if outcome == "indexed":
            result.records_indexed += 1
        elif outcome == "replaced":
            result.revisions += 1
        elif outcome == "duplicate":
            result.duplicates += 1
            logger.log(
                "indexer",
                "journal_duplicate_event_id",
                status="skipped",
                journal_file=key,
                event_id=record.event_id,
            )

    if used:
        # Курсор продвигается только вперёд и только до конца последней
        # обработанной полной записи: остаток ждёт следующего запуска
        # (§9.2 п.8, 11). Откатываться назад нельзя — иначе сброс курсора
        # приводил бы к повторному чтению уже обработанного хвоста.
        new_offset = max(processed_end, offset)
        with connection:
            store.cursor_upsert(
                key,
                new_offset,
                journal_path.stat().st_size,
                journal_path.stat().st_mtime,
                _now_iso(),
            )
    return used


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def index_project(
    store: Store,
    memory_root: Path,
    logger: Logger,
    project: str | None = None,
    batch_limit: int = BATCH_LIMIT,
) -> IndexResult:
    """Инкрементально индексирует журнал проекта (§3.6, §9.2).

    За один проход обрабатывается не более `batch_limit` записей; остаток
    дожидается следующего запуска.
    """

    started = time.monotonic()
    result = IndexResult()
    files = paths.existing_journal_files(memory_root)
    if project is not None:
        prefix = (project + "/").casefold()
        files = [
            path
            for path in files
            if paths.relative_to_root(path, memory_root).casefold().startswith(prefix)
        ]

    for journal_path in files:
        if result.records_indexed + result.revisions >= batch_limit:
            result.batch_limited = True
            break
        index_journal_file(
            store, journal_path, memory_root, logger, result, batch_limit
        )

    result.duration_ms = int((time.monotonic() - started) * 1000)
    logger.log(
        "indexer",
        "rebuild_index" if result.cursor_resets else "incremental_index",
        status="error" if result.errors else "ok",
        records_scanned=result.records_scanned,
        records_indexed=result.records_indexed,
        records_suppressed=result.records_suppressed,
        error_code="index_errors" if result.errors else None,
    )
    return result


def rebuild_index(
    store: Store,
    memory_root: Path,
    logger: Logger,
    project: str | None = None,
) -> IndexResult:
    """Полностью пересоздаёт индексный слой из журнала (§4.3, §21.5).

    Счётчики использования и снятие по правилу 2 не восстанавливаются:
    после пересборки эти записи возвращаются в поиск (§4.3, §15).
    """

    store.drop_index_layer()
    store.create_schema()
    logger.log("indexer", "rebuild_index", status="ok", records_indexed=0)
    return index_project(store, memory_root, logger, project=project)


@dataclass
class VerifyReport:
    """Результат проверки целостности (§21.5)."""

    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    missing_in_index: list[str] = field(default_factory=list)
    missing_in_journal: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    damaged: list[str] = field(default_factory=list)
    derived_turns: list[str] = field(default_factory=list)
    bad_cursors: list[str] = field(default_factory=list)
    hash_mismatch: list[str] = field(default_factory=list)
    multi_revision: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify(
    store: Store,
    memory_root: Path,
    logger: Logger,
    project: str | None = None,
) -> VerifyReport:
    """Проверяет соответствие журнала и индекса (§21.5).

    Проверяются: доступность индексных таблиц, записи журнала, отсутствующие
    в индексе, дубликаты event_id, повреждённые записи, производные ключи,
    недействительные курсоры, соответствие content_hash и ревизии.
    """

    report = VerifyReport()
    indexed = store.meta_event_ids()
    journal_ids: set[str] = set()
    seen_by_event: dict[str, int] = {}

    for journal_path in paths.existing_journal_files(memory_root):
        key = paths.relative_to_root(journal_path, memory_root)
        records, damaged = journal.read_records(journal_path)
        for item in damaged:
            report.damaged.append(f"{key}@{item.offset}:{item.reason}")
        for record in records:
            if record.damaged or not record.event_id:
                continue
            seen_by_event[record.event_id] = seen_by_event.get(record.event_id, 0) + 1
            journal_ids.add(record.event_id)
            if record.metadata.get("turn_source") == "derived":
                report.derived_turns.append(record.event_id)
            meta = store.meta_get(record.event_id)
            if meta is None:
                report.missing_in_index.append(f"{key}@{record.offset} {record.event_id}")
                continue
            if meta["journal_offset"] != record.offset:
                report.missing_in_index.append(
                    f"{key}@{record.offset} {record.event_id}: смещение в индексе "
                    f"{meta['journal_offset']}"
                )
            if meta["content_hash"] != record.content_hash:
                report.hash_mismatch.append(record.event_id)

    for event_id, count in seen_by_event.items():
        if count > 1:
            report.duplicates.append(f"{event_id}: {count} физических записей")
            revisions = [
                record.revision
                for path in paths.existing_journal_files(memory_root)
                for record in journal.read_records(path)[0]
                if record.event_id == event_id
            ]
            if len(set(revisions)) > 1:
                report.multi_revision.append(event_id)

    for event_id in sorted(indexed - journal_ids):
        report.missing_in_journal.append(event_id)

    for cursor in store.cursor_all():
        if project and not cursor["journal_file"].casefold().startswith(project.casefold()):
            continue
        path = memory_root / cursor["journal_file"]
        if not path.is_file():
            report.bad_cursors.append(f"{cursor['journal_file']}: файла нет")
            continue
        size = path.stat().st_size
        if int(cursor["byte_offset"] or 0) > size:
            report.bad_cursors.append(
                f"{cursor['journal_file']}: смещение {cursor['byte_offset']} больше размера {size}"
            )
        elif size < int(cursor["file_size"] or 0):
            report.bad_cursors.append(f"{cursor['journal_file']}: файл уменьшился")

    for name, items in (
        ("записи журнала, отсутствующие в индексе", report.missing_in_index),
        ("записи индекса, отсутствующие в журнале", report.missing_in_journal),
        ("duplicate event_id в журнале", report.duplicates),
        ("повреждённые записи журнала", report.damaged),
        ("недействительные journal_cursors", report.bad_cursors),
        ("несоответствие content_hash", report.hash_mismatch),
    ):
        if items:
            report.problems.append(f"{name}: {len(items)}")
        else:
            report.notes.append(f"{name}: нет")

    if report.derived_turns:
        report.notes.append(
            f"записи с turn_source=derived: {len(report.derived_turns)} "
            f"({', '.join(report.derived_turns[:5])})"
        )
    if report.multi_revision:
        report.notes.append(f"event_id с несколькими ревизиями: {len(report.multi_revision)}")

    logger.log(
        "indexer",
        "verify",
        status="error" if report.problems else "ok",
        error_code="verify_problems" if report.problems else None,
        records_indexed=store.meta_count(),
    )
    return report


