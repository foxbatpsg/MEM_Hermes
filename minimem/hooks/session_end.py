"""Хук on_session_end: Дайджест сессии.

Основание: ТЗ v1.7 §18 (конец сессии), §14 (Дайджест), §26.1-§26.2
(регистрация и формат обмена), §19.2 (изоляция ошибок); план реализации
v1.7, этап 3.

Скрипт тонкий: читает stdin, вызывает `mm.digest`, пишет в stdout пустой
объект и всегда завершается кодом 0. Дайджест создаётся независимо от
`mode_return`, если у сессии есть записи в индексе (§18).

Уплотнение (`mode_compaction`) в этом этапе не выполняется: оно относится к
этапу 6 плана.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm import digest, paths  # noqa: E402
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


def run(event: dict, config, memory_root: Path, logger: Logger) -> digest.DigestResult | None:
    """Создаёт дайджест сессии из события on_session_end; `None`, если не нужно."""

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
