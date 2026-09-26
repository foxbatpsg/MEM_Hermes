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
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mm import MINIMEM_VERSION, digest, indexer, insert, paths  # noqa: E402
from mm.config import Config, ConfigError, load_config  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.project import current_cwd, resolve_project  # noqa: E402
from mm.store import Store, StoreUnavailable  # noqa: E402

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


def _open_store(config: Config, memory_root: Path) -> Store:
    """Открывает служебный слой; при отказе — диагностика, а не исключение (§19.2)."""

    store = Store(paths.sqlite_path(memory_root, config["sqlite_filename"]))
    store.create_schema()
    return store


def cmd_rebuild_index(config: Config, args: argparse.Namespace) -> int:
    """Полностью пересоздаёт индекс из журнала (§4.3, §21.5)."""

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    project = None
    if getattr(args, "project", None):
        project, _ = resolve_project(args.project, config["project_mapping"])
    try:
        store = _open_store(config, memory_root)
    except StoreUnavailable as exc:
        print(f"проблема: SQLite недоступен ({exc})")
        return EXIT_PROBLEMS
    with store:
        result = indexer.rebuild_index(store, memory_root, logger, project=project, config=config)
    print(
        f"rebuild-index: проиндексировано {result.records_indexed}, "
        f"заменено ревизий {result.revisions}, дублей {result.duplicates}, "
        f"повреждено пропущено {result.records_damaged}, курсоров сброшено "
        f"{result.cursor_resets}, {result.duration_ms} мс"
    )
    for error in result.errors:
        print(f"проблема: {error}")
    return EXIT_OK if result.ok else EXIT_PROBLEMS


def cmd_rebuild_digest(config: Config, args: argparse.Namespace) -> int:
    """Пересоздаёт файлы дайджестов из журнала (§14, §21.5).

    Административная операция: выполняется независимо от автоматических
    режимов. Поле «Последний ход» при пересборке не меняется (§11.1, П-07).
    """

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    project = None
    if getattr(args, "project", None):
        project, _ = resolve_project(args.project, config["project_mapping"])

    try:
        store = _open_store(config, memory_root)
    except StoreUnavailable as exc:
        print(f"проблема: SQLite недоступен ({exc})")
        return EXIT_PROBLEMS

    with store:
        results = digest.rebuild_digests(
            store,
            config,
            memory_root,
            logger,
            session_id=args.session,
            all_sessions=args.all,
            project=project,
        )

    if not results:
        print("rebuild-digest: подходящих сессий в индексе нет")
        return EXIT_PROBLEMS

    failures = 0
    for result in results:
        if result.status == "ok":
            print(
                f"{result.project}/{result.session_id}: {result.file}, "
                f"{result.chars} символов, ходов {result.turns_included} из {result.turns_total}"
            )
        elif result.status == "empty":
            print(f"{result.project}/{result.session_id}: записей нет, дайджест не создан")
        else:
            failures += 1
            print(f"проблема: {result.project}/{result.session_id}: {result.error}")
    print(f"итог: обработано {len(results)}, ошибок {failures}")
    return EXIT_PROBLEMS if failures else EXIT_OK


def cmd_show(config: Config, args: argparse.Namespace) -> int:
    """Показывает оригинальную запись журнала по event_id (§21.5)."""

    from mm import journal as journal_module

    memory_root = paths.config_memory_root(config)
    for path in paths.existing_journal_files(memory_root):
        records, _ = journal_module.read_records(path)
        for record in records:
            if record.event_id != args.event_id:
                continue
            key = paths.relative_to_root(path, memory_root)
            print(f"# {key}@{record.offset}")
            print(f"# turn={record.turn} session={record.session_id} revision={record.revision}")
            print(f"# content_hash={record.content_hash} truncated={record.metadata.get('truncated')}")
            print(f"### User\n{record.user_utterance}")
            print(f"### Assistant\n{record.assistant_answer}")
            return EXIT_OK
    print(f"проблема: запись {args.event_id} не найдена в журнале")
    return EXIT_PROBLEMS



