"""Хук on_session_end: Уплотнение и Дайджест сессии.

Основание: ТЗ v1.7 §18 (конец сессии), §3.5 и §3.7.7 (`mode_compaction`),
§14 (Дайджест), §17 (`sessions.completed`), §26.1-§26.2 (регистрация и формат
обмена), §19.2 (изоляция ошибок); план реализации v1.7, этапы 3 и 7.

Скрипт тонкий: читает stdin, вызывает модули, пишет в stdout пустой объект и
всегда завершается кодом 0. Порядок §18: уплотнение (если
`mode_compaction = true`), затем дайджест, который создаётся независимо от
`mode_return`, если у сессии есть записи в индексе. Отказ одного шага не
отменяет другой: дайджест создаётся и при недоступном уплотнении.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm import compaction, digest, paths  # noqa: E402
from mm.config import ConfigError, default_config_path, load_config  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.project import resolve_project  # noqa: E402
from mm.store import Store  # noqa: E402

CONFIG_ENV = "MINIMEM_CONFIG"


def config_path() -> Path:
    """Файл конфигурации: `MINIMEM_CONFIG` либо `config.json` рядом с кодом."""

    import os

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


def run_compaction_step(
    store: Store, config, logger: Logger, session_id: str, project: str
) -> compaction.CompactionResult | None:
    """Уплотнение на `on_session_end` (§18, §3.5, §3.7.7).

    При `mode_compaction = false` пишется `compaction_skipped` и состояние
    снятия не меняется; отказ уплотнения не поднимается наружу (§19.2).
    """

    if not compaction.auto_compaction_enabled(config):
        return compaction.note_disabled(
            config, logger, session_id=session_id, project=project
        )
    return compaction.run_compaction(
        store, config, logger, session_id=session_id, project=project
    )


def mark_completed(store: Store, project: str, session_id: str, extra: Any) -> None:
    """Пишет `sessions.completed` по событию on_session_end (§17, §26.2).

    Поле отражает штатное завершение сессии: `completed` истёкшего и
    `interrupted` считаются незавершёнными, а отсутствие полей не мешает
    записи (MM-159).
    """

    values = extra if isinstance(extra, dict) else {}
    completed = 0
    for name in ("interrupted", "failed", "completed"):
        value = values.get(name)
        if isinstance(value, bool) and value:
            completed = 0 if name != "completed" else 1
            break
    try:
        with store.connection:
            store.session_upsert(session_id, project, completed=completed)
    except Exception:  # noqa: BLE001 - состояние не критично для дайджеста
        return


def run(event: dict, config, memory_root: Path, logger: Logger) -> digest.DigestResult | None:
    """Уплотняет и создаёт дайджест сессии из события on_session_end.

    Порядок §18: уплотнение -> дайджест. `None`, если работать не нужно.
    """

    session_id = str(event.get("session_id") or "")
    if not session_id.strip():
        logger.log("digest", "digest_empty_session", status="skipped", hook_event="on_session_end")
        return None
    project, _ = resolve_project(event.get("cwd"), config["project_mapping"])
    extra = event.get("extra") or {}
    completion = digest.completion_from_event(extra if isinstance(extra, dict) else None)
    try:
        store = Store(paths.sqlite_path(memory_root, config["sqlite_filename"]))
    except Exception as exc:  # noqa: BLE001 - без индекса дайджест не собрать
        logger.error("digest", "digest_failed", type(exc).__name__, session_id=session_id, project=project)
        return None
    with store:
        store.create_schema()
        mark_completed(store, project, session_id, extra)
        try:
            run_compaction_step(store, config, logger, session_id, project)
        except Exception as exc:  # noqa: BLE001 - дайджест создаётся и без него
            logger.error(
                "compaction",
                compaction.OPERATION_APPLIED,
                type(exc).__name__,
                session_id=session_id,
                project=project,
                hook_event="on_session_end",
            )
        return digest.build_digest(
            store, config, memory_root, logger, session_id, project, completion=completion
        )


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
        run(event, config, memory_root, logger)
    except Exception as exc:  # noqa: BLE001 - граница хука (§19.2)
        logger.error("digest", "digest_failed", type(exc).__name__, hook_event="on_session_end")

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
