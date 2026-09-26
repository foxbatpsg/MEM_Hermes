"""Тесты этапа 8: установка и приёмка.

Основание: ТЗ v1.7 §21.1 (`minimem doctor`, П-06 и П-19), §21.3
(`minimem stats`, П-10), §19.1 и §19.1.1 (таймауты и суббюджеты), §20 и
§20.1 (инварианты), §22 (логирование), §25 (критерии 28 и 29), §26.1 и
§26.4 (регистрация хуков и порядок включения); план реализации v1.7, этап 8.

Номера тестов: MM-140, MM-141, MM-142 (П-10), MM-165, MM-166, MM-167,
MM-168 (П-19), плюс инфраструктурные проверки `doctor` и тест 47 (установка).

Тест 47 запускает настоящий Hermes и по умолчанию пропускается: включается
только по команде владельца переменной `MINIMEM_RUN_HERMES_TESTS=1`.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import doctor, stats  # noqa: E402
from mm.config import DEFAULTS, Config, validate  # noqa: E402

MINIMEM_PY = CODE_DIR / "minimem.py"
TODAY = date(2026, 9, 26)

HOOKS_YAML = """\
cwd: .
agent:
  home_mode: auto
hooks:
  pre_llm_call:
    - command: C:\\Python\\python.exe C:\\hermes\\agent-hooks\\wiki_hook.py
      timeout: 15
  post_llm_call:
    - command: C:\\Python\\python.exe C:\\MiniMem\\hooks\\post_llm.py
      timeout: 30
  on_session_end:
    - command: C:\\Python\\python.exe C:\\MiniMem\\hooks\\session_end.py
      timeout: 30
hooks_auto_accept: false
code_execution:
  timeout: 300
  max_tool_calls: 50
"""

#: Тот же файл без `on_session_end`: запись хука ещё не зарегистрирована.
HOOKS_YAML_WITHOUT_SESSION_END = HOOKS_YAML.replace(
    "  on_session_end:\n"
    "    - command: C:\\Python\\python.exe C:\\MiniMem\\hooks\\session_end.py\n"
    "      timeout: 30\n",
    "",
)


def make_config(tmp: str, **overrides) -> Config:
    """Конфигурация MiniMem поверх DEFAULTS с временным `memory_root`."""

    values = dict(DEFAULTS)
    values["memory_root"] = tmp
    values.update(overrides)
    return Config(values)


class HermesConfigTests(unittest.TestCase):
    """Разбор `config.yaml` Hermes: блок `hooks` и `hooks_auto_accept` (§26.1)."""

    def test_parses_hooks_and_auto_accept(self) -> None:
        hooks, auto_accept = doctor.parse_hooks_yaml(HOOKS_YAML)
        self.assertIs(auto_accept, False)
        self.assertEqual(len(hooks), 3)
        self.assertEqual(hooks[0].event, "pre_llm_call")
        self.assertEqual(hooks[0].timeout_s, 15)
        self.assertIn("wiki_hook.py", hooks[0].command)
        self.assertEqual(hooks[1].event, "post_llm_call")
        self.assertEqual(hooks[1].timeout_s, 30)
        self.assertEqual(hooks[2].event, "on_session_end")
        self.assertEqual(hooks[2].timeout_s, 30)

    def test_minimem_commands_are_detected(self) -> None:
        """`doctor` узнаёт, какие записи хуков принадлежат MiniMem."""

        hooks, _ = doctor.parse_hooks_yaml(HOOKS_YAML)
        commands = [hook.command for hook in hooks]
        self.assertIn("C:\\Python\\python.exe C:\\MiniMem\\hooks\\post_llm.py", commands)
        self.assertIn(
            "C:\\Python\\python.exe C:\\MiniMem\\hooks\\session_end.py", commands
        )
        self.assertNotIn(
            "C:\\Python\\python.exe C:\\hermes\\agent-hooks\\wiki_hook.py",
            doctor.HermesHooks(path=Path("config.yaml"), hooks=hooks)
            .minimem_commands("MiniMem\\hooks"),
        )

    def test_timeout_from_other_sections_is_not_hooks(self) -> None:
        """`code_execution.timeout = 300` не должен попадать в хуки."""

        hooks, _ = doctor.parse_hooks_yaml(HOOKS_YAML)
        self.assertNotIn("code_execution", [hook.event for hook in hooks])
        self.assertTrue(all(hook.timeout_s != 300 for hook in hooks))

    def test_missing_file_is_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = doctor.read_hermes_hooks(Path(tmp) / "config.yaml")
        self.assertFalse(hooks.exists)
        self.assertEqual(hooks.error, "файл не найден")
        self.assertEqual(hooks.hooks, [])


class DeadlineTests(unittest.TestCase):
    """Пересчёт `hook_deadline_ms` и сумма суббюджетов (§19.1.1, §21.1)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_deadline_is_min_of_hermes_and_internal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(HOOKS_YAML, encoding="utf-8")
            hermes = doctor.read_hermes_hooks(config_path)
        config = make_config(self.tmp)
        report = doctor.DoctorReport()
        doctor.check_timeouts(config, hermes, report)
        pre = doctor.effective_deadline_ms(15, 15, 2000)
        post = doctor.effective_deadline_ms(30, 30, 2000)
        self.assertEqual(pre, 13000)
        self.assertEqual(post, 28000)
        self.assertIn("13000", " ".join(report.hook_lines))
        self.assertEqual(report.findings, [])

    def test_missing_hook_registration_is_reported(self) -> None:
        """Нет записи `on_session_end` — это расхождение `config_invalid`."""

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(
                HOOKS_YAML_WITHOUT_SESSION_END, encoding="utf-8"
            )
            hermes = doctor.read_hermes_hooks(config_path)
        config = make_config(self.tmp)
        report = doctor.DoctorReport()
        doctor.check_timeouts(config, hermes, report)
        self.assertEqual(
            [finding.code for finding in report.findings],
            [doctor.CODE_CONFIG_INVALID],
        )
        self.assertIn("on_session_end", report.findings[0].message)

    def test_smaller_hermes_limit_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text(HOOKS_YAML, encoding="utf-8")
            hermes = doctor.read_hermes_hooks(config_path)
        self.assertEqual(hermes.timeout_s("post_llm_call"), 30)
        self.assertEqual(
            doctor.effective_deadline_ms(30, 20, 2000), 18000
        )

    def test_budget_mismatch_is_reported(self) -> None:
        """Сумма суббюджетов больше дедлайна даёт `budget_mismatch`."""

        config = make_config(self.tmp, hook_safety_margin_ms=20000)
        report = doctor.DoctorReport()
        doctor.check_budgets(config, report)
        codes = {finding.code for finding in report.findings}
        self.assertIn(doctor.CODE_BUDGET_MISMATCH, codes)

    def test_shipped_budgets_fit_deadlines(self) -> None:
        config = make_config(self.tmp)
        report = doctor.DoctorReport()
        doctor.check_budgets(config, report)
        self.assertEqual(report.findings, [])

    def test_config_invariants_are_reported_as_config_invalid(self) -> None:
        values = dict(DEFAULTS)
        values["memory_root"] = self.tmp
        values["digest_turns"] = 0
        issues = validate(values)
        self.assertTrue(issues)
        report = doctor.DoctorReport()
        doctor.check_config_invariants(Config(values, issues=tuple(issues)), report)
        self.assertTrue(report.findings)
        self.assertTrue(
            all(f.code == doctor.CODE_CONFIG_INVALID for f in report.findings)
        )

    def test_valid_config_has_no_findings(self) -> None:
        config = make_config(self.tmp)
        report = doctor.DoctorReport()
        doctor.check_config_invariants(config, report)
        self.assertEqual(report.findings, [])
        self.assertTrue(report.notes)


