"""Пути хранилища MiniMem и часовой пояс журнала.

Основание: ТЗ v1.7 §4.1, §20 (`journal_timezone`).

Структура хранилища (ТЗ v1.7 §4.1):

    <memory_root>/<project>/memory/YYYY-MM-DD.md
    <memory_root>/<project>/memory/digest/<session_id>.md
    <memory_root>/minimem.db
    <memory_root>/logs/minimem.log
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

JOURNAL_DIRNAME = "memory"
DIGEST_DIRNAME = "digest"
LOG_DIRNAME = "logs"
JOURNAL_DATE_FORMAT = "%Y-%m-%d"

#: Значение `project`, когда префикс не найден (ТЗ v1.7 §8).
DEFAULT_PROJECT = "common"


def resolve_timezone(journal_timezone: str) -> timezone:
    """Часовой пояс дневного файла журнала: `system` или `utc` (§4.1)."""

    if journal_timezone == "utc":
        return timezone.utc
    if journal_timezone != "system":
        raise ValueError(f"Недопустимый journal_timezone: {journal_timezone!r}")
    local = datetime.now().astimezone().tzinfo
    return local if isinstance(local, timezone) else timezone.utc


def local_date(journal_timezone: str, moment: datetime | None = None) -> date:
    """Дата дневного файла журнала в `journal_timezone`."""

    tz = resolve_timezone(journal_timezone)
    now = moment if moment is not None else datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    return now.astimezone(tz).date()


def now(tz: timezone | None = None) -> datetime:
    """Текущее время со смещением (timestamp в ISO-8601 с offset, §4.1)."""

    return datetime.now(tz or timezone.utc).astimezone()


def safe_component(name: str) -> str:
    """Имя каталога или файла, безопасное для Windows.

    Запрещённые символы заменяются подчёркиванием, хвост путей и пробелы
    убираются, длина ограничивается. Схема применяется к дайджестам (§14,
    П-12) и к именам проектов.
    """

    forbidden = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in forbidden or ord(ch) < 32 else ch for ch in name)
    cleaned = cleaned.strip().rstrip(". ").replace("..", "_")
    cleaned = cleaned or "unnamed"
    return cleaned[:120]


def project_dir(memory_root: Path, project: str) -> Path:
    """Каталог журнала проекта: `<memory_root>/<project>/memory`."""

    return memory_root / safe_component(project) / JOURNAL_DIRNAME


def journal_file(memory_root: Path, project: str, day: date) -> Path:
    """Дневной файл журнала: `<memory_root>/<project>/memory/YYYY-MM-DD.md` (§4.1)."""

    return project_dir(memory_root, project) / f"{day.strftime(JOURNAL_DATE_FORMAT)}.md"


def digest_dir(memory_root: Path, project: str) -> Path:
    """Каталог дайджестов проекта."""

    return project_dir(memory_root, project) / DIGEST_DIRNAME


def sqlite_path(memory_root: Path, sqlite_filename: str = "minimem.db") -> Path:
    """Путь служебной базы: `<memory_root>/minimem.db` (§4.2)."""

    return memory_root / sqlite_filename


def log_path(memory_root: Path, log_filename: str = "minimem.log") -> Path:
    """Путь технического лога MiniMem."""

    return memory_root / LOG_DIRNAME / log_filename


def existing_journal_files(memory_root: Path) -> list[Path]:
    """Все дневные файлы журнала во всех проектах, по возрастанию пути."""

    if not memory_root.is_dir():
        return []
    files: list[Path] = []
    for project_path in sorted(memory_root.iterdir(), key=lambda p: p.name.casefold()):
        journal = project_path / JOURNAL_DIRNAME
        if not journal.is_dir():
            continue
        for day_file in sorted(journal.glob("*.md"), key=lambda p: p.name):
            if day_file.is_file():
                files.append(day_file)
    return files


def ensure_directory(path: Path) -> bool:
    """Создаёт каталог; при отказе возвращает False (журнал не теряется)."""

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


def config_memory_root(values: Mapping[str, Any] | Any) -> Path:
    """`memory_root` из конфигурации как абсолютный путь."""

    raw = values["memory_root"] if not isinstance(values, Mapping) else values.get("memory_root", "")
    return Path(os.path.expandvars(str(raw))).expanduser().resolve()


def relative_to_root(path: Path, memory_root: Path) -> str:
    """Путь относительно `memory_root` через прямой слэш (для вывода и логов)."""

    try:
        return path.resolve().relative_to(memory_root).as_posix()
    except ValueError:
        return path.as_posix()
