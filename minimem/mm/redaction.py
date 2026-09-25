"""Redaction секретов детерминированными placeholder'ами.

Основание: ТЗ v1.7 §7, §7.1, §20 (`redaction_timeout_ms`), П-08.

Placeholder зависит только от категории секрета, а не от его значения:
одинаковый секрет всегда даёт один и тот же placeholder и один и тот же
`content_hash` (MM-107). В лог попадают только категория и количество
замен, фрагменты секретов не пишутся (§7.1 п.5-6).
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Callable, Iterable, NamedTuple

#: Категории секретов и их placeholder'ы (§7.1).
PLACEHOLDERS: dict[str, str] = {
    "anthropic_key": "[REDACTED:anthropic_key]",
    "openai_key": "[REDACTED:api_key]",
    "github_token": "[REDACTED:access_token]",
    "slack_token": "[REDACTED:access_token]",
    "google_key": "[REDACTED:api_key]",
    "aws_access_key": "[REDACTED:api_key]",
    "aws_secret_key": "[REDACTED:cloud_secret]",
    "bearer": "[REDACTED:bearer]",
    "jwt": "[REDACTED:jwt]",
    "private_key": "[REDACTED:private_key]",
    "connection_string": "[REDACTED:connection_string]",
    "password": "[REDACTED:password]",
    "access_token": "[REDACTED:access_token]",
    "admin_secret": "[REDACTED:admin_secret]",
}

#: Категория по ключу вхождения вида `password=...` (§7.1).
KEY_VALUE_CATEGORIES: dict[str, str] = {
    "password": "password",
    "passwd": "password",
    "pwd": "password",
    "secret": "admin_secret",
    "client_secret": "admin_secret",
    "token": "access_token",
    "access_token": "access_token",
    "refresh_token": "access_token",
    "api_key": "openai_key",
    "apikey": "openai_key",
    "api-key": "openai_key",
    "access_key": "aws_access_key",
    "secret_key": "aws_secret_key",
}


class RedactionTemplate(NamedTuple):
    """Имя категории и линейный по времени regex (§7.1 п.2-3)."""

    name: str
    pattern: re.Pattern[str]


def _compile(name: str, pattern: str) -> RedactionTemplate:
    return RedactionTemplate(name, re.compile(pattern))


#: Встроенный набор шаблонов (§7.1). Вложенных квантификаторов нет.
BUILTIN_TEMPLATES: tuple[RedactionTemplate, ...] = (
    _compile(
        "private_key",
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
    ),
    _compile("anthropic_key", r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    _compile("openai_key", r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    _compile("github_token", r"gh[pousr]_[A-Za-z0-9]{16,}"),
    _compile("slack_token", r"xox[baprse]-[A-Za-z0-9\-]{10,}"),
    _compile("google_key", r"AIza[0-9A-Za-z_\-]{35}"),
    _compile("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _compile("jwt", r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),
    _compile(
        "aws_secret_key",
        r"(?i:aws_secret_access_key|aws_secret_key)\s*[:=]\s*[A-Za-z0-9/+=]{40}",
    ),
    _compile("bearer", r"(?i:bearer)\s+[A-Za-z0-9\-._~+/]{12,}={0,2}"),
    _compile(
        "connection_string",
        r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@[^\s/]+",
    ),
    _compile(
        "key_value",
        r"(?i:\b(?:password|passwd|pwd|secret|client_secret|token|access_token"
        r"|refresh_token|api_key|apikey|api-key|access_key|secret_key)\b)"
        r"\s*[:=]\s*(?:\"[^\"\n]{3,}\"|'[^'\n]{3,}'|[^\s,;]{3,})",
    ),
)


class RedactionTimeout(Exception):
    """Превышен `redaction_timeout_ms` — сбой redaction (П-08 п.3)."""


class RedactionResult:
    """Текст после redaction, счётчики замен и отпечаток набора шаблонов."""

    __slots__ = ("text", "counts", "redaction_set", "total")

    def __init__(
        self,
        text: str,
        counts: dict[str, int],
        redaction_set: str,
    ) -> None:
        self.text = text
        self.counts = counts
        self.total = sum(counts.values())
        self.redaction_set = redaction_set

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        return f"RedactionResult(total={self.total}, set={self.redaction_set})"


def redaction_set_hash(
    templates: Iterable[RedactionTemplate] = BUILTIN_TEMPLATES,
) -> str:
    """Первые 8 hex-символов sha256 набора шаблонов (§4.4)."""

    payload = "|".join(f"{item.name}:{item.pattern.pattern}" for item in templates)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _key_value_category(match: re.Match[str]) -> str:
    key = match.group(0).split(":", 1)[0].split("=", 1)[0].strip().casefold()
    return KEY_VALUE_CATEGORIES.get(key, "admin_secret")


def _replacement_for(
    name: str,
    match: re.Match[str],
    key_value_resolver: Callable[[re.Match[str]], str],
) -> str:
    if name == "key_value":
        return PLACEHOLDERS[key_value_resolver(match)]
    if name == "aws_secret_key":
        return PLACEHOLDERS["aws_secret_key"]
    if name == "connection_string":
        return PLACEHOLDERS["connection_string"]
    return PLACEHOLDERS.get(name, "[REDACTED:admin_secret]")


def redact(
    text: str,
    templates: Iterable[RedactionTemplate] = BUILTIN_TEMPLATES,
    timeout_ms: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RedactionResult:
    """Заменяет секреты детерминированными placeholder'ами (§7).

    `timeout_ms` — бюджет из конфигурации; его превышение равносильно сбою
    redaction и обрабатывается вызывающим кодом как `withheld` (П-08).
    Проверка бюджета выполняется между шаблонами: регулярные выражения
    линейны, поэтому один шаблон не может занять неограниченное время.
    """

    result = text
    counts: dict[str, int] = {}
    ordered = tuple(templates)
    started = clock()
    for template in ordered:
        if timeout_ms is not None and (clock() - started) * 1000 > timeout_ms:
            raise RedactionTimeout(f"redaction превысил {timeout_ms} мс")

        category = template.name
        counter = [0]

        def _replace(match: re.Match[str], _template=template) -> str:
            counter[0] += 1
            return _replacement_for(_template.name, match, _key_value_category)

        result = template.pattern.sub(_replace, result)
        if counter[0]:
            counts[category] = counts.get(category, 0) + counter[0]
    if timeout_ms is not None and (clock() - started) * 1000 > timeout_ms:
        raise RedactionTimeout(f"redaction превысил {timeout_ms} мс")
    return RedactionResult(result, counts, redaction_set_hash(ordered))


def redact_pair(
    user_utterance: str,
    assistant_answer: str,
    timeout_ms: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[RedactionResult, RedactionResult]:
    """Redaction обеих частей хода с общим бюджетом `timeout_ms` (§7)."""

    started = clock()
    user = redact(user_utterance, timeout_ms=timeout_ms, clock=clock)
    spent_ms = (clock() - started) * 1000
    remaining = None if timeout_ms is None else max(0.0, timeout_ms - spent_ms)
    assistant = redact(assistant_answer, timeout_ms=remaining, clock=clock)
    return user, assistant