class BackupSettingsCase(unittest.TestCase):
    """Общая подготовка: `backup.json`, слепки и `memory_root` во временных каталогах."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.memory_root = self.tmp / "memory"
        self.memory_root.mkdir(parents=True)
        self.backup_root = self.tmp / "backup"
        self.backup_root.mkdir(parents=True)
        self.settings = self.tmp / "backup.json"
        self.write_settings()

    def write_settings(self, **extra) -> None:
        payload = {
            "backup_root": str(self.backup_root),
            "minimem_dir": str(CODE_DIR),
            "keep_daily": 30,
            "keep_monthly": 12,
        }
        payload.update(extra)
        self.settings.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def make_snapshot(
        self, day: date, journal: bool = True, config: bool = True
    ) -> Path:
        """Слепок `<YYYY-MM-DD>` составом раздела 3 документа об эксплуатации."""

        snapshot = self.backup_root / day.strftime("%Y-%m-%d")
        if journal:
            journal_dir = snapshot / "journal" / "common" / "memory"
            journal_dir.mkdir(parents=True)
            (journal_dir / f"{day.strftime('%Y-%m-%d')}.md").write_text(
                "запись\n", encoding="utf-8"
            )
        if config:
            minimem_dir = snapshot / "minimem"
            minimem_dir.mkdir(parents=True, exist_ok=True)
            (minimem_dir / "config.json").write_text("{}", encoding="utf-8")
        (snapshot / "hermes").mkdir(parents=True, exist_ok=True)
        return snapshot

    def write_hermes_config(self) -> Path:
        path = self.tmp / "hermes_config.yaml"
        path.write_text(HOOKS_YAML, encoding="utf-8")
        return path

    def run_doctor_on(self, today: date = TODAY) -> doctor.DoctorReport:
        config = make_config(str(self.memory_root))
        return doctor.run_doctor(
            config,
            self.memory_root,
            hermes_config=self.write_hermes_config(),
            backup_settings=self.settings,
            today=today,
        )


class BackupDoctorTests(BackupSettingsCase):
    """П-19: контроль резервной копии журнала в `doctor` (MM-165…MM-168)."""

    def test_fresh_backup_reports_no_mismatch(self) -> None:
        """MM-165. Свежая копия не даёт расхождения по бэкапу."""

        self.make_snapshot(TODAY)
        report = self.run_doctor_on()
        self.assertEqual(report.backup.decision, doctor.BACKUP_OK)
        self.assertEqual(report.backup.age_days, 0)
        self.assertNotIn(
            doctor.CODE_BACKUP_STALE, {finding.code for finding in report.findings}
        )

    def test_stale_backup_reports_date_path_and_age(self) -> None:
        """MM-166. Копия старше порога: backup_stale с датой, путём и возрастом."""

        old = TODAY - timedelta(days=5)
        snapshot = self.make_snapshot(old)
        report = doctor.check_backup(self.memory_root, self.settings, today=TODAY)
        self.assertEqual(report.decision, doctor.BACKUP_STALE)
        self.assertEqual(report.snapshot_date, old)
        self.assertEqual(report.snapshot_path, snapshot)
        self.assertEqual(report.age_days, 5)
        line = report.line()
        self.assertIn(doctor.CODE_BACKUP_STALE, line)
        self.assertIn(str(snapshot), line)
        self.assertIn("5 сут", line)

    def test_threshold_is_read_from_backup_json(self) -> None:
        """Порог принадлежит эксплуатации: `backup_stale_days` в `backup.json`."""

        self.write_settings(backup_stale_days=1)
        self.make_snapshot(TODAY - timedelta(days=3))
        report = doctor.check_backup(self.memory_root, self.settings, today=TODAY)
        self.assertEqual(report.threshold_days, 1)
        self.assertEqual(report.threshold_source, "backup_stale_days")
        self.assertEqual(report.decision, doctor.BACKUP_STALE)

    def test_default_threshold_when_key_absent(self) -> None:
        """Без ключа берётся порог по умолчанию, и это видно в отчёте."""

        self.make_snapshot(TODAY - timedelta(days=2))
        report = doctor.check_backup(self.memory_root, self.settings, today=TODAY)
        self.assertEqual(report.threshold_days, doctor.DEFAULT_BACKUP_STALE_DAYS)
        self.assertIn("backup_stale_days", report.threshold_source)
        self.assertEqual(report.decision, doctor.BACKUP_OK)

    def test_no_snapshots_reports_backup_stale(self) -> None:
        """MM-167. Каталогов копии нет: backup_stale, а не ошибка конфигурации."""

        report = self.run_doctor_on()
        self.assertEqual(report.backup.decision, doctor.BACKUP_MISSING)
        codes = {finding.code for finding in report.findings}
        self.assertIn(doctor.CODE_BACKUP_STALE, codes)
        self.assertNotIn(doctor.CODE_CONFIG_INVALID, codes)

    def test_incomplete_snapshot_reports_backup_stale(self) -> None:
        """MM-168. Прерванный слепок: нет ни журнала, ни config.json."""

        self.make_snapshot(TODAY, journal=False, config=False)
        report = doctor.check_backup(self.memory_root, self.settings, today=TODAY)
        self.assertEqual(report.decision, doctor.BACKUP_STALE)
        self.assertFalse(report.has_journal)
        self.assertFalse(report.has_config)

    def test_missing_settings_file_skips_check(self) -> None:
        """MM-168. Файла настроек нет: проверка пропускается, ошибкой не считается."""

        config = make_config(str(self.memory_root))
        report = doctor.run_doctor(
            config,
            self.memory_root,
            hermes_config=self.write_hermes_config(),
            backup_settings=self.tmp / "нет-каталога" / "backup.json",
            today=TODAY,
        )
        self.assertEqual(report.backup.decision, doctor.BACKUP_SKIPPED)
        self.assertTrue(report.backup.ok)
        self.assertIn("вне области MiniMem", report.backup.detail)
        self.assertNotIn(doctor.CODE_BACKUP_STALE, {f.code for f in report.findings})

    def test_snapshot_with_only_config_is_accepted(self) -> None:
        """Есть config.json — слепок не прерван, даже если журнала нет."""

        self.make_snapshot(TODAY, journal=False, config=True)
        report = doctor.check_backup(self.memory_root, self.settings, today=TODAY)
        self.assertEqual(report.decision, doctor.BACKUP_OK)

    def test_doctor_does_not_modify_operational_files(self) -> None:
        """§21.1: doctor только читает — слепок, настройки и config.yaml не меняются."""

        snapshot = self.make_snapshot(TODAY)
        hermes_config = self.write_hermes_config()

        def fingerprint() -> tuple:
            return (
                self.settings.read_bytes(),
                hermes_config.read_bytes(),
                tuple(
                    sorted(
                        (path.relative_to(snapshot).as_posix(), path.stat().st_size)
                        for path in snapshot.rglob("*")
                    )
                ),
            )

        before = fingerprint()
        self.run_doctor_on()
        self.assertEqual(before, fingerprint())

    def test_doctor_works_without_sqlite(self) -> None:
        """§21.1: проверка копии не зависит от индекса."""

        self.make_snapshot(TODAY)
        report = self.run_doctor_on()
        self.assertFalse((self.memory_root / "minimem.db").exists())
        self.assertEqual(report.backup.decision, doctor.BACKUP_OK)

    def test_latest_snapshot_by_name(self) -> None:
        """Берётся последний каталог вида <YYYY-MM-DD>, а не последний по mtime."""

        self.make_snapshot(TODAY - timedelta(days=1))
        newest = self.make_snapshot(TODAY)
        self.make_snapshot(TODAY - timedelta(days=9))
        found = doctor.latest_snapshot(self.backup_root)
        self.assertEqual(found, (TODAY, newest))


class BackupSettingsSearchTests(unittest.TestCase):
    """Поиск `backup.json` во всех возможных для программы местах (§21.1).

    Проверка идёт на временных каталогах: реальное окружение подменяется
    пустым, чтобы поиск не выходил за пределы теста.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.memory_root = self.tmp / "memory"
        self.memory_root.mkdir(parents=True)
        self.config_dir = self.tmp / "code" / "minimem"
        self.config_dir.mkdir(parents=True)
        self.backup_root = self.tmp / "backup"
        self.backup_root.mkdir()

    def write_settings(self, path: Path, day: date = TODAY) -> Path:
        """`backup.json` с заданной датой изменения файла."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "backup_root": str(self.backup_root),
                    "minimem_dir": str(CODE_DIR),
                    "backup_stale_days": 2,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        stamp = datetime.datetime(day.year, day.month, day.day, 12, 0).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def make_snapshot(self, day: date = TODAY) -> Path:
        snapshot = self.backup_root / day.strftime("%Y-%m-%d")
        (snapshot / "journal" / "common" / "memory").mkdir(parents=True)
        (snapshot / "journal" / "common" / "memory" / f"{day:%Y-%m-%d}.md").write_text(
            "запись\n", encoding="utf-8"
        )
        (snapshot / "minimem").mkdir(parents=True)
        (snapshot / "minimem" / "config.json").write_text("{}", encoding="utf-8")
        return snapshot

    def test_candidates_cover_every_program_location(self) -> None:
        """В перечень входят переменная окружения, каталоги программы и хранилища."""

        override = self.tmp / "override.json"
        env = {
            "MINIMEM_BACKUP_SETTINGS": str(override),
            "LOCALAPPDATA": str(self.tmp / "localappdata"),
            "USERPROFILE": str(self.tmp / "home"),
        }
        candidates = doctor.backup_settings_candidates(
            self.memory_root, self.config_dir, env=env, drive_roots=[]
        )
        names = [str(path) for path in candidates]
        self.assertEqual(names[0], str(override))
        for expected in (
            self.config_dir / "minimem-backup" / "backup.json",
            self.config_dir / "backup.json",
            self.memory_root / "minimem-backup" / "backup.json",
            self.memory_root / "backup.json",
            self.memory_root.parent / "backup.json",
            self.tmp / "localappdata" / "minimem-backup" / "backup.json",
            self.tmp / "home" / "minimem-backup" / "backup.json",
        ):
            self.assertIn(str(expected), names)

    def test_candidates_without_duplicates(self) -> None:
        """Совпадающие пути перечисляются один раз."""

        candidates = doctor.backup_settings_candidates(
            self.memory_root, self.memory_root.parent, env={}, drive_roots=[]
        )
        keys = {os.path.normcase(str(path)) for path in candidates}
        self.assertEqual(len(keys), len(candidates))

    def test_newest_settings_file_wins(self) -> None:
        """Из нескольких найденных выбирается самый свежий по дате файла."""

        self.write_settings(self.memory_root / "backup.json", TODAY - timedelta(days=3))
        newest = self.write_settings(
            self.config_dir / "minimem-backup" / "backup.json", TODAY
        )
        self.assertEqual(self.locate(), newest)

    def test_environment_override_competes_by_date(self) -> None:
        """`MINIMEM_BACKUP_SETTINGS` — кандидат, а не безусловный приоритет."""

        override = self.write_settings(
            self.tmp / "override.json", TODAY - timedelta(days=5)
        )
        in_code_dir = self.write_settings(
            self.config_dir / "minimem-backup" / "backup.json", TODAY - timedelta(days=1)
        )
        env = {"MINIMEM_BACKUP_SETTINGS": str(override)}
        self.assertEqual(self.locate(env), in_code_dir)

    def drive_roots(self) -> list[Path]:
        """Изолированные «диски» теста: настоящие локальные диски не обходятся."""

        return [self.tmp / "disk"]

    def locate(self, env: dict[str, str] | None = None) -> Path | None:
        return doctor.locate_backup_settings(
            self.memory_root,
            self.config_dir,
            env=env if env is not None else {},
            drive_roots=self.drive_roots(),
        )

    def check(self) -> doctor.BackupReport:
        return doctor.check_backup(
            self.memory_root,
            None,
            today=TODAY,
            config_dir=self.config_dir,
            env={},
            drive_roots=self.drive_roots(),
        )

    def test_settings_on_separate_drive_are_found(self) -> None:
        """Копирование на отдельном томе: `<том>\\minimem-backup\\backup.json`."""

        on_drive = self.write_settings(
            self.tmp / "disk" / "minimem-backup" / "backup.json", TODAY
        )
        self.assertEqual(self.locate(), on_drive)

    def test_local_drive_roots_are_existing_only(self) -> None:
        """В перечень дисков попадают только существующие корни."""

        roots = doctor.local_drive_roots()
        self.assertTrue(roots)
        for root in roots:
            self.assertTrue(root.is_dir())
            drive, tail = os.path.splitdrive(str(root))
            self.assertTrue(drive)
            self.assertEqual(tail, "\\" if os.name == "nt" else root.anchor)

    def test_equal_dates_fall_back_to_priority(self) -> None:
        """При равной дате побеждает более приоритетное место."""

        override = self.write_settings(self.tmp / "override.json", TODAY)
        self.write_settings(self.config_dir / "backup.json", TODAY)
        env = {"MINIMEM_BACKUP_SETTINGS": str(override)}
        self.assertEqual(self.locate(env), override)

    def test_nothing_found_returns_none(self) -> None:
        """Ни одного файла настроек — проверка пропускается."""

        self.assertIsNone(self.locate())
        report = self.check()
        self.assertEqual(report.decision, doctor.BACKUP_SKIPPED)
        self.assertTrue(report.ok)

    def test_report_names_the_chosen_file(self) -> None:
        """В строке отчёта видно, какой файл выбран и сколько найдено."""

        self.write_settings(self.memory_root / "backup.json", TODAY - timedelta(days=3))
        chosen = self.write_settings(
            self.config_dir / "minimem-backup" / "backup.json", TODAY
        )
        self.make_snapshot(TODAY)
        report = self.check()
        self.assertEqual(report.decision, doctor.BACKUP_OK)
        self.assertEqual(report.settings_path, chosen)
        line = report.line()
        self.assertIn(str(chosen), line)
        self.assertIn("более свежий из 2 найденных", line)



def log_entry(operation: str, **fields) -> dict:
    """Запись лога с меткой времени текущих суток."""

    record = {
        "timestamp": "2026-09-26T07:15:42+00:00",
        "module": "test",
        "operation": operation,
        "status": "ok",
    }
    record.update(fields)
    return record


def search_operations_in_source() -> set[str]:
    """Операции из реальных вызовов `log_search` в `mm/search.py`."""

    import re

    source = (CODE_DIR / "mm" / "search.py").read_text(encoding="utf-8")
    return set(re.findall(r"log_search\(\s*logger,\s*\"([a-z_]+)\"", source))


def capture_operations_in_source() -> set[str]:
    """Операции Захвата: вызовы `logger.log("capture", ...)` и значения операции."""

    import re

    source = (CODE_DIR / "mm" / "capture.py").read_text(encoding="utf-8")
    called = re.findall(
        r"logger\.(?:log|error)\(\s*\n?\s*\"capture\",\s*\n?\s*\"([a-z_]+)\"", source
    )
    assigned = re.findall(r"(?:operation: str|operation) = \"(capture_[a-z_]+)\"", source)
    return set(called) | set(assigned)


def synthetic_log() -> list[dict]:
    """Синтетический лог со всеми группами метрик нормативного списка §21.3."""

    return [
        log_entry(
            "search_completed",
            project="common",
            duration_ms=120,
            search_terms=4,
            search_hits=3,
            search_returned=2,
            returned_event_ids=["e_1", "e_2"],
            top5_scores=[3.2, 1.8, 1.1],
            threshold_applied="absolute",
        ),
        log_entry(
            "search_completed",
            project="common",
            duration_ms=340,
            search_terms=6,
            search_hits=0,
            search_returned=0,
            returned_event_ids=[],
            top5_scores=[],
        ),
        log_entry(
            "search_completed",
            project="common",
            duration_ms=200,
            status="error",
            error_code="search_timeout",
            search_terms=5,
            search_hits=2,
            search_returned=0,
        ),
        log_entry(
            "capture_record_written",
            project="common",
            duration_ms=40,
            turns_seen=1,
            turns_captured=1,
            turns_skipped=0,
            turns_derived=0,
            redaction_count=1,
        ),
        log_entry("record_truncated_to_limit", project="common", truncation_fields="user"),
        log_entry(
            "record_body_withheld_redaction_failed",
            project="common",
            error_code="redaction_failed",
        ),
        log_entry("capture_duplicate_ignored", project="common"),
        log_entry("cursor_reset", project="common"),
        log_entry("journal_record_damaged", project="common"),
        log_entry("incremental_index", project="common", records_indexed=5, duration_ms=90),
        log_entry("catch_up", project="common", duration_ms=2000),
        log_entry("catch_up_timeout", project="common"),
        log_entry("digest_created", project="common", digest_chars=1200),
        log_entry("digest_created_field_fallback", project="common"),
        log_entry("digest_interrupted_fallback", project="common"),
        log_entry(
            "compaction_applied",
            project="common",
            records_suppressed=4,
            suppression_by_rule={"age": 1, "unused": 2, "duplicate": 1},
        ),
    ]


class StatsTests(unittest.TestCase):
    """П-10: `minimem stats` по нормативному перечню §21.3 (MM-140…MM-142)."""

    def test_counts_all_normative_metrics(self) -> None:
        """MM-140. Все метрики нормативного списка считаются на синтетическом логе."""

        report = stats.collect(synthetic_log(), config=dict(DEFAULTS))
        self.assertEqual(report.records_total, 16)
        self.assertEqual(report.search_runs, 3)
        self.assertEqual(report.search_empty, 1)
        self.assertEqual(report.search_no_hits, 1)
        self.assertEqual(report.search_errors, 1)
        self.assertEqual(report.search_terms_total, 15)
        self.assertEqual(report.search_hits_total, 5)
        self.assertEqual(report.search_returned_total, 2)
        self.assertEqual(report.search_scores, [3.2, 1.8, 1.1])
        self.assertEqual(report.search_score_ratio, DEFAULTS["search_score_ratio"])
        self.assertEqual(
            report.search_score_threshold, DEFAULTS["search_score_threshold"]
        )
        self.assertEqual(report.turns_seen, 1)
        self.assertEqual(report.turns_captured, 1)
        self.assertEqual(report.truncations, 1)
        self.assertEqual(report.withheld, 1)
        self.assertEqual(report.redactions, 1)
        self.assertEqual(report.duplicates, 1)
        self.assertEqual(report.indexer_runs, 1)
        self.assertEqual(report.records_indexed, 5)
        self.assertEqual(report.cursor_resets, 1)
        self.assertEqual(report.records_damaged, 1)
        self.assertEqual(report.catch_up_runs, 2)
        self.assertEqual(report.catch_up_timeouts, 1)
        self.assertEqual(report.digest_created, 1)
        self.assertEqual(report.digest_fallback, 1)
        self.assertEqual(report.digest_interrupted, 1)
        self.assertEqual(report.suppressed_age, 1)
        self.assertEqual(report.suppressed_unused, 2)
        self.assertEqual(report.suppressed_duplicate, 1)
        self.assertEqual(report.latency_max, 2000)
        self.assertGreaterEqual(report.latency_p95, report.latency_p50)
        self.assertEqual(report.minimem_version, DEFAULTS["minimem_version"])

    def test_distinguishes_no_hits_from_error(self) -> None:
        """MM-142. «Нет попаданий» и «ошибка/таймаут» — разные числа в отчёте."""

        report = stats.collect(synthetic_log(), config=dict(DEFAULTS))
        self.assertEqual(report.search_no_hits, 1)
        self.assertEqual(report.search_errors, 1)
        self.assertEqual(report.search_timeouts, 1)
        text = report.render()
        self.assertIn("нет попаданий 1", text)
        self.assertIn("ошибок 1", text)
        self.assertIn("таймаутов 1", text)
        self.assertIn("таймаутов 2", text)  # поиск и catch-up

    def test_search_log_carries_measurement_contract(self) -> None:
        """MM-141. В логе поиска есть search_hits, returned_event_ids и top5_scores."""

        from mm.log import build_record

        plan = stats.StatsReport()
        self.assertEqual(plan.search_hits_total, 0)
        record = build_record(
            "search",
            "search_completed",
            search_hits=3,
            returned_event_ids=["e_1", "e_2"],
            top5_scores=[2.5, 1.5],
            search_terms=4,
            search_query_mode="any",
            search_returned=2,
            threshold_applied="absolute",
        )
        for key in (
            "search_terms",
            "search_query_mode",
            "search_hits",
            "search_returned",
            "returned_event_ids",
            "top5_scores",
            "threshold_applied",
        ):
            self.assertIn(key, record)

    def test_project_filter(self) -> None:
        """`--project` ограничивает отчёт проектом (§21.3)."""

        records = synthetic_log()
        records.append(log_entry("incremental_index", project="work", records_indexed=7))
        report = stats.collect(records, project="work")
        self.assertEqual(report.records_total, 1)
        self.assertEqual(report.records_indexed, 7)

    def test_period_filter(self) -> None:
        """`--since` и `--until` отсекают записи вне периода."""

        records = synthetic_log()
        records.append(
            {
                "timestamp": "2026-09-20T07:15:42+00:00",
                "module": "test",
                "operation": "incremental_index",
                "records_indexed": 99,
            }
        )
        report = stats.collect(
            records,
            since=stats.parse_date_arg("2026-09-26"),
            until=stats.parse_date_arg("2026-09-27"),
        )
        self.assertEqual(report.records_indexed, 5)

    def test_broken_log_lines_are_skipped(self) -> None:
        """Нечитаемая строка лога не роняет отчёт (§19.2)."""

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "minimem.log"
            log_file.write_text(
                '{"operation": "digest_created"}\nне json\n\n', encoding="utf-8"
            )
            records = stats.read_log_records(log_file)
        self.assertEqual(len(records), 1)

    def test_json_output_has_all_sections(self) -> None:
        """`--json` содержит те же разделы, что и текстовый отчёт."""

        data = stats.collect(synthetic_log(), config=dict(DEFAULTS)).as_dict()
        for section in (
            "period",
            "search",
            "capture",
            "indexer",
            "catch_up",
            "digest",
            "compaction",
            "hooks",
            "config",
        ):
            self.assertIn(section, data)
        json.dumps(data, ensure_ascii=False)


class StatsCoverageTests(unittest.TestCase):
    """Перечни `stats` покрывают реальные операции модулей (§21.3).

    Найдено на rollout: операции `search_injected` и `capture_record_written`
    не были в перечнях, поэтому на живом логе `stats` показывал нулевые
    запуски поиска и нулевые записанные ходы. Тест не даёт этому повториться.
    """

    def test_search_operations_are_counted(self) -> None:
        operations = search_operations_in_source()
        self.assertIn("search_injected", operations)
        self.assertTrue(operations)
        missing = operations - stats.SEARCH_OPERATIONS
        self.assertEqual(missing, set(), f"операции поиска вне перечня stats: {missing}")

    def test_capture_operations_are_counted(self) -> None:
        operations = capture_operations_in_source()
        self.assertIn("capture_record_written", operations)
        self.assertTrue(operations)
        missing = operations - stats.CAPTURE_OPERATIONS
        self.assertEqual(missing, set(), f"операции захвата вне перечня stats: {missing}")

    def test_injected_search_counts_as_run(self) -> None:
        """Успешная вставка — это состоявшийся запуск поиска."""

        record = log_entry(
            "search_injected",
            project="common",
            search_terms=4,
            search_hits=1,
            search_returned=1,
            top5_scores=[2.4],
        )
        report = stats.collect([record])
        self.assertEqual(report.search_runs, 1)
        self.assertEqual(report.search_returned_total, 1)
        self.assertEqual(report.search_empty, 0)
        self.assertEqual(report.search_no_hits, 0)

    def test_written_turn_counts_as_captured(self) -> None:
        """Успешная запись в журнал — это захваченный ход."""

        record = log_entry(
            "capture_record_written",
            project="common",
            turns_seen=1,
            turns_captured=1,
            turns_skipped=0,
            turns_derived=0,
        )
        report = stats.collect([record])
        self.assertEqual(report.turns_seen, 1)
        self.assertEqual(report.turns_captured, 1)
        self.assertEqual(report.turns_skipped, 0)


class DoctorCliTests(BackupSettingsCase):
    """Команда `minimem doctor` как CLI (§21.1)."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", str(MINIMEM_PY), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def write_config_file(self) -> Path:
        payload = dict(DEFAULTS)
        payload["memory_root"] = str(self.memory_root)
        path = self.tmp / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_doctor_exit_codes(self) -> None:
        """Расхождение по бэкапу даёт код 1, отсутствие настроек — код 0."""

        hermes_config = self.write_hermes_config()
        result = self.run_cli(
            "--config",
            str(self.write_config_file()),
            "doctor",
            "--hermes-config",
            str(hermes_config),
            "--backup-settings",
            str(self.settings),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("backup_stale", result.stdout)

        empty = self.tmp / "нет" / "backup.json"
        result = self.run_cli(
            "--config",
            str(self.write_config_file()),
            "doctor",
            "--hermes-config",
            str(hermes_config),
            "--backup-settings",
            str(empty),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("вне области MiniMem", result.stdout)

    def test_doctor_reports_deadlines(self) -> None:
        """Отчёт содержит пересчитанные дедлайны и суммы суббюджетов."""

        result = self.run_cli(
            "--config",
            str(self.write_config_file()),
            "doctor",
            "--hermes-config",
            str(self.write_hermes_config()),
            "--backup-settings",
            str(self.tmp / "нет" / "backup.json"),
        )
        self.assertIn("hook_deadline_ms 13000", result.stdout)
        self.assertIn("hook_deadline_ms 28000", result.stdout)
        self.assertIn("сумма 9400 мс", result.stdout)
        self.assertIn("сумма 22400 мс", result.stdout)

    def test_doctor_logs_result(self) -> None:
        """Результат `doctor` пишется в лог (§22)."""

        result = self.run_cli(
            "--config",
            str(self.write_config_file()),
            "doctor",
            "--hermes-config",
            str(self.write_hermes_config()),
            "--backup-settings",
            str(self.tmp / "нет" / "backup.json"),
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        log_file = self.memory_root / "logs" / "minimem.log"
        records = [json.loads(line) for line in log_file.read_text("utf-8").splitlines()]
        operations = [record["operation"] for record in records]
        self.assertIn("doctor", operations)


class StatsCliTests(BackupSettingsCase):
    """Команда `minimem stats` как CLI (§21.3)."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", str(MINIMEM_PY), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def write_config_file(self) -> Path:
        payload = dict(DEFAULTS)
        payload["memory_root"] = str(self.memory_root)
        path = self.tmp / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def write_log(self, records: list[dict]) -> None:
        log_file = self.memory_root / "logs" / "minimem.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )

    def test_stats_text_and_json(self) -> None:
        """`stats` печатает отчёт и его же отдаёт в JSON."""

        self.write_log(synthetic_log())
        config = str(self.write_config_file())
        text = self.run_cli("--config", config, "stats", "--since", "2020-01-01")
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn("поиск: запусков 3", text.stdout)
        as_json = self.run_cli("--config", config, "stats", "--since", "2020-01-01", "--json")
        self.assertEqual(as_json.returncode, 0, as_json.stdout + as_json.stderr)
        data = json.loads(as_json.stdout)
        self.assertEqual(data["search"]["runs"], 3)
        self.assertEqual(data["search"]["no_hits"], 1)

    def test_stats_bad_date_argument(self) -> None:
        """Неразбираемая дата — код 1, не исключение."""

        result = self.run_cli(
            "--config", str(self.write_config_file()), "stats", "--since", "позавчера"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("не удалось разобрать дату", result.stdout)


class HookScriptsTests(unittest.TestCase):
    """Состав установки §26.3: три скрипта хуков рядом с кодом MiniMem."""

    HOOKS = ("pre_llm.py", "post_llm.py", "session_end.py")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.config_file = self.tmp / "config.json"
        payload = dict(DEFAULTS)
        payload["memory_root"] = str(self.tmp / "memory")
        self.config_file.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_hook_scripts_exist(self) -> None:
        for name in self.HOOKS:
            path = CODE_DIR / "hooks" / name
            with self.subTest(hook=name):
                self.assertTrue(path.is_file(), f"нет {path}")

    def test_hook_scripts_compile(self) -> None:
        for name in self.HOOKS:
            path = CODE_DIR / "hooks" / name
            with self.subTest(hook=name):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "py_compile", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def run_hook(self, name: str, payload: dict) -> None:
        env = dict(os.environ)
        env["MINIMEM_CONFIG"] = str(self.config_file)
        result = subprocess.run(
            [sys.executable, "-B", str(CODE_DIR / "hooks" / name)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def log_records(self) -> list[dict]:
        log_file = self.tmp / "memory" / "logs" / "minimem.log"
        if not log_file.exists():
            return []
        return [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_hooks_log_their_duration(self) -> None:
        """§22 и §21.3: каждый хук пишет `duration_ms`, иначе задержки нулевые."""

        self.run_hook(
            "post_llm.py",
            {
                "session_id": "s1",
                "cwd": "C:/projects/work",
                "extra": {
                    "task_id": "t1",
                    "turn_id": "1",
                    "user_message": "проверка задержки",
                    "assistant_response": "ответ",
                },
            },
        )
        self.run_hook(
            "pre_llm.py",
            {
                "session_id": "s2",
                "cwd": "C:/projects/work",
                "extra": {"task_id": "t2", "turn_id": "1", "user_message": "вопрос"},
            },
        )
        self.run_hook(
            "session_end.py",
            {
                "session_id": "s2",
                "cwd": "C:/projects/work",
                "extra": {"completed": True},
            },
        )
        durations = [
            record
            for record in self.log_records()
            if record.get("operation") == "hook_completed"
        ]
        self.assertEqual(
            sorted(record["hook_event"] for record in durations),
            ["on_session_end", "post_llm_call", "pre_llm_call"],
        )
        for record in durations:
            with self.subTest(hook=record["hook_event"]):
                self.assertIsInstance(record["duration_ms"], int)
                self.assertGreaterEqual(record["duration_ms"], 0)


@unittest.skipUnless(
    os.environ.get("MINIMEM_RUN_HERMES_TESTS") == "1",
    "тест 47 запускает настоящий Hermes и выполняется только по команде владельца",
)
class InstallationTest47(unittest.TestCase):
    """Тест 47 (§23): все три хука зарегистрированы, `hermes hooks doctor` здоров.

    Запускается только при `MINIMEM_RUN_HERMES_TESTS=1` и только по команде
    владельца: тест обращается к установленному Hermes и его конфигурации.
    """

    HOOKS = ("pre_llm.py", "post_llm.py", "session_end.py")
    EVENTS = {
        "pre_llm.py": "pre_llm_call",
        "post_llm.py": "post_llm_call",
        "session_end.py": "on_session_end",
    }

    def hermes_home(self) -> Path:
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"

    def test_all_hooks_registered(self) -> None:
        hooks = doctor.read_hermes_hooks()
        self.assertTrue(hooks.exists, hooks.path)
        for name in self.HOOKS:
            with self.subTest(hook=name):
                event = self.EVENTS[name]
                commands = [hook.command for hook in hooks.by_event(event)]
                self.assertTrue(
                    any(name in command for command in commands),
                    f"{event}: нет записи со скриптом {name}: {commands}",
                )
                self.assertIsNotNone(hooks.timeout_s(event), f"{event}: нет предела")

    def test_commands_allowlisted(self) -> None:
        allowlist = self.hermes_home() / "shell-hooks-allowlist.json"
        raw = json.loads(allowlist.read_text(encoding="utf-8"))
        approvals = {
            (item.get("event"), item.get("command"))
            for item in raw.get("approvals", [])
        }
        hooks = doctor.read_hermes_hooks()
        for name in self.HOOKS:
            with self.subTest(hook=name):
                event = self.EVENTS[name]
                commands = [hook.command for hook in hooks.by_event(event)]
                self.assertTrue(
                    any((event, command) in approvals for command in commands),
                    f"{event}: команда не внесена в allowlist",
                )

    def test_hermes_hooks_doctor_reports_no_errors(self) -> None:
        result = subprocess.run(
            ["hermes", "hooks", "doctor"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)