def cmd_search(config: Config, args: argparse.Namespace) -> int:
    """Тот же пайплайн, что хук поиска, но без вставки и без счётчиков (§21.2).

    Команда административная: индекс, `usage_count` и `session_returns` не
    меняются. С `--explain` печатаются основы, собранный запрос, режим,
    score каждой кандидатуры и причина отсева — этого достаточно для
    калибровки `search_score_ratio` и `search_score_threshold` (П-11).
    """

    from mm import search as search_module

    memory_root = paths.config_memory_root(config)
    cwd = str(args.project) if getattr(args, "project", None) else current_cwd()
    project, _ = resolve_project(cwd, config["project_mapping"])

    try:
        store = _open_store(config, memory_root)
    except StoreUnavailable as exc:
        print(f"проблема: SQLite недоступен ({exc})")
        return EXIT_PROBLEMS

    text = " ".join(args.query) if isinstance(args.query, list) else str(args.query)
    with store:
        search_plan = search_module.plan(
            store,
            config,
            project,
            session_id="",
            text=text,
            # Вне хука фильтр проекта не применяется к выдаче FTS5, но чужие
            # записи всё равно отсеиваются: --explain должен показывать и их.
            restrict_project=False,
        )

    if not search_plan.stems:
        print("search: после нормализации не осталось ни одного термина")
        return EXIT_OK

    if args.explain:
        print(f"проект: {project}")
        print(f"основы: {', '.join(search_plan.stems)}")
        print(f"режим запроса: {search_plan.query_mode}")
        print(f"fts-запрос: {search_plan.match_query}")
        print(f"порог: {search_plan.threshold:.4f} ({search_plan.threshold_applied})")
        print(f"попаданий: {search_plan.hits}")
        for candidate in search_plan.candidates:
            verdict = (
                "выдаётся" if candidate.selected else f"отсев: {candidate.reject_reason}"
            )
            print(
                f"  {candidate.event_id} score={candidate.score:.4f} "
                f"session={candidate.session_id} turn={candidate.turn} — {verdict}"
            )

    body, kept, _cut = search_module.assemble_body(config, search_plan.selected)
    if body:
        block, truncated = insert.build_memory_block(
            body, int(config["max_return_chars"])
        )
        print(f"выдано записей: {len(kept)}" + (" (обрезано)" if truncated else ""))
        print("event_id: " + ", ".join(item.event_id for item in kept))
        print(block)
    else:
        print("выдано записей: 0")
    return EXIT_OK


def cmd_compact(config: Config, args: argparse.Namespace) -> int:
    """Ручное уплотнение: снятие записей с поиска без удаления истории (§21.5).

    Команда административная и выполняется при `mode_compaction = false`
    (§3.5, §3.7.7), но уважает `compaction_rule2_enabled`: выключенное
    правило 2 не включается этой командой (§15.1, тест MM-79).
    """

    from mm import compaction as compaction_module

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    try:
        store = _open_store(config, memory_root)
    except StoreUnavailable as exc:
        print(f"проблема: SQLite недоступен ({exc})")
        return EXIT_PROBLEMS

    with store:
        result = compaction_module.run_compaction(
            store,
            config,
            logger,
            dry_run=bool(getattr(args, "dry_run", False)),
        )

    counts = compaction_module.plan_compaction_counts(result.suppressions)
    mode = "пробный прогон" if getattr(args, "dry_run", False) else "уплотнение"
    print(
        f"compact: {mode}, просмотрено записей {result.scanned}, "
        f"снято {len(result.suppressions)} "
        f"(по возрасту {counts[compaction_module.REASON_AGE]}, "
        f"дублей {counts[compaction_module.REASON_DUPLICATE]}, "
        f"неиспользованных {counts[compaction_module.REASON_UNUSED]})"
    )
    print(
        f"правило 2: {'включено' if config['compaction_rule2_enabled'] else 'выключено'}, "
        f"mode_compaction: {'вкл' if config['mode_compaction'] else 'выкл'}"
    )
    if result.already_suppressed:
        print(f"уже снято ранее: {result.already_suppressed}")
    for error in result.errors:
        print(f"проблема: {error}")
    return EXIT_OK if result.ok else EXIT_PROBLEMS


