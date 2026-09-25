"""Определение проекта по рабочему каталогу Hermes.

Основание: ТЗ v1.7 §8 (включая П-16).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

#: Проект, когда соответствие не найдено (ТЗ v1.7 §8).
DEFAULT_PROJECT = "common"


def normalize_path(value: str | os.PathLike[str] | None) -> str:
    """Нормализует путь перед сравнением (ТЗ v1.7 §8 п.1, 4, 6).

    Правила: приведение к абсолютному пути, разделители — прямой слэш,
    учёт регистра на Windows, trailing separators отбрасываются.
    """

    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        resolved = Path(raw).resolve()
    except OSError:
        return raw.replace("\\", "/").rstrip("/").casefold() if os.name == "nt" else raw
    text = resolved.as_posix()
    if len(text) > 1:
        text = text.rstrip("/")
    if os.name == "nt":
        text = text.casefold()
    return text


def current_cwd() -> str:
    """Нормализованный cwd; при недоступности — пустая строка (§8 п.5)."""

    try:
        return normalize_path(Path.cwd())
    except (OSError, RuntimeError):
        return ""


def resolve_project(
    cwd: str | os.PathLike[str] | None,
    project_mapping: Mapping[str, str] | None,
) -> tuple[str, str]:
    """Возвращает пару `(project, normalized_cwd)` (ТЗ v1.7 §8).

    Побеждает самое длинное совпадение префикса; сравнение регистронезависимо
    на Windows. При отсутствии соответствия или недоступном cwd — `common`.
    """

    normalized = normalize_path(cwd)
    if not normalized:
        return DEFAULT_PROJECT, ""

    best_prefix: str | None = None
    best_project = DEFAULT_PROJECT
    for raw_prefix, project in (project_mapping or {}).items():
        prefix = normalize_path(raw_prefix)
        if not prefix or not project:
            continue
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            if best_prefix is None or len(prefix) > len(best_prefix):
                best_prefix = prefix
                best_project = project
    return best_project, normalized


def project_from_cwd(
    cwd: str | os.PathLike[str] | None = None,
    project_mapping: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Определение проекта для текущего или указанного каталога."""

    target = current_cwd() if cwd is None else normalize_path(cwd)
    return resolve_project(target, project_mapping)
