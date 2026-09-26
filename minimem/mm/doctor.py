"""Диагностика MiniMem: команда `minimem doctor`.

Основание: ТЗ v1.7 §21.1 (П-06), §19.1 и §19.1.1 (таймауты, `hook_deadline_ms`
и суббюджеты), §20 и §20.1 (инварианты конфигурации), §21.1 (П-19 — контроль
резервной копии), §22 (логирование), §25 (критерий готовности 29).

Команда только читает: ни `config.yaml` Hermes, ни `backup.json`, ни рабочее
хранилище она не меняет. Проверка копии и сверка таймаутов не зависят от
SQLite, поэтому `doctor` работает и при недоступном служебном слое.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import (
    CATCH_UP_SHARE,
    COMPACTION_DIGEST_SHARE,
    Config,
    catch_up_budget_ms,
    digest_budget_ms,
)

#: Коды расхождений, которые `doctor` сообщает (ТЗ v1.7 §21.1).
CODE_CONFIG_INVALID = "config_invalid"
CODE_BUDGET_MISMATCH = "budget_mismatch"
CODE_BACKUP_STALE = "backup_stale"

#: Решения по резервной копии (ТЗ v1.7 §21.1).
BACKUP_OK = "годна"
BACKUP_STALE = "устаревает"
BACKUP_MISSING = "не найдена"
BACKUP_SKIPPED = "пропущена"

#: Порог свежести копии в сутках, если ключ `backup_stale_days` не задан
#: владельцем в эксплуатационном `backup.json` (П-19).
DEFAULT_BACKUP_STALE_DAYS = 2

#: События Hermes и соответствующие им внутренние пределы MiniMem
#: (§26.1: `on_session_end` ограничен тем же пределом, что `post_llm_call`).
HERMES_EVENTS: dict[str, str] = {
    "pre_llm_call": "pre_llm",
    "post_llm_call": "post_llm",
    "on_session_end": "post_llm",
}

#: Имя файла настроек копирования рядом со скриптом (документ «Установка,
#: бэкап и восстановление», раздел 3).
BACKUP_SETTINGS_FILENAME = "backup.json"

#: Имя каталога копирования; он же — имя файла настроек без расширения.
BACKUP_DIRNAME = "minimem-backup"

_SNAPSHOT_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YAML_KEY = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.*)$")
_YAML_ITEM = re.compile(
    r"^(?P<indent>\s*)-\s*(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.*)$"
)


@dataclass
class Finding:
    """Одно расхождение: код нормы и текст для владельца."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class BackupReport:
    """Результат проверки резервной копии журнала (П-19)."""

    decision: str = BACKUP_SKIPPED
    settings_path: Path | None = None
    settings_note: str = ""
    backup_root: Path | None = None
    snapshot_date: date | None = None
    snapshot_path: Path | None = None
    age_days: int | None = None
    threshold_days: int = DEFAULT_BACKUP_STALE_DAYS
    threshold_source: str = "по умолчанию"
    has_journal: bool = False
    has_config: bool = False
    detail: str = ""
    settings_found: list[Path] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.decision in (BACKUP_OK, BACKUP_SKIPPED)

    def settings_line(self) -> str:
        """Откуда взяты настройки копирования: путь и число найденных файлов."""

        if self.settings_path is None:
            return ""
        if len(self.settings_found) > 1:
            return (
                f"настройки {self.settings_path} (более свежий из "
                f"{len(self.settings_found)} найденных)"
            )
        return f"настройки {self.settings_path}"

    def line(self) -> str:
        """Строка отчёта: дата, путь, возраст в сутках и решение (§21.1)."""

        settings = self.settings_line()
        if self.decision == BACKUP_SKIPPED:
            if not settings:
                return f"резервная копия: {self.detail or self.settings_note}"
            return f"резервная копия: {self.detail or self.settings_note}; {settings}"
        if self.snapshot_path is None:
            return f"резервная копия: {self.decision} ({self.detail})"
        return (
            f"резервная копия: {self.decision}; дата {self.snapshot_date}, "
            f"путь {self.snapshot_path}, возраст {self.age_days} сут, "
            f"порог {self.threshold_days} сут ({self.threshold_source}); {self.detail}"
            f"; {settings}"
        )


