"""Административная перепривязка записей журнала к проектам (П-16).

Основание: ТЗ v1.7 §8 п. 9-10, §21.4, §22.

`re-project` — единственный способ изменить `project` в существующих
записях: автоматическая миграция не выполняется. Команда обязана сделать
резервную копию журнала до правки и записать результат в лог; без копии
правка не выполняется вовсе.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import journal, paths
from .log import Logger
from .project import resolve_project

#: Операция в логе (§22: `project_mapping_changed`).
OPERATION_APPLIED = "project_mapping_changed"


@dataclass
class ReprojectResult:
    """Итог перепривязки по всем дневным файлам."""

    backup_dir: Path | None = None
    scanned: int = 0
    changed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def backup_root(memory_root: Path, moment: datetime | None = None) -> Path:
    """Каталог резервной копии журнала перед перепривязкой (§21.4)."""

    stamp = (moment or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return memory_root / "backups" / f"re-project-{stamp}"


def make_backup(memory_root: Path, moment: datetime | None = None) -> Path:
    """Копирует все дневные файлы журнала в новый каталог."""

    target = backup_root(memory_root, moment)
    target.mkdir(parents=True, exist_ok=False)
    for source in paths.existing_journal_files(memory_root):
        relative = paths.relative_to_root(source, memory_root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target


def _project_line_indexes(text: str) -> dict[str, int]:
    """Номера строк `- project:` по event_id для всех блоков дневного файла.

    Разбор идёт по маркерам записи, а не по смещениям из `journal`: смещения
    байтовые, а индексы здесь — позиции строк. Порядок полей метаданных
    фиксирован (§4.4 п.3) и `project` стоит раньше `event_id`, поэтому
    индекс строки запоминается и привязывается к событию при его чтении.
    """

    indexes: dict[str, int] = {}
    event = ""
    project_index: int | None = None
    inside = False
    for index, line in enumerate(text.split("\n")):
        if line == journal.BEGIN_MARKER:
            inside = True
            event = ""
            project_index = None
            continue
        if line == journal.END_MARKER:
            if project_index is not None and event and event not in indexes:
                indexes[event] = project_index
            inside = False
            continue
        if not inside:
            continue
        if line.startswith("- project: "):
            project_index = index
        elif line.startswith("- event_id: "):
            event = line.split(": ", 1)[1].strip()
    return indexes


def run_reproject(
    memory_root: Path,
    mapping: Mapping[str, Any],
    logger: Logger,
    project: str | None = None,
) -> ReprojectResult:
    """Перепривязывает записи журнала по текущему маппингу (MM-157, §21.4).

    Порядок: резервная копия всех дневных файлов -> правка поля `project` по
    значению `project_path` -> атомарная перезапись файлов. Правка меняет
    только строку `- project:`; тело записи и порядок записей сохраняются.
    Индекс перестраивается вызывающим кодом командой `rebuild-index`.
    """

    result = ReprojectResult()
    files = paths.existing_journal_files(memory_root)
    if project is not None:
        prefix = (project + "/").casefold()
        files = [
            path
            for path in files
            if paths.relative_to_root(path, memory_root).casefold().startswith(prefix)
        ]
    if not files:
        result.errors.append("дневных файлов журнала нет")
        return result

    try:
        result.backup_dir = make_backup(memory_root)
    except OSError as exc:
        result.errors.append(f"резервная копия не создана: {type(exc).__name__}")
        return result

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.errors.append(f"{path.name}: чтение не удалось ({type(exc).__name__})")
            continue
        records, _ = journal.read_records(path)
        result.scanned += len(records)
        indexes = _project_line_indexes(text)
        lines = text.split("\n")
        changed = 0
        for record in records:
            if record.damaged or not record.event_id:
                continue
            current, _ = resolve_project(record.metadata.get("project_path", ""), mapping)
            if current == record.project:
                continue
            index = indexes.get(record.event_id)
            if index is None:
                continue
            lines[index] = f"- project: {current}"
            changed += 1
            result.changed.append(
                f"{path.name}: {record.project} -> {current} ({record.event_id})"
            )
        if not changed:
            continue
        temporary = path.with_name(f"{path.name}.reproject.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            result.errors.append(f"{path.name}: запись не удалась ({type(exc).__name__})")

    logger.log(
        "indexer",
        OPERATION_APPLIED,
        status="error" if result.errors else "ok",
        error_code="reproject_failed" if result.errors else None,
        records_scanned=result.scanned,
        records_indexed=len(result.changed),
    )
    return result

    for source in paths.existing_journal_files(memory_root):
        relative = paths.relative_to_root(source, memory_root)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target
