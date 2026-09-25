"""Хук post_llm_call: Захват.

Основание: ТЗ v1.7 §10 (хук Захвата), §26.2 (формат обмена), §19.2
(изоляция ошибок), план реализации v1.7 (этап 1).

Скрипт тонкий: читает stdin, вызывает модуль, пишет в stdout пустой объект
и всегда завершается кодом 0. Вся логика — в `mm.capture` (ТЗ §26.2).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm import capture, paths  # noqa: E402
from mm.config import ConfigError, default_config_path, load_config  # noqa: E402
from mm.log import Logger  # noqa: E402

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


def turn_from_event(event: dict) -> capture.TurnData:
    """Данные хода из события post_llm_call (§10, §26.2)."""

    extra = event.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    return capture.TurnData(
        user_message=str(extra.get("user_message") or ""),
        assistant_response=str(extra.get("assistant_response") or ""),
        session_id=str(event.get("session_id") or ""),
        task_id=str(extra.get("task_id") or ""),
        turn_id=extra.get("turn_id"),
        cwd=event.get("cwd"),
        sender_id=str(event.get("sender_id") or extra.get("sender_id") or ""),
        platform=str(event.get("platform") or extra.get("platform") or ""),
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
    if not config["mode_capture"]:
        logger.log("capture", "capture_disabled", status="skipped", hook_event="post_llm_call")
        print("{}")
        return 0

    try:
        capture.capture_turn(turn_from_event(event), config, logger, memory_root)
    except Exception as exc:  # noqa: BLE001 - граница хука (§19.2)
        logger.error("capture", "capture_failed", type(exc).__name__, hook_event="post_llm_call")

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