def cmd_doctor(config: Config, args: argparse.Namespace) -> int:
    """Сверяет таймауты, суббюджеты, конфигурацию и резервную копию (§21.1).

    Команда только читает: ни один файл она не меняет. Работает и при
    недоступном SQLite — проверки таймаутов и копии от индекса не зависят.
    """

    from mm import doctor

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    report = doctor.run_doctor(
        config,
        memory_root,
        hermes_config=getattr(args, "hermes_config", None),
        backup_settings=getattr(args, "backup_settings", None),
    )

    hermes = doctor.read_hermes_hooks(getattr(args, "hermes_config", None))
    print(f"конфигурация MiniMem: {config.path}")
    print(f"config.yaml Hermes: {hermes.path} ({'прочитан' if hermes.exists else hermes.error})")
    if hermes.auto_accept is not None:
        print(f"hooks_auto_accept: {'true' if hermes.auto_accept else 'false'}")
    for line in report.hook_lines:
        print(f"  {line}")
    for note in report.notes:
        print(f"ok: {note}")
    print(f"backup: {report.backup.line()}")
    for finding in report.findings:
        print(f"проблема: {finding}")

    if report.findings:
        logger.log(
            "cli",
            "doctor",
            status="error",
            error_code=";".join(sorted({finding.code for finding in report.findings})),
            error_detail_code="; ".join(
                finding.message for finding in report.findings
            )[:500],
        )
    else:
        logger.log("cli", "doctor", status="ok")

    print(f"итог: {'проблемы' if report.findings else 'без проблем'}")
    return EXIT_PROBLEMS if report.findings else EXIT_OK


