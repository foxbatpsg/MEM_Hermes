"""Хук pre_llm_call: режимы, Возврат, Поиск по реплике.

Основание: ТЗ v1.7 §12 (хук Поиска), §3.3, §18 (жизненный цикл хуков),
§26.2 (формат обмена), §19.1 (таймаут pre_llm_call), §19.2 (изоляция
ошибок); план реализации v1.7, этапы 4 и 5.

Скрипт тонкий: читает stdin, вызывает модули, пишет в stdout
`{"context": "..."}` либо пустой объект и всегда завершается кодом 0.

Порядок §18: состояние сессии -> метрики истории и решение о сжатии ->
Возврат при `mode_return` -> Поиск при `mode_search` на ходах, где Возврат
не вставлял. Любой отказ перехватывается здесь и не выходит наружу.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm import insert, paths, ret, search, session  # noqa: E402
from mm.config import ConfigError, default_config_path, hook_deadline_ms, load_config  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.project import resolve_project  # noqa: E402
from mm.store import Store  # noqa: E402

#: Переменная окружения с путём к конфигурации (используется в тестах).
CONFIG_ENV = "MINIMEM_CONFIG"


def config_path() -> Path:
    """Файл конфигурации: `MINIMEM_CONFIG` либо `config.json` рядом с кодом."""

    override = os.environ.get(CONFIG_ENV)
    return Path(override) if override else default_config_path()


def read_event(stream) -> dict:
    """Читает полезную нагрузку Hermes; битые байты не мешают (§7.1 п.8)."""

    raw = stream.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def deadline_remaining(config, started: float) -> int:
    """Остаток нормативного дедлайна `pre_llm_call` в миллисекундах (§19.1.1, П-06).

    Отрицательное значение означает, что предел уже превышен: модуль не
    бросает исключение, а пишет факт в лог.
    """

    deadline_ms = hook_deadline_ms(config, "pre_llm")
    return int(deadline_ms - (time.monotonic() - started) * 1000)


def handle_turn(
    event: dict, config, memory_root: Path, logger: Logger, started: float | None = None
):
    """Один ход `pre_llm_call`: учёт состояния, детекция сжатия, Возврат, Поиск.

    Порядок §18: состояние сессии -> метрики истории и решение о сжатии ->
    Возврат при `mode_return` -> Поиск при `mode_search` на ходах, где
    Возврат ничего не вставил. Возвращается результат фактически
    вставленного модуля. Любой отказ перехватывается вызывающим хуком
    и не выходит наружу (§19.2).
    """

    started = time.monotonic() if started is None else started
    turn = session.turn_from_event(event)
    project, _ = resolve_project(turn.cwd, config["project_mapping"])
    result = ret.ReturnResult(session_id=turn.session_id)

    if not config["mode_return"] and not config["mode_search"]:
        logger.log(
            "return",
            "return_disabled",
            status="skipped",
            session_id=turn.session_id,
            project=project,
            hook_event="pre_llm_call",
        )
        return result

    if not turn.has_session_id:
        logger.log(
            "return",
            "return_no_session_id",
            status="skipped",
            project=project,
            hook_event="pre_llm_call",
            error_detail_code="missing_session_id",
        )
        return result

    try:
        store = Store(paths.sqlite_path(memory_root, config["sqlite_filename"]))
    except Exception as exc:  # noqa: BLE001 - без индекса Возврат не работает
        logger.error(
            "return",
            "return_unavailable",
            type(exc).__name__,
            session_id=turn.session_id,
            project=project,
            hook_event="pre_llm_call",
        )
        return result

    with store:
        store.create_schema()
        remaining_ms = deadline_remaining(config, started)
        session.touch_session(store, project, turn)
        state, decision = session.register_turn(
            store, config, logger, project, turn, deadline_remaining_ms=remaining_ms
        )
        if config["mode_return"]:
            result = ret.perform_return(
                store,
                config,
                memory_root,
                logger,
                project,
                turn,
                state,
                decision=decision,
                deadline_remaining_ms=deadline_remaining(config, started),
            )
        if config["mode_search"] and not result.inserted:
            if turn.is_first_turn or (decision is not None and decision.detected):
                # Поиск на первом ходе и на ходе Возврата не запускается
                # (§3.3, §12): оба модуля решают это по флагу первого хода.
                logger.log(
                    "search",
                    "search_skipped",
                    status="skipped",
                    session_id=turn.session_id,
                    project=project,
                    hook_event="pre_llm_call",
                    search_terms=0,
                    search_hits=0,
                    search_returned=0,
                    error_detail_code="return_turn",
                )
            else:
                result = search.perform_search(
                    store,
                    config,
                    logger,
                    project,
                    turn.session_id,
                    # Блок памяти прошлой вставки не участвует в поиске
                    # текущей реплики (§10 п.3).
                    insert.strip_memory_block(turn.user_message).text,
                    deadline_remaining_ms=deadline_remaining(config, started),
                )
    return result


def main() -> int:
    try:
        event = read_event(sys.stdin)
    except Exception:  # noqa: BLE001 - хук не имеет права упасть (§19.2)
        event = {}

    try:
        config = load_config(config_path())
    except ConfigError:
        print("{}")
        return 0

    memory_root = paths.config_memory_root(config)
    logger = Logger(paths.log_path(memory_root, config["log_filename"]))
    try:
        result = handle_turn(event, config, memory_root, logger)
    except Exception as exc:  # noqa: BLE001 - граница хука (§19.2)
        logger.error("hook", "return_failed", type(exc).__name__, hook_event="pre_llm_call")
        print("{}")
        return 0

    if result.inserted:
        print(json.dumps({"context": result.text}, ensure_ascii=False))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
