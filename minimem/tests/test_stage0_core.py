"""Тесты этапа 0 (каркас MiniMem).

Основание: план реализации v1.7, этап 0 («Тесты ТЗ: инфраструктурные,
обязательных приёмочных тестов нет»). Номера MM-NN этим тестам не
присвоены: §23 ТЗ v1.7 относится к приёмочным проверкам подсистем, которые
появляются на этапах 1-8.

Запуск: python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import paths  # noqa: E402
from mm.config import (  # noqa: E402
    DEFAULTS,
    ConfigError,
    config_hash,
    load_config,
    validate,
)
from mm.log import Logger, build_record, format_record  # noqa: E402
from mm.project import normalize_path, resolve_project  # noqa: E402


class ConfigTests(unittest.TestCase):
    """Загрузка конфигурации, умолчания и инварианты (ТЗ v1.7 §20, П-14)."""

    def test_shipped_config_is_valid(self) -> None:
        config = load_config(CODE_DIR / "config.json")
        self.assertEqual(config.issues, ())
        self.assertEqual(validate(config.as_dict()), [])

    def test_defaults_match_specification(self) -> None:
        self.assertIs(DEFAULTS["mode_capture"], True)
        self.assertIs(DEFAULTS["mode_return"], False)
        self.assertIs(DEFAULTS["mode_search"], False)
        self.assertIs(DEFAULTS["mode_compaction"], False)
        self.assertIs(DEFAULTS["compaction_rule2_enabled"], False)
        self.assertIs(DEFAULTS["journal_fsync"], True)
        self.assertEqual(DEFAULTS["journal_timezone"], "system")
        self.assertEqual(DEFAULTS["catch_up_retry_max_turns"], 2)
        self.assertEqual(DEFAULTS["catch_up_budget_seconds"], 8)
        self.assertEqual(DEFAULTS["search_score_ratio"], 0.35)
        self.assertEqual(DEFAULTS["compaction_min_chars_drop"], 3000)
        self.assertEqual(DEFAULTS["compaction_cooldown_turns"], 2)
        self.assertEqual(DEFAULTS["digest_turns"], 3)

    def test_missing_keys_take_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text(
                json.dumps({"memory_root": tmp, "mode_search": True}),
                encoding="utf-8",
            )
            config = load_config(config_file)
        self.assertIs(config["mode_search"], True)
        self.assertEqual(config["max_search_terms"], DEFAULTS["max_search_terms"])
        self.assertEqual(config.issues, ())

    def test_invalid_invariant_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text(
                json.dumps({"memory_root": tmp, "compaction_unused_age": 400}),
                encoding="utf-8",
            )
            config = load_config(config_file)
        self.assertTrue(any(issue.startswith("config_invalid:") for issue in config.issues))
        self.assertEqual(config["compaction_unused_age"], DEFAULTS["compaction_unused_age"])

    def test_bad_type_reports_config_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text(
                json.dumps({"memory_root": tmp, "journal_fsync": "да"}),
                encoding="utf-8",
            )
            config = load_config(config_file)
        self.assertIs(config["journal_fsync"], True)
        self.assertTrue(any("journal_fsync" in issue for issue in config.issues))

    def test_unknown_key_is_reported_and_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text(
                json.dumps({"memory_root": tmp, "mode_turbo": True}),
                encoding="utf-8",
            )
            config = load_config(config_file)
        self.assertIn("config_unknown_key: mode_turbo", config.issues)
        self.assertNotIn("mode_turbo", config.as_dict())

    def test_invalid_json_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.write_text("{нет", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_file)

    def test_config_hash_changes_with_behavior(self) -> None:
        base = dict(DEFAULTS)
        changed = dict(DEFAULTS)
        changed["max_user_text"] = DEFAULTS["max_user_text"] + 1
        self.assertNotEqual(config_hash(base), config_hash(changed))
        self.assertEqual(config_hash(base), config_hash(dict(reversed(list(base.items())))))

    def test_config_hash_ignores_paths(self) -> None:
        base = dict(DEFAULTS)
        other = dict(DEFAULTS)
        other["memory_root"] = "C:\\other"
        self.assertEqual(config_hash(base), config_hash(other))

    def test_hook_deadline_respects_safety_margin(self) -> None:
        config = load_config(CODE_DIR / "config.json")
        self.assertEqual(
            config.hook_deadline_ms("pre_llm"),
            int(config["pre_llm_timeout"] * 1000) - config["hook_safety_margin_ms"],
        )


class ProjectTests(unittest.TestCase):
    """Определение проекта по cwd (ТЗ v1.7 §8, П-16)."""

    mapping = {
        "C:\\work": "work",
        "C:\\work\\inner": "inner",
        "C:\\Other": "other",
    }

    def test_unknown_path_gives_common(self) -> None:
        project, normalized = resolve_project("C:\\nowhere\\project", self.mapping)
        self.assertEqual(project, "common")
        self.assertTrue(normalized)

    def test_empty_cwd_gives_common(self) -> None:
        self.assertEqual(resolve_project("", self.mapping)[0], "common")
        self.assertEqual(resolve_project(None, self.mapping)[1], "")

    def test_longest_prefix_wins(self) -> None:
        self.assertEqual(resolve_project("C:\\work\\inner\\sub", self.mapping)[0], "inner")
        self.assertEqual(resolve_project("C:\\work\\other", self.mapping)[0], "work")

    def test_comparison_is_case_insensitive_on_windows(self) -> None:
        self.assertEqual(resolve_project("C:\\WORK\\INNER", self.mapping)[0], "inner")

    def test_trailing_separators_ignored(self) -> None:
        self.assertEqual(resolve_project("C:\\work\\", self.mapping)[0], "work")
        self.assertEqual(
            resolve_project("C:\\work", {"C:\\work\\": "work"})[0], "work"
        )

    def test_sibling_prefix_is_not_a_match(self) -> None:
        self.assertEqual(resolve_project("C:\\workspace", self.mapping)[0], "common")

    def test_normalize_collapses_separators(self) -> None:
        normalized = normalize_path("C:\\work\\inner\\")
        self.assertNotIn("\\", normalized)
        self.assertFalse(normalized.endswith("/"))

    def test_exact_prefix_matches(self) -> None:
        self.assertEqual(resolve_project("C:\\Other", self.mapping)[0], "other")


class PathsTests(unittest.TestCase):
    """Пути хранилища и часовой пояс журнала (ТЗ v1.7 §4.1)."""

    def test_journal_file_path(self) -> None:
        root = Path("C:/store")
        self.assertEqual(
            paths.journal_file(root, "proj", date(2026, 9, 22)).as_posix(),
            "C:/store/proj/memory/2026-09-22.md",
        )

    def test_digest_dir_under_memory(self) -> None:
        self.assertEqual(
            paths.digest_dir(Path("C:/store"), "proj").as_posix(),
            "C:/store/proj/memory/digest",
        )

    def test_sqlite_and_log_paths(self) -> None:
        root = Path("C:/store")
        self.assertEqual(paths.sqlite_path(root).as_posix(), "C:/store/minimem.db")
        self.assertEqual(paths.log_path(root).as_posix(), "C:/store/logs/minimem.log")

    def test_utc_timezone_uses_utc_day(self) -> None:
        moment = datetime(2026, 9, 22, 23, 30, tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(paths.local_date("utc", moment), date(2026, 9, 22))
        self.assertEqual(paths.local_date("system", moment), date(2026, 9, 22))

    def test_timezone_shifts_day_at_boundary(self) -> None:
        moment = datetime(2026, 9, 23, 1, 0, tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(paths.local_date("utc", moment), date(2026, 9, 22))

    def test_invalid_timezone_rejected(self) -> None:
        with self.assertRaises(ValueError):
            paths.resolve_timezone("local")

    def test_safe_component_forbidden_characters(self) -> None:
        self.assertEqual(paths.safe_component("a/b:c*d?e"), "a_b_c_d_e")
        self.assertEqual(paths.safe_component("   "), "unnamed")
        self.assertEqual(paths.safe_component("проект"), "проект")

    def test_existing_journal_files_sorted_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("b", "a"):
                journal = root / name / "memory"
                journal.mkdir(parents=True)
                (journal / "2026-09-23.md").write_text("<!-- mm:begin -->\n", encoding="utf-8")
                (journal / "2026-09-22.md").write_text("<!-- mm:begin -->\n", encoding="utf-8")
                (journal / "notes.txt").write_text("не журнал", encoding="utf-8")
            files = paths.existing_journal_files(root)
            self.assertEqual(
                [path.name for path in files],
                ["2026-09-22.md", "2026-09-23.md", "2026-09-22.md", "2026-09-23.md"],
            )


class LogTests(unittest.TestCase):
    """Структурированный лог (ТЗ v1.7 §22, §19.5, §7.1)."""

    def test_base_fields_present(self) -> None:
        record = build_record("capture", "capture_disabled", status="ok", project="common")
        for field in ("timestamp", "module", "operation", "status"):
            self.assertIn(field, record)
        self.assertEqual(record["module"], "capture")
        self.assertEqual(record["operation"], "capture_disabled")

    def test_secret_bearing_fields_are_dropped(self) -> None:
        record = build_record(
            "capture",
            "capture",
            user_message="мой пароль",
            assistant_response="ответ",
            api_key="sk-123",
            event_id="e1",
        )
        self.assertNotIn("user_message", record)
        self.assertNotIn("assistant_response", record)
        self.assertNotIn("api_key", record)
        self.assertEqual(record["event_id"], "e1")

    def test_traceback_field_is_dropped(self) -> None:
        record = build_record("capture", "capture", traceback="Traceback...")
        self.assertNotIn("traceback", record)

    def test_none_values_omitted(self) -> None:
        record = build_record("capture", "capture", error_code=None)
        self.assertNotIn("error_code", record)

    def test_line_has_no_newlines(self) -> None:
        line = format_record(build_record("capture", "capture", detail="a\nb"))
        self.assertNotIn("\n", line)
        self.assertIn(" ", line)

    def test_logger_writes_utf8_lf_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "logs" / "minimem.log"
            logger = Logger(log_file)
            logger.log("cli", "verify", status="ok", error_code="нет")
            raw = log_file.read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertTrue(raw.endswith(b"\n"))
            payload = json.loads(raw.decode("utf-8").splitlines()[0])
            self.assertEqual(payload["operation"], "verify")
            self.assertEqual(log_file.parent.name, "logs")

    def test_logger_survives_unwritable_path(self) -> None:
        blocker = Path(tempfile.gettempdir()) / "minimem-log-blocker"
        blocker.write_text("file", encoding="utf-8")
        try:
            logger = Logger(blocker / "nested" / "minimem.log")
            record = logger.log("cli", "status", status="ok")
            self.assertEqual(record["operation"], "status")
            self.assertIsNotNone(logger.last_error)
        finally:
            blocker.unlink()

    def test_error_uses_code_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = Logger(Path(tmp) / "minimem.log")
            record = logger.error("journal", "journal_append_unverified", "journal_error")
            self.assertEqual(record["status"], "error")
            self.assertEqual(record["error_code"], "journal_error")
            self.assertNotIn("exception", record)


class CliTests(unittest.TestCase):
    """Каркас CLI: `status` и `verify` (ТЗ v1.7 §21, этап 0 плана)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.config_file = root / "config.json"
        cls.config_file.write_text(
            json.dumps(
                {
                    "memory_root": str(root / "store"),
                    "project_mapping": {str(root / "work"): "work"},
                }
            ),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(CODE_DIR / "minimem.py"),
                "--config",
                str(self.config_file),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(cwd) if cwd else None,
            check=False,
        )

    def test_status_exit_code_and_content(self) -> None:
        result = self._run("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in (
            "memory_root",
            "файлов журнала",
            "SQLite",
            "mode_capture: вкл",
            "mode_return: выкл",
            "compaction_rule2_enabled: выкл",
            "journal_fsync: вкл",
            "journal_timezone: system",
            "catch_up_retry_max_turns: 2",
            "catch_up_budget_seconds: 8",
            "конфигурация: валидна",
        ):
            self.assertIn(expected, result.stdout)

    def test_status_uses_nested_cwd_mapping(self) -> None:
        nested = Path(self.tmp.name) / "work" / "inner"
        nested.mkdir(parents=True, exist_ok=True)
        result = self._run("status", cwd=nested)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("текущий проект: work", result.stdout)

    def test_verify_without_journal_succeeds(self) -> None:
        result = self._run("verify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("итог: без проблем", result.stdout)

    def test_verify_reports_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "config.json"
            bad.write_text(
                json.dumps({"memory_root": tmp, "search_score_ratio": 5}),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(CODE_DIR / "minimem.py"), "--config", str(bad), "verify"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("config_invalid", result.stdout)

    def test_missing_config_exits_with_code_2(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CODE_DIR / "minimem.py"), "--config", "нет.json", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("проблема", result.stderr)

    def test_verify_writes_log(self) -> None:
        self._run("verify")
        log_file = Path(self.tmp.name) / "store" / "logs" / "minimem.log"
        self.assertTrue(log_file.exists())
        operations = [
            json.loads(line)["operation"]
            for line in log_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("verify", operations)


if __name__ == "__main__":
    unittest.main()