def cmd_stats(config: Config, args: argparse.Namespace) -> int:
    """Агрегирует структурированный лог за период (§21.3, П-10)."""

    from mm import stats

    memory_root = paths.config_memory_root(config)
    try:
        since = stats.parse_date_arg(args.since) if args.since else None
        until = stats.parse_date_arg(args.until) if args.until else None
    except ValueError as exc:
        print(f"проблема: {exc}")
        return EXIT_PROBLEMS

    project = None
    if getattr(args, "project", None):
        project, _ = resolve_project(args.project, config["project_mapping"])

    values = config.as_dict()
    values["__config_hash__"] = config.hash
    report = stats.collect_from_log(
        paths.log_path(memory_root, config["log_filename"]),
        config=values,
        since=since,
        until=until,
        project=project,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(report.render())
    return EXIT_OK


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
    """Проверки этапов 0 и 2: конфигурация, журнал, SQLite, соответствие индекса.

    Проверки по §21.5: доступность журнала и SQLite, записи журнала,
    отсутствующие в индексе, дубликаты, повреждения, курсоры, content_hash.
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
    if state == "damaged":
        problems.append(f"SQLite: {state} ({detail})")
    else:
        notes.append(f"SQLite: {state} ({detail})")

    if state == "missing" or not sqlite_file.is_file():
        notes.append("индекс отсутствует: запустите minimem rebuild-index")
    else:
        project = None
        if getattr(args, "projects", False):
            project, _ = resolve_project(current_cwd(), config["project_mapping"])
        try:
            store = _open_store(config, memory_root)
        except StoreUnavailable as exc:
            problems.append(f"SQLite недоступен для проверки индекса ({exc})")
            store = None
        if store is not None:
            with store:
                report = indexer.verify(
                    store,
                    memory_root,
                    logger,
                    project=project,
                    project_mapping=config["project_mapping"] if project else None,
                )
                notes.extend(report.notes)
                problems.extend(report.problems)
                for item in report.missing_in_index[:10]:
                    print(f"нет в индексе: {item}")
                for item in report.duplicates[:10]:
                    print(f"дубль: {item}")
                for item in report.bad_cursors[:10]:
                    print(f"курсор: {item}")
                for item in report.derived_turns[:10]:
                    print(f"производный ключ хода: {item}")
                for item in report.format_newer[:10]:
                    print(f"формат новее: {item}")
                for item in report.externally_modified[:10]:
                    print(f"внешнее переписывание: {item}")
                if project:
                    for item in report.project_mismatch[:10]:
                        print(f"расхождение project_path: {item}")
                if report.project_mismatch:
                    logger.log(
                        "indexer",
                        "project_mapping_changed",
                        status="error",
                        error_code="project_mapping_changed",
                        records_scanned=len(report.project_mismatch),
                    )
                print(
                    f"индекс: записей {store.meta_count()}, единиц FTS {store.fts_count()}, "
                    f"курсоров {len(store.cursor_all())}"
                )
                digests = digest.verify_digests(store, memory_root, logger, config, project)
                notes.extend(digests.notes)
                problems.extend(digests.problems)
                for item in (digests.damaged + digests.missing + digests.collisions)[:10]:
                    print(f"дайджест: {item}")

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
    print(f"итог: {'проблемы' if problems else 'без проблем'}")
    return EXIT_PROBLEMS if problems else EXIT_OK


def cmd_re_project(config: Config, args: argparse.Namespace) -> int:
    """Перепривязывает записи журнала к проектам по текущему маппингу (§21.4).

    Административная операция: выполняется независимо от автоматических
    режимов. Резервная копия журнала обязательна, без неё правка не
    выполняется. Индекс после команды перестраивается заново: `project`
    в служебном слое хранится отдельно от журнала.
    """

    from mm import reproject

    memory_root = paths.config_memory_root(config)
    logger = build_logger(config, memory_root)
    project = None
    if getattr(args, "project", None):
        project, _ = resolve_project(args.project, config["project_mapping"])

    result = reproject.run_reproject(memory_root, config["project_mapping"], logger, project)
    if result.backup_dir is not None:
        print(f"резервная копия: {result.backup_dir}")
    print(
        f"re-project: просмотрено записей {result.scanned}, "
        f"перепривязано {len(result.changed)}"
    )
    for item in result.changed[:20]:
        print(f"изменено: {item}")
    for error in result.errors:
        print(f"проблема: {error}")
    if result.ok and result.changed:
        with _open_store(config, memory_root) as store:
            indexer.rebuild_index(store, memory_root, logger, project=project, config=config)
    return EXIT_OK if result.ok else EXIT_PROBLEMS


COMMANDS = {
    "status": cmd_status,
    "verify": cmd_verify,
    "rebuild-index": cmd_rebuild_index,
    "rebuild-digest": cmd_rebuild_digest,
    "re-project": cmd_re_project,
    "show": cmd_show,
    "search": cmd_search,
    "compact": cmd_compact,
    "stats": cmd_stats,
    "doctor": cmd_doctor,
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

    rebuild = subparsers.add_parser("rebuild-index", help="пересоздать индекс из журнала")
    rebuild.add_argument("--project", type=Path, default=None, help="ограничить проектом")

    rebuild_digest = subparsers.add_parser(
        "rebuild-digest", help="пересоздать файл дайджеста из журнала"
    )
    rebuild_digest.add_argument("--session", default=None, help="session_id для пересборки")
    rebuild_digest.add_argument(
        "--all", action="store_true", help="пересобрать дайджесты всех сессий"
    )
    rebuild_digest.add_argument("--project", type=Path, default=None, help="ограничить проектом")

    show = subparsers.add_parser("show", help="показать запись журнала по event_id")
    show.add_argument("event_id")

    search_parser = subparsers.add_parser(
        "search",
        help="поиск по реплике без вставки и без влияния на счётчики (П-11)",
    )
    search_parser.add_argument("query", nargs="+", help="текст запроса")
    search_parser.add_argument(
        "--explain",
        action="store_true",
        help="основы, FTS-запрос, режим, score и причины отсева",
    )
    search_parser.add_argument(
        "--project", type=Path, default=None, help="каталог проекта для поиска"
    )

    compact_parser = subparsers.add_parser(
        "compact",
        help="механическое уплотнение: снять записи с поиска без удаления",
    )
    compact_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только показать, что будет снято, без записи в служебный слой",
    )

    reproject_parser = subparsers.add_parser(
        "re-project",
        help="перепривязать записи журнала к проектам по текущему маппингу (П-16)",
    )
    reproject_parser.add_argument(
        "--backup",
        action="store_true",
        help="уже сделанная резервная копия (по умолчанию копия создаётся всегда)",
    )
    reproject_parser.add_argument(
        "--project", type=Path, default=None, help="ограничить каталогом проекта"
    )

    verify_parser = subparsers.add_parser("verify", help="проверка целостности и конфигурации")
    verify_parser.add_argument(
        "--projects",
        action="store_true",
        help="ограничить проверку текущим проектом (П-16)",
    )

    stats_parser = subparsers.add_parser(
        "stats",
        help="агрегаты структурированного лога за период (П-10)",
    )
    stats_parser.add_argument("--since", default=None, help="дата или метка времени начала")
    stats_parser.add_argument("--until", default=None, help="дата или метка времени конца")
    stats_parser.add_argument("--project", type=Path, default=None, help="ограничить проектом")
    stats_parser.add_argument("--json", action="store_true", help="вывод в формате JSON")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="сверка таймаутов, суббюджетов, конфигурации и резервной копии (П-06, П-19)",
    )
    doctor_parser.add_argument(
        "--hermes-config",
        type=Path,
        default=None,
        help="путь к config.yaml Hermes (по умолчанию %%LOCALAPPDATA%%\\hermes\\config.yaml)",
    )
    doctor_parser.add_argument(
        "--backup-settings",
        type=Path,
        default=None,
        help="путь к backup.json рядом со скриптом копирования (П-19)",
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