@dataclass
class DoctorReport:
    """Итог `doctor`: находки, строки сверки таймаутов и проверка копии."""

    findings: list[Finding] = field(default_factory=list)
    hook_lines: list[str] = field(default_factory=list)
    backup: BackupReport = field(default_factory=BackupReport)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def add(self, code: str, message: str) -> None:
        self.findings.append(Finding(code, message))



# --- чтение конфигурации Hermes (config.yaml) --------------------------------


@dataclass
class HermesHook:
    """Одна запись массива хуков Hermes (§26.1)."""

    event: str
    command: str
    timeout_s: int | None


@dataclass
class HermesHooks:
    """Разобранный блок `hooks` и `hooks_auto_accept` из `config.yaml`."""

    path: Path
    exists: bool = False
    hooks: list[HermesHook] = field(default_factory=list)
    auto_accept: bool | None = None
    error: str = ""

    def by_event(self, event: str) -> list[HermesHook]:
        return [hook for hook in self.hooks if hook.event == event]

    def timeout_s(self, event: str) -> int | None:
        """Наименьший предел среди записей события: берётся он (§19.1)."""

        values = [
            hook.timeout_s for hook in self.by_event(event) if hook.timeout_s is not None
        ]
        return min(values) if values else None

    def minimem_commands(self, marker: str) -> list[str]:
        lowered = marker.casefold()
        return [hook.command for hook in self.hooks if lowered in hook.command.casefold()]


def hermes_home() -> Path:
    """Домашний каталог Hermes: `%LOCALAPPDATA%\\hermes` (ТЗ v1.7 §26)."""

    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return Path("hermes")
    return Path(local) / "hermes"


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _as_int(value: str) -> int | None:
    text = _unquote(value)
    try:
        return int(float(text))
    except ValueError:
        return None



def parse_hooks_yaml(text: str) -> tuple[list[HermesHook], bool | None]:
    """Разбирает блок `hooks` и `hooks_auto_accept` без сторонних библиотек.

    Поддерживается форма §26.1: отображение верхнего уровня, ключ `hooks`,
    событие с массивом элементов вида `- command: ...` и `timeout: N`. Иная
    форма даёт пустой список и `None` вместо `hooks_auto_accept` — это
    диагностика, а не исключение (§19.2).
    """

    hooks: list[HermesHook] = []
    auto_accept: bool | None = None
    in_hooks = False
    hooks_indent = 0
    event: str | None = None
    event_indent = 0
    command = ""
    timeout: int | None = None

    def flush() -> None:
        nonlocal command, timeout
        if event is not None and (command or timeout is not None):
            hooks.append(HermesHook(event=event, command=command, timeout_s=timeout))
        command = ""
        timeout = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _YAML_ITEM.match(line)
        if item and item.group("key") == "command" and in_hooks:
            indent = len(item.group("indent"))
            if event is None:
                continue
            if indent <= event_indent:
                # Элемент вне события: разбор блока закончен.
                flush()
                event = None
                continue
            if command or timeout is not None:
                # У события несколько записей: предыдущая завершена.
                flush()
            command = _unquote(item.group("value"))
            continue
        match = _YAML_KEY.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        key = match.group("key")
        value = _unquote(match.group("value"))
        if key == "hooks" and not value:
            in_hooks = True
            hooks_indent = indent
            event = None
            continue
        if key == "hooks_auto_accept":
            auto_accept = value.strip().lower() in ("true", "yes", "1")
            if in_hooks:
                flush()
                in_hooks = False
                event = None
            continue
        if in_hooks and indent <= hooks_indent:
            # Любой ключ уровня блока `hooks` или выше закрывает разбор хуков.
            flush()
            in_hooks = False
            event = None
        if in_hooks and indent == hooks_indent + 2 and not value:
            if event is not None:
                flush()
            event = key
            event_indent = indent
            command = ""
            timeout = None
            continue
        if in_hooks and event is not None and key == "timeout":
            timeout = _as_int(value)
    flush()
    return hooks, auto_accept


