#!/usr/bin/env python3
"""MiniMem-1 — CLI.

Основание: ТЗ v1.7 §21 (каркас этапа 0: команды `status` и `verify`),
план реализации v1.7, этап 0.

Команды `rebuild-index`, `rebuild-digest`, `show`, `compact`, `search`,
`stats`, `doctor`, `re-project` появляются на соответствующих этапах.
CLI-команды — административные операции и выполняются независимо от
автоматических режимов (§21).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm import MINIMEM_VERSION, paths  # noqa: E402
from mm.config import Config, ConfigError, load_config  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.project import current_cwd, resolve_project  # noqa: E402

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CONFIG = 2

#: Поля, которые `status` обязан показывать (ТЗ v1.7 §3.7.10, §21).
STATUS_FIELDS = (
    "mode_capture",
    "mode_return",
    "mode_search",
    "mode_compaction",
    "compaction_rule2_enabled",
    "journal_fsync",
    "journal_timezone",
    "catch_up_retry_max_turns",
    "catch_up_budget_seconds",
)


def build_logger(config: Config, memory_root: Path) -> Logger:
    """Технический лог рядом с хранилищем (ТЗ v1.7 §22)."""

    return Logger(paths.log_path(memory_root, config["log_filename"]))


def sqlite_state(sqlite_file: Path) -> tuple[str, str]:
    """Состояние SQLite: `missing`, `empty`, `ok`, `damaged`, `unreadable`."""

    if not sqlite_file.exists():
        return "missing", "файл не создан"
    if sqlite_file.stat().st_size == 0:
        return "empty", "файл нулевого размера"
    try:
        connection = sqlite3.connect(f"file:{sqlite_file.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return "unreadable", type(exc).__name__
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            return "damaged", str(integrity[0] if integrity else "нет результата")
        version = connection.execute("PRAGMA user_version").fetchone()
        return "ok", f"user_version={version[0] if version else 0}"
    except sqlite3.Error as exc:
        return "unreadable", type(exc).__name__
    finally:
        connection.close()


def journal_stats(memory_root: Path) -> tuple[int, int]:
    """Число дневных файлов журнала и их суммарный размер в байтах."""

    files = paths.existing_journal_files(memory_root)
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return len(files), total


def _flag(value: object) -> str:
    if isinstance(value, bool):
        return "вкл" if value else "выкл"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _project_count(memory_root: Path) -> int:
    if not memory_root.is_dir():
        return 0
    return sum(
        1
        for child in memory_root.iterdir()
        if child.is_dir() and (child / paths.JOURNAL_DIRNAME).is_dir()
    )


def _safe_size(path: Path) -> str:
    try:
        return f"{path.stat().st_size} байт"
    except OSError:
        return "нет файла"


def _writable(directory: Path) -> str:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"нет ({type(exc).__name__})"
    return "да"


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    """Путь журнала, число файлов, состояние SQLite, активные режимы (§21)."""

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    sqlite_file = paths.sqlite_path(memory_root, config["sqlite_filename"])
    state, detail = sqlite_state(sqlite_file)
    files_count, files_bytes = journal_stats(memory_root)
    project, project_path = resolve_project(current_cwd(), config["project_mapping"])

    print(f"minimem: версия {config['minimem_version']} (код {MINIMEM_VERSION})")
    print(f"конфигурация: {config.path}")
    print(f"config_hash: {config.hash}")
    print(f"project_mapping_hash: {config.mapping_hash}")
    print(f"memory_root: {memory_root}")
    print(f"memory_root существует: {'да' if memory_root.is_dir() else 'нет'}")
    print(f"журнал: {paths.project_dir(memory_root, project)}")
    print(f"проектов: {_project_count(memory_root)}")
    print(f"текущий проект: {project} (project_path: {project_path or '—'})")
    print(f"файлов журнала: {files_count} ({files_bytes} байт)")
    print(f"SQLite: {sqlite_file.name} — состояние {state} ({detail})")
    print(f"размер индекса: {_safe_size(sqlite_file)}")
    print("режимы и флаги:")
    for field in STATUS_FIELDS:
        print(f"  {field}: {_flag(config[field])}")
    print(
        f"hook_deadline_ms: pre_llm={config.hook_deadline_ms('pre_llm')}, "
        f"post_llm={config.hook_deadline_ms('post_llm')}"
    )
    print(f"журнал доступен для записи: {_writable(paths.project_dir(memory_root, project))}")
    print("состояние текущей сессии: не отслеживается до этапа 2 (SQLite)")
    print(f"состояние курсоров индексатора: появляется на этапе 2")
    print(f"ошибка последнего запуска: {logger.last_error or 'нет'}")
    if config.issues:
        print("проблемы конфигурации (config_invalid):")
        for issue in config.issues:
            print(f"  {issue}")
    else:
        print("конфигурация: валидна")
    return EXIT_OK if not config.issues else EXIT_PROBLEMS


def cmd_verify(config: Config, args: argparse.Namespace) -> int:
    """Проверки этапа 0: конфигурация, доступность журнала, состояние SQLite.

    Проверки соответствия журнала и индекса появляются на этапе 2 вместе
    с `mm/indexer.py` (ТЗ v1.7 §21.5).
    """

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    sqlite_file = paths.sqlite_path(memory_root, config["sqlite_filename"])
    problems: list[str] = []
    notes: list[str] = []

    if config.issues:
        problems.extend(config.issues)
    else:
        notes.append("конфигурация: инварианты выполнены")

    if not memory_root.is_dir():
        if paths.ensure_directory(memory_root):
            notes.append(f"memory_root создан: {memory_root}")
        else:
            problems.append("memory_root недоступен: каталог не создан и не может быть создан")
    else:
        notes.append(f"memory_root доступен для чтения: {memory_root}")

    files_count, _ = journal_stats(memory_root)
    notes.append(
        f"дневных файлов журнала: {files_count}"
        if files_count
        else "дневных файлов журнала пока нет"
    )

    state, detail = sqlite_state(sqlite_file)
    if state in ("ok", "missing"):
        notes.append(
            f"SQLite: {state} ({detail}) — производный слой, наполняется rebuild-index на этапе 2"
        )
    else:
        problems.append(f"SQLite: {state} ({detail})")

    logger.log(
        "cli",
        "verify",
        status="error" if problems else "ok",
        error_code="verify_problems" if problems else None,
        error_detail_code="verify_config" if config.issues else None,
        records_scanned=files_count,
    )

    for note in notes:
        print(f"ok: {note}")
    for problem in problems:
        print(f"проблема: {problem}")
    print(
        f"итог: {'проблемы' if problems else 'без проблем'}; "
        "проверки этапа 2 (соответствие журнала и индекса) ещё не реализованы"
    )
    return EXIT_PROBLEMS if problems else EXIT_OK


COMMANDS = {
    "status": cmd_status,
    "verify": cmd_verify,
}


def build_parser() -> argparse.ArgumentParser:
    """Разбор командной строки (ТЗ v1.7 §21)."""

    parser = argparse.ArgumentParser(
        prog="minimem",
        description="MiniMem-1: минимальная активная долгосрочная память для Hermes Agent",
    )
    parser.add_argument("--version", action="version", version=MINIMEM_VERSION)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="путь к файлу конфигурации (по умолчанию config.json рядом с кодом)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="путь журнала, число файлов, SQLite, режимы")
    verify_parser = subparsers.add_parser("verify", help="проверка целостности и конфигурации")
    verify_parser.add_argument(
        "--projects",
        action="store_true",
        help="проекты с несоответствием project_path (реализуется на этапе 2, П-16)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI: код 0 — успех, 1 — нарушения, 2 — ошибка конфигурации."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"проблема: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if config.issues:
        build_logger(config, paths.config_memory_root(config)).log(
            "config",
            "config_invalid",
            status="error",
            error_code="config_invalid",
            error_detail_code="; ".join(config.issues),
        )
    return COMMANDS[args.command](config, args)


if __name__ == "__main__":
    raise SystemExit(main())
