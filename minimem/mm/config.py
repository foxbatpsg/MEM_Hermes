"""Загрузка и валидация конфигурации MiniMem.

Основание: ТЗ v1.7 §20, §20.1 (П-14), §3.7.

Значения по умолчанию заданы здесь, а не в `config.json`: файл конфигурации
содержит нормативные пути и отличия от умолчаний. Ключи, отсутствующие в
файле, берутся из `DEFAULTS`; неизвестные ключи и неверные типы порождают
проблему `config_invalid` и переход на безопасные значения по умолчанию
(ТЗ v1.7 §20 п.4).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

DEFAULT_CONFIG_FILENAME = "config.json"

#: Нормативные значения по умолчанию (ТЗ v1.7 §20).
DEFAULTS: dict[str, Any] = {
    # Служебное.
    "minimem_version": "1.7.0",
    "memory_root": "",
    "sqlite_filename": "minimem.db",
    "log_filename": "minimem.log",
    "project_mapping": {},
    # Режимы работы (§3.7.1).
    "mode_capture": True,
    "mode_return": False,
    "mode_search": False,
    "mode_compaction": False,
    "compaction_rule2_enabled": False,
    # Ограничения размеров (§6).
    "max_user_text": 2000,
    "max_assistant_text": 4000,
    "max_memory_record": 8000,
    "max_digest_chars": 1500,
    "digest_turns": 3,
    "max_return_records": 5,
    "max_return_chars": 1500,
    "max_search_query": 256,
    "max_search_terms": 8,
    # Поиск (§3.3, П-11).
    "search_score_threshold": 1.0,
    "search_score_ratio": 0.35,
    "search_min_word_len": 4,
    "search_stem_min_len": 4,
    # Уплотнение (§15, П-04).
    "compaction_age": 180,
    "compaction_unused_age": 30,
    "compaction_shrink_ratio": 0.6,
    "compaction_min_drop": 6,
    "compaction_min_chars_drop": 3000,
    "compaction_cooldown_turns": 2,
    "max_return_injections_per_session": 3,
    # Журнал (§4.5).
    "journal_fsync": True,
    "journal_timezone": "system",
    # Catch-up (§18.1).
    "catch_up_retry_max_turns": 2,
    "catch_up_budget_seconds": 8,
    # Таймауты (§19.1, П-06).
    "pre_llm_timeout": 15,
    "post_llm_timeout": 30,
    "hook_safety_margin_ms": 2000,
    "return_min_budget_ms": 300,
    "redaction_timeout_ms": 200,
    "fail_closed": False,
}

#: Ключи, участвующие в `config_hash` и в событии `config_changed` (П-14).
#: Значения шаблонов redaction сюда не входят.
BEHAVIORAL_KEYS: tuple[str, ...] = tuple(
    key
    for key in DEFAULTS
    if key not in ("memory_root", "sqlite_filename", "log_filename", "minimem_version")
)

_BOOL_KEYS = frozenset(
    {
        "mode_capture",
        "mode_return",
        "mode_search",
        "mode_compaction",
        "compaction_rule2_enabled",
        "journal_fsync",
        "fail_closed",
    }
)

_INT_KEYS = frozenset(
    {
        "max_user_text",
        "max_assistant_text",
        "max_memory_record",
        "max_digest_chars",
        "digest_turns",
        "max_return_records",
        "max_return_chars",
        "max_search_query",
        "max_search_terms",
        "search_min_word_len",
        "search_stem_min_len",
        "compaction_age",
        "compaction_unused_age",
        "compaction_min_drop",
        "compaction_min_chars_drop",
        "compaction_cooldown_turns",
        "max_return_injections_per_session",
        "catch_up_retry_max_turns",
        "pre_llm_timeout",
        "post_llm_timeout",
        "hook_safety_margin_ms",
        "return_min_budget_ms",
        "redaction_timeout_ms",
    }
)

_FLOAT_KEYS = frozenset(
    {
        "search_score_threshold",
        "search_score_ratio",
        "compaction_shrink_ratio",
        "catch_up_budget_seconds",
    }
)

_STR_KEYS = frozenset({"journal_timezone", "minimem_version"})

#: Допустимые значения `journal_timezone` (ТЗ v1.7 §4.1).
JOURNAL_TIMEZONES = ("system", "utc")

#: Доли суббюджетов от `hook_deadline_ms` (ТЗ v1.7 §19.1.1, П-06).
CATCH_UP_SHARE = 0.40
COMPACTION_DIGEST_SHARE = 0.30


def default_config_path() -> Path:
    """Путь файла конфигурации: `config.json` рядом с кодом MiniMem."""

    return Path(__file__).resolve().parent.parent / DEFAULT_CONFIG_FILENAME


def _coerce_value(key: str, value: Any) -> tuple[Any, str | None]:
    """Приводит значение к нормативному типу; при неудаче — значение по умолчанию."""

    fallback = DEFAULTS[key]
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value, None
        return fallback, f"{key}: ожидается boolean"
    if key in _INT_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            return fallback, f"{key}: ожидается int"
        return value, None
    if key in _FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return fallback, f"{key}: ожидается число"
        return float(value), None
    if key in _STR_KEYS:
        if not isinstance(value, str) or not value:
            return fallback, f"{key}: ожидается непустая строка"
        return value, None
    if key == "project_mapping":
        if not isinstance(value, dict):
            return {}, f"{key}: ожидается объект path_prefix -> project"
        mapping: dict[str, str] = {}
        for prefix, project in value.items():
            if not isinstance(prefix, str) or not isinstance(project, str):
                return {}, f"{key}: ключи и значения должны быть строками"
            if not prefix.strip() or not project.strip():
                return {}, f"{key}: пустой path_prefix или project"
            mapping[prefix] = project
        return mapping, None
    if not isinstance(value, str):
        return fallback, f"{key}: ожидается строка"
    return value, None


def _coerce_all(raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    values = dict(DEFAULTS)
    issues: list[str] = []
    for key, value in raw.items():
        if key not in DEFAULTS:
            issues.append(f"config_unknown_key: {key}")
            continue
        coerced, issue = _coerce_value(key, value)
        values[key] = coerced
        if issue:
            issues.append(f"config_invalid: {issue}")
    return values, issues


def hook_deadline_ms(values: Mapping[str, Any], hook: str) -> int:
    """Нормативный дедлайн хука: предел минус запас (ТЗ v1.7 §19.1.1)."""

    timeout_key = f"{hook}_timeout"
    timeout_s = values.get(timeout_key, DEFAULTS[timeout_key])
    margin_ms = values["hook_safety_margin_ms"]
    return int(float(timeout_s) * 1000) - int(margin_ms)


def validate(values: Mapping[str, Any]) -> list[str]:
    """Инварианты конфигурации (ТЗ v1.7 §20 п.3, П-14).

    Возвращает список проблем вида `config_invalid: <ключ> <нарушение>`.
    """

    issues: list[str] = []
    if values["compaction_unused_age"] >= values["compaction_age"]:
        issues.append("config_invalid: compaction_unused_age не меньше compaction_age")
    if values["search_score_threshold"] < 0:
        issues.append("config_invalid: search_score_threshold отрицателен")
    if not 0 < values["search_score_ratio"] <= 1:
        issues.append("config_invalid: search_score_ratio вне интервала (0, 1]")
    if values["digest_turns"] < 1:
        issues.append("config_invalid: digest_turns меньше 1")
    if values["journal_timezone"] not in JOURNAL_TIMEZONES:
        issues.append("config_invalid: journal_timezone недопустим")
    deadline_ms = hook_deadline_ms(values, "pre_llm")
    if values["catch_up_budget_seconds"] * 1000 >= deadline_ms:
        issues.append("config_invalid: catch_up_budget_seconds не меньше hook_deadline_ms")
    subbudget_ms = (
        deadline_ms * CATCH_UP_SHARE
        + deadline_ms * COMPACTION_DIGEST_SHARE
        + values["return_min_budget_ms"]
    )
    if subbudget_ms > deadline_ms:
        issues.append("config_invalid: сумма суббюджетов больше hook_deadline_ms")
    if not str(values["memory_root"]).strip():
        issues.append("config_invalid: memory_root не задан")
    return issues


def _reset_invalid_keys(values: dict[str, Any], issues: list[str]) -> None:
    """Возвращает к безопасным значениям по умолчанию нарушенные ключи.

    `memory_root` и маппинг не сбрасываются: они задаются при установке,
    и их потеря сделала бы работу невозможной (ТЗ v1.7 §20 п.4).
    """

    protected = ("memory_root", "project_mapping", "sqlite_filename", "log_filename")
    for issue in issues:
        if not issue.startswith("config_invalid: "):
            continue
        detail = issue.split("config_invalid: ", 1)[1]
        key = detail.split(" ")[0]
        if key in values and key not in protected:
            values[key] = DEFAULTS[key]


class ConfigError(Exception):
    """Файл конфигурации отсутствует или не является объектом JSON."""


def config_hash(values: Mapping[str, Any]) -> str:
    """Отпечаток поведенческих параметров (ТЗ v1.7 §20, П-14)."""

    payload = json.dumps(
        {key: values[key] for key in sorted(BEHAVIORAL_KEYS)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def project_mapping_hash(values: Mapping[str, Any]) -> str:
    """Отпечаток маппинга path_prefix -> project (ТЗ v1.7 §8 п.8, П-16)."""

    mapping = values.get("project_mapping") or {}
    payload = json.dumps(
        {str(key).casefold(): str(project) for key, project in sorted(mapping.items())},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class Config:
    """Неизменяемая выборка конфигурации вместе с результатом её проверки."""

    __slots__ = ("_values", "path", "issues", "hash", "mapping_hash")

    def __init__(
        self,
        values: Mapping[str, Any],
        path: Path | None = None,
        issues: tuple[str, ...] = (),
    ) -> None:
        self._values = dict(values)
        self.path = path
        self.issues = issues
        self.hash = config_hash(self._values)
        self.mapping_hash = project_mapping_hash(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:  # pragma: no cover - защитный код
            raise AttributeError(name) from exc

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self._values.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    @property
    def modes(self) -> dict[str, bool]:
        return {
            "mode_capture": self._values["mode_capture"],
            "mode_return": self._values["mode_return"],
            "mode_search": self._values["mode_search"],
            "mode_compaction": self._values["mode_compaction"],
        }

    def hook_deadline_ms(self, hook: str) -> int:
        return hook_deadline_ms(self._values, hook)


def load_config(path: Path | None = None) -> Config:
    """Читает конфигурацию, проверяет типы и инварианты.

    Нарушение инварианта не останавливает работу: нарушенный ключ
    возвращается к безопасному значению по умолчанию, а проблема попадает
    в `Config.issues` (ТЗ v1.7 §20 п.4). Пути и маппинг не сбрасываются.
    """

    config_path = Path(path) if path is not None else default_config_path()
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"Файл конфигурации не найден: {config_path}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Файл конфигурации не является корректным JSON: {config_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Файл конфигурации должен быть объектом JSON: {config_path}")

    values, issues = _coerce_all(raw)
    for issue in validate(values):
        if issue not in issues:
            issues.append(issue)
    _reset_invalid_keys(values, issues)
    return Config(values, path=config_path, issues=tuple(issues))