def read_hermes_hooks(config_path: Path | None = None) -> HermesHooks:
    """Читает `config.yaml` Hermes; отсутствие файла — не исключение."""

    path = Path(config_path) if config_path is not None else hermes_home() / "config.yaml"
    hooks = HermesHooks(path=path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        hooks.error = "файл не найден"
        return hooks
    except OSError as exc:
        hooks.error = type(exc).__name__
        return hooks
    hooks.exists = True
    try:
        hooks.hooks, hooks.auto_accept = parse_hooks_yaml(text)
    except Exception as exc:  # noqa: BLE001 - разбор не должен ронять doctor
        hooks.error = type(exc).__name__
    return hooks



# --- сверка таймаутов и суббюджетов -----------------------------------------


def effective_deadline_ms(
    internal_timeout_s: float,
    hermes_timeout_s: int | None,
    margin_ms: int,
) -> int:
    """`hook_deadline_ms = min(таймаут Hermes, внутренний предел) − запас` (§19.1.1)."""

    limit = float(internal_timeout_s)
    if hermes_timeout_s is not None:
        limit = min(limit, float(hermes_timeout_s))
    return int(limit * 1000) - int(margin_ms)


def check_timeouts(config: Config, hermes: HermesHooks, report: DoctorReport) -> None:
    """Сверяет таймауты хуков Hermes с конфигом MiniMem (§21.1, §19.1.1)."""

    margin = int(config["hook_safety_margin_ms"])
    for event, internal_hook in HERMES_EVENTS.items():
        hermes_timeout = hermes.timeout_s(event)
        internal_timeout = float(config[f"{internal_hook}_timeout"])
        deadline = effective_deadline_ms(internal_timeout, hermes_timeout, margin)
        config_deadline = config.hook_deadline_ms(internal_hook)
        line = (
            f"{event}: Hermes {hermes_timeout if hermes_timeout is not None else '—'} с, "
            f"MiniMem {int(internal_timeout)} с, hook_deadline_ms {deadline} мс"
        )
        if hermes_timeout is None:
            report.add(
                CODE_CONFIG_INVALID,
                f"{event}: в config.yaml Hermes нет записи с пределом; действует "
                f"внутренний предел {int(internal_timeout)} с",
            )
            line += ", запись Hermes не найдена"
        elif hermes_timeout != int(internal_timeout):
            report.add(
                CODE_CONFIG_INVALID,
                f"{event}: предел Hermes {hermes_timeout} с не совпадает с внутренним "
                f"{int(internal_timeout)} с; hook_deadline_ms пересчитан по меньшему",
            )
            line += ", расхождение с внутренним пределом"
        if deadline != config_deadline:
            report.add(
                CODE_CONFIG_INVALID,
                f"{event}: пересчитанный hook_deadline_ms {deadline} мс не совпадает "
                f"со значением конфигурации {config_deadline} мс",
            )
            line += f" (в конфиге {config_deadline} мс)"
        report.hook_lines.append(line)


def check_budgets(config: Config, report: DoctorReport) -> None:
    """Проверяет сумму суббюджетов и `return_min_budget_ms` (§19.1.1, §21.1)."""

    values: Mapping[str, Any] = config.as_dict()
    pre_deadline = config.hook_deadline_ms("pre_llm")
    post_deadline = config.hook_deadline_ms("post_llm")
    margin = int(config["hook_safety_margin_ms"])

    pre_parts = {
        "catch-up": catch_up_budget_ms(values),
        "дайджест при сжатии": digest_budget_ms(values),
        "вставка памяти": int(config["return_min_budget_ms"]),
    }
    post_parts = {
        "захват": int(post_deadline * CATCH_UP_SHARE),
        "индексатор": int(post_deadline * CATCH_UP_SHARE),
    }
    pre_total = sum(pre_parts.values())
    post_total = sum(post_parts.values())

    report.hook_lines.append(
        "pre_llm_call: "
        + ", ".join(f"{name} {value} мс" for name, value in pre_parts.items())
        + f"; сумма {pre_total} мс при hook_deadline_ms {pre_deadline} мс, запас {margin} мс"
    )
    report.hook_lines.append(
        "post_llm_call: "
        + ", ".join(f"{name} {value} мс" for name, value in post_parts.items())
        + f"; сумма {post_total} мс при hook_deadline_ms {post_deadline} мс"
    )

    if pre_total > pre_deadline:
        report.add(
            CODE_BUDGET_MISMATCH,
            f"сумма суббюджетов pre_llm_call {pre_total} мс больше hook_deadline_ms "
            f"{pre_deadline} мс: "
            + ", ".join(f"{name} {value} мс" for name, value in pre_parts.items()),
        )
    if post_total > post_deadline:
        report.add(
            CODE_BUDGET_MISMATCH,
            f"сумма суббюджетов post_llm_call {post_total} мс больше hook_deadline_ms "
            f"{post_deadline} мс: "
            + ", ".join(f"{name} {value} мс" for name, value in post_parts.items()),
        )
    if float(config["catch_up_budget_seconds"]) * 1000 >= pre_deadline:
        report.add(
            CODE_BUDGET_MISMATCH,
            f"catch_up_budget_seconds ({config['catch_up_budget_seconds']} с) не меньше "
            f"hook_deadline_ms pre_llm_call ({pre_deadline} мс)",
        )


def check_config_invariants(config: Config, report: DoctorReport) -> None:
    """Инварианты §20.1 и §20 п.3 — они уже посчитаны при загрузке конфигурации."""

    if config.issues:
        for issue in config.issues:
            report.add(CODE_CONFIG_INVALID, issue)
    else:
        report.notes.append("конфигурация: инварианты §20 и §20.1 выполнены")


# --- проверка резервной копии журнала (П-19) ---------------------------------


def local_drive_roots() -> list[Path]:
    """Корни доступных локальных дисков: установка копирования на отдельном томе.

    Проверяется только существование корня, обход каталогов не выполняется.
    Пустой список на системах без локальных дисков в этом смысле.
    """

    roots: list[Path] = []
    if os.name != "nt":
        anchor = Path(Path.cwd().anchor or "/")
        return [anchor] if anchor.exists() else []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        candidate = Path(f"{letter}:\\")
        try:
            if not candidate.is_dir():
                continue
        except OSError:
            continue
        roots.append(candidate)
    return roots


def backup_settings_candidates(
    memory_root: Path,
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    drive_roots: Sequence[Path] | None = None,
) -> list[Path]:
    """Перечисляет все места, где программа ищет `backup.json` (П-19, §21.1).

    Порядок — только приоритет при равной дате файла:

    1. `MINIMEM_BACKUP_SETTINGS` — явное указание владельца;
    2. каталог `minimem-backup` рядом с конфигурацией MiniMem и сам каталог
       конфигурации (установка из репозитория);
    3. каталог `minimem-backup` внутри `memory_root`, рядом с ним и в его
       родителе (установка рядом с хранилищем, раздел 3 документа
       «Установка, бэкап и восстановление»);
    4. `%LOCALAPPDATA%`, `%APPDATA%`, `%PROGRAMDATA%` и домашний каталог —
       каждый с подкаталогом `minimem-backup` и без него;
    5. корень каждого локального диска с подкаталогом `minimem-backup` —
       установка копирования на отдельном томе (на этой машине
       `D:\\minimem-backup`).
    """

    environ = os.environ if env is None else env
    roots: list[Path] = []
    if config_dir is not None:
        roots.extend((config_dir / BACKUP_DIRNAME, config_dir))
    roots.extend(
        (
            memory_root / BACKUP_DIRNAME,
            memory_root,
            memory_root.parent,
        )
    )

    candidates: list[Path] = []
    seen: set[str] = set()
    override = (environ.get("MINIMEM_BACKUP_SETTINGS") or "").strip()
    if override:
        candidates.append(Path(os.path.expandvars(override)).expanduser())
        seen.add(os.path.normcase(str(candidates[-1])))

    for name in ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA"):
        value = (environ.get(name) or "").strip()
        if not value:
            continue
        roots.append(Path(value))
        roots.append(Path(value) / BACKUP_DIRNAME)
    home = (environ.get("USERPROFILE") or environ.get("HOME") or "").strip()
    if home:
        roots.append(Path(home))
        roots.append(Path(home) / BACKUP_DIRNAME)

    for root in local_drive_roots() if drive_roots is None else drive_roots:
        roots.append(Path(root) / BACKUP_DIRNAME)

    for root in roots:
        candidate = root / BACKUP_SETTINGS_FILENAME
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def locate_backup_settings(
    memory_root: Path,
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    drive_roots: Sequence[Path] | None = None,
) -> Path | None:
    """Ищет `backup.json` во всех возможных для программы местах (§21.1).

    Из найденных выбирается самый свежий по дате изменения файла; при равной
    дате — первый по приоритету (см. `backup_settings_candidates`).
    Ничего не найдено — проверка пропускается, это не ошибка `doctor`.
    """

    environ = os.environ if env is None else env
    found: list[tuple[float, int, Path]] = []
    for index, candidate in enumerate(
        backup_settings_candidates(memory_root, config_dir, env=environ, drive_roots=drive_roots)
    ):
        try:
            if not candidate.is_file():
                continue
            stamp = candidate.stat().st_mtime
        except OSError:
            continue
        found.append((stamp, -index, candidate))
    if not found:
        return None
    return max(found, key=lambda item: (item[0], item[1]))[2]


def read_backup_settings(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Читает эксплуатационный `backup.json`; возврат `(данные, ошибка)`."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    if not isinstance(raw, dict):
        return None, "ожидался объект JSON"
    return raw, ""


def latest_snapshot(backup_root: Path) -> tuple[date, Path] | None:
    """Последний каталог слепка вида `<YYYY-MM-DD>` (ТЗ v1.7 §21.1)."""

    if not backup_root.is_dir():
        return None
    found: list[tuple[date, Path]] = []
    for child in backup_root.iterdir():
        if child.is_dir() and _SNAPSHOT_NAME.match(child.name):
            try:
                found.append((date.fromisoformat(child.name), child))
            except ValueError:
                continue
    if not found:
        return None
    return max(found, key=lambda item: item[0])


def snapshot_content(snapshot: Path) -> tuple[bool, bool]:
    """Есть ли в слепке журнал и копия `config.json` (прерванное копирование)."""

    journal = snapshot / "journal"
    has_journal = journal.is_dir() and any(path.is_file() for path in journal.rglob("*"))
    has_config = (snapshot / "minimem" / "config.json").is_file()
    return has_journal, has_config


def check_backup(
    memory_root: Path,
    settings_path: Path | None = None,
    today: date | None = None,
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    drive_roots: Sequence[Path] | None = None,
) -> BackupReport:
    """Проверяет наличие и свежесть последней резервной копии журнала (П-19).

    Файл настроек копирования ищется во всех возможных для программы местах,
    при нескольких найденных выбирается самый свежий по дате (§21.1).
    Отсутствие файла настроек пропускает проверку с пометкой, ошибкой это не
    считается. Копия непригодна, если она старше порога владельца
    либо если в слепке нет ни журнала, ни копии `config.json`.
    """

    report = BackupReport()
    if settings_path is None:
        report.settings_found = [
            candidate
            for candidate in backup_settings_candidates(
                memory_root, config_dir, env=env, drive_roots=drive_roots
            )
            if candidate.is_file()
        ]
        settings_path = locate_backup_settings(
            memory_root, config_dir, env=env, drive_roots=drive_roots
        )
    else:
        report.settings_found = [Path(settings_path)]
    if settings_path is None or not Path(settings_path).is_file():
        # Отсутствие файла настроек — не ошибка: копирование вне области MiniMem.
        report.settings_path = Path(settings_path) if settings_path else None
        report.decision = BACKUP_SKIPPED
        report.settings_note = (
            "файл настроек резервного копирования не найден: копирование находится "
            "вне области MiniMem, проверка пропущена"
        )
        report.detail = report.settings_note
        return report

    report.settings_path = Path(settings_path)
    settings, error = read_backup_settings(report.settings_path)
    if settings is None:
        report.decision = BACKUP_MISSING
        report.settings_note = f"настройки копирования нечитаемы: {error}"
        report.detail = f"настройки копирования нечитаемы: {error}"
        return report

    raw_root = str(settings.get("backup_root") or "").strip()
    if not raw_root:
        report.decision = BACKUP_SKIPPED
        report.settings_note = "в настройках копирования не задан backup_root"
        report.detail = (
            "в настройках копирования не задан backup_root: проверка пропущена"
        )
        return report
    report.backup_root = Path(os.path.expandvars(raw_root)).expanduser()

    raw_threshold = settings.get("backup_stale_days")
    if isinstance(raw_threshold, (int, float)) and not isinstance(raw_threshold, bool):
        report.threshold_days = int(raw_threshold)
        report.threshold_source = "backup_stale_days"
    else:
        report.threshold_days = DEFAULT_BACKUP_STALE_DAYS
        report.threshold_source = (
            f"по умолчанию {DEFAULT_BACKUP_STALE_DAYS} сут, ключ backup_stale_days "
            "в backup.json не задан"
        )

    found = latest_snapshot(report.backup_root)
    if found is None:
        report.decision = BACKUP_MISSING
        report.detail = f"каталогов копии вида <YYYY-MM-DD> в {report.backup_root} нет"
        return report

    snapshot_date, snapshot_path = found
    report.snapshot_date = snapshot_date
    report.snapshot_path = snapshot_path
    report.age_days = max(0, ((today or date.today()) - snapshot_date).days)
    report.has_journal, report.has_config = snapshot_content(snapshot_path)

    if not report.has_journal and not report.has_config:
        report.decision = BACKUP_STALE
        report.detail = "слепок прерван: в нём нет ни журнала, ни копии config.json"
        return report
    if report.age_days > report.threshold_days:
        report.decision = BACKUP_STALE
        missing = "в слепке нет журнала" if not report.has_journal else "в слепке нет config.json"
        report.detail = f"возраст {report.age_days} сут больше порога; {missing}"
        return report
    report.decision = BACKUP_OK
    report.detail = "слепок полный"
    return report


# --- итоговая проверка -------------------------------------------------------


def run_doctor(
    config: Config,
    memory_root: Path,
    hermes_config: Path | None = None,
    backup_settings: Path | None = None,
    today: date | None = None,
    env: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Выполняет все проверки `doctor` и возвращает отчёт (ТЗ v1.7 §21.1)."""

    report = DoctorReport()
    hermes = read_hermes_hooks(hermes_config)
    if hermes.error:
        report.notes.append(f"config.yaml Hermes: {hermes.error} ({hermes.path})")
    check_timeouts(config, hermes, report)
    check_budgets(config, report)
    check_config_invariants(config, report)
    config_path = getattr(config, "path", None)
    report.backup = check_backup(
        memory_root,
        backup_settings,
        today,
        config_dir=Path(config_path).parent if config_path else None,
        env=env,
    )
    if not report.backup.ok:
        report.add(CODE_BACKUP_STALE, report.backup.line())
    return report

