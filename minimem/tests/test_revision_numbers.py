"""Этап 1 ревизии приёмочных номеров: проверка 19 номеров раздела 3.3.

Основание: ТЗ v1.7 §23 (номера и статусы тестов), §13.1, §7.1, §4.4, §9.2,
§11.1, §18.1, §19.1.1, §20.1, §21.2, §21.4, §22, П-18; документ
«Docs/MiniMem-1 - План ревизии приёмочных номеров.md», раздел 3.3.

Номера этого файла: 56, 82, 83, 84, 87, 88, 119, 126, 127, 128, 129, 130, 132,
133, 144, 145, 151, 152, 155, 160. Каждый тест сначала был написан как пробная
проверка поведения; факт о поведении зафиксирован в таблице 3.3 плана ревизии.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import (  # noqa: E402
    canonical,
    capture,
    catchup,
    digest,
    indexer,
    insert,
    journal,
    paths,
    redaction,
    ret,
    search,
    session,
)
from mm.config import DEFAULTS, config_hash, load_config, validate  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.project import normalize_path  # noqa: E402
from mm.store import Store  # noqa: E402

CLI = CODE_DIR / "minimem.py"
POST_LLM_HOOK = CODE_DIR / "hooks" / "post_llm.py"
PRE_LLM_HOOK = CODE_DIR / "hooks" / "pre_llm.py"
MOMENT = datetime.fromisoformat("2026-09-26T07:15:42+03:00")
PREVIOUS = "2026-09-25-01"
SESSION = "2026-09-26-01"
CWD = "C:/projects/work"


def make_config(tmp: str, **overrides) -> dict:
    config = dict(DEFAULTS)
    config["memory_root"] = tmp
    config["project_mapping"] = {CWD: "work"}
    config.update(overrides)
    return config


def make_logger(tmp: str) -> Logger:
    return Logger(Path(tmp) / "logs" / "minimem.log")


def make_store(tmp: str) -> Store:
    store = Store(Path(tmp) / "minimem.db")
    store.create_schema()
    return store


def log_operations(tmp: str) -> list[str]:
    log = Path(tmp) / "logs" / "minimem.log"
    if not log.exists():
        return []
    return [json.loads(line)["operation"] for line in log.read_text("utf-8").splitlines()]


def write_config(root: Path, config: dict) -> Path:
    path = root / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def run_hook(path: Path, config_path: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(path)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env={**dict(os.environ), "MINIMEM_CONFIG": str(config_path)},
    )


class RevisionTestCase(unittest.TestCase):
    """Общая подготовка: временное хранилище, конфигурация, служебный слой."""

    modes: dict = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = Path(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp, **self.modes)
        self.logger = make_logger(self.tmp)
        self._store: Store | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = make_store(self.tmp)
        return self._store

    def close_store(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def tearDown(self) -> None:
        self.close_store()

    def add_turns(
        self,
        count: int = 3,
        session_id: str = SESSION,
        start: int = 1,
        base: datetime | None = None,
        user: str = "реплика номер",
        answer: str = "ответ номер",
    ) -> list[capture.CaptureResult]:
        moment = base or MOMENT
        results: list[capture.CaptureResult] = []
        for offset in range(count):
            number = start + offset
            data = capture.TurnData(
                user_message=f"{user} {number}",
                assistant_response=f"{answer} {number}",
                session_id=session_id,
                task_id=f"task_{number}",
                turn_id=str(number),
                cwd=CWD,
            )
            results.append(
                capture.capture_turn(
                    data,
                    self.config,
                    self.logger,
                    self.root,
                    moment=moment + timedelta(minutes=5 * offset),
                )
            )
        indexer.index_project(self.store, self.root, self.logger)
        return results

    def journal_file(self) -> Path:
        return paths.existing_journal_files(self.root)[0]


class AccumulationModeTests(RevisionTestCase):
    """56: режим «Накопление» пишет журнал, но не влияет на контекст Hermes."""

    modes = {"mode_return": False, "mode_search": False, "mode_compaction": False}

    def test_56_capture_only_mode_writes_journal_without_context(self) -> None:
        """MM-56. Захват пишет журнал; pre_llm_call не вставляет ничего."""

        config_path = write_config(self.root, self.config)
        posted = run_hook(
            POST_LLM_HOOK,
            config_path,
            {
                "session_id": SESSION,
                "cwd": CWD,
                "extra": {
                    "task_id": "task_1",
                    "turn_id": "1",
                    "user_message": "накопление без возврата",
                    "assistant_response": "ответ накопления",
                },
            },
        )
        self.assertEqual(posted.returncode, 0, posted.stderr)
        self.assertEqual(posted.stdout.decode("utf-8").strip(), "{}")

        files = paths.existing_journal_files(self.root)
        self.assertEqual(len(files), 1)
        records, damaged = journal.read_records(files[0])
        self.assertEqual(damaged, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].user_utterance, "накопление без возврата")

        called = run_hook(
            PRE_LLM_HOOK,
            config_path,
            {
                "session_id": SESSION,
                "cwd": CWD,
                "extra": {
                    "task_id": "task_2",
                    "turn_id": "2",
                    "user_message": "следующий вопрос",
                    "conversation_history": [],
                    "is_first_turn": True,
                },
            },
        )
        self.assertEqual(called.returncode, 0, called.stderr)
        # Никакой вставки: сообщение пользователя не меняется (§3.7.1).
        self.assertEqual(called.stdout.decode("utf-8").strip(), "{}")
        self.assertIn("return_disabled", log_operations(self.tmp))


class InsertSanitizationTests(unittest.TestCase):
    """82, 83, 84: делимитеры, управляющие символы и секции записи (§13.1)."""

    def test_82_record_with_end_delimiter_does_not_break_block(self) -> None:
        """MM-82. `=== КОНЕЦ ДАННЫХ ПАМЯТИ ===` в тексте не ломает границу."""

        body = f"строка до\n{insert.END_DELIMITER}\nстрока после"
        block, _ = insert.build_memory_block(body, int(DEFAULTS["max_return_chars"]))
        lines = [line.strip() for line in block.split("\n")]
        self.assertEqual(lines.count(insert.END_DELIMITER), 1)
        self.assertEqual(lines[-1], insert.END_DELIMITER)
        self.assertEqual(lines.count(insert.SANITIZED_END_DELIMITER), 1)
        self.assertIn("строка после", block)

    def test_83_control_unicode_is_neutralised_before_insert(self) -> None:
        """MM-83. Управляющие Unicode-символы нейтрализуются перед вставкой."""

        raw = "до\u200b\ufeff\u2060\u200dпосле\x1b[31m\x07"
        sanitized = insert.sanitize_for_insert(raw)
        for char in ("\u200b", "\ufeff", "\u2060", "\u200d", "\x1b", "\x07"):
            self.assertNotIn(char, sanitized)
        block, _ = insert.build_memory_block(raw, int(DEFAULTS["max_return_chars"]))
        self.assertNotIn("\u200b", block)
        self.assertIn("до", block)
        self.assertIn("после", block)

    def test_84_record_with_section_headers_is_handled_safely(self) -> None:
        """MM-84. `### User` / `### Assistant` в записи не ломают разбор и вставку."""

        self.assertEqual(canonical.escape_body_line(canonical.USER_SECTION), "\\### User")
        self.assertEqual(
            canonical.unescape_body_line(
                canonical.escape_body_line(canonical.ASSISTANT_SECTION)
            ),
            canonical.ASSISTANT_SECTION,
        )
        body = f"**Вы:** {canonical.USER_SECTION}\n**Агент:** {canonical.ASSISTANT_SECTION}"
        block, _ = insert.build_memory_block(body, int(DEFAULTS["max_return_chars"]))
        lines = [line.strip() for line in block.split("\n")]
        self.assertEqual(lines.count(insert.END_DELIMITER), 1)
        self.assertEqual(lines[-1], insert.END_DELIMITER)
        self.assertIn(canonical.USER_SECTION, block)


class RedactionTemplateTests(unittest.TestCase):
    """87, 88: JWT и PEM-ключ заменяются placeholder'ами (§7.1)."""

    def test_87_jwt_is_replaced(self) -> None:
        """MM-87. JWT заменяется placeholder'ом."""

        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
        result = redaction.redact(f"токен доступа: {jwt} конец")
        self.assertNotIn(jwt, result.text)
        self.assertIn("[REDACTED:jwt]", result.text)
        self.assertEqual(result.counts.get("jwt"), 1)

    def test_88_pem_private_key_is_replaced(self) -> None:
        """MM-88. PEM-ключ заменяется placeholder'ом."""

        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxSecretMaterialWithoutReproduction0000000000\n"
            "-----END RSA PRIVATE KEY-----"
        )
        result = redaction.redact(f"ключ:\n{pem}\nконец")
        self.assertNotIn("MIIEowIBAAKCAQEAxSecretMaterial", result.text)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", result.text)
        self.assertIn("[REDACTED:private_key]", result.text)
        self.assertEqual(result.counts.get("private_key"), 1)



class UnknownMetadataKeyTests(RevisionTestCase):
    """119: запись с неизвестным ключом читается полностью и индексируется."""

    def test_119_unknown_key_record_is_indexed_with_text(self) -> None:
        """MM-119. Неизвестный ключ метаданных: текст цел, запись в индексе."""

        metadata = {
            "timestamp": MOMENT.isoformat(timespec="seconds"),
            "fmt": canonical.RECORD_FMT,
            "project": "work",
            "project_path": normalize_path(CWD),
            "session_id": SESSION,
            "task_id": "task_1",
            "turn": "1",
            "turn_source": "hermes",
            "event_id": "e_unknownkey0000000000000000000000000",
            "revision": 1,
            "supersedes": "",
            "content_hash": canonical.content_hash("текст реплики", "текст ответа"),
            "redaction_set": "00000000",
            "body_status": "full",
            "truncated": "none",
            "sender_id": "",
            "platform": "",
            "future_field_v2": "значение будущего поля",
        }
        target = paths.journal_file(self.root, "work", MOMENT.date())
        journal.append_record(
            target, canonical.render_record(metadata, "текст реплики", "текст ответа")
        )
        records, damaged = journal.read_records(target)
        self.assertEqual(damaged, [])
        self.assertFalse(records[0].damaged)
        self.assertEqual(records[0].extra_metadata["future_field_v2"], "значение будущего поля")
        self.assertEqual(records[0].user_utterance, "текст реплики")

        result = indexer.index_project(self.store, self.root, self.logger)
        self.assertEqual(result.records_indexed, 1)
        self.assertIsNotNone(self.store.meta_get(metadata["event_id"]))
        self.assertEqual(self.store.fts_count(), 1)
        self.assertEqual(len(self.store.fts_search("реплики")), 1)


class DigestSanitizationTests(RevisionTestCase):
    """126, 127: делимитер в дайджесте и измерение истории (П-05)."""

    modes = {"mode_return": True}

    def _build(self, body: str) -> Path:
        self.add_turns(1, answer=body)
        result = digest.build_digest(
            self.store, self.config, self.root, self.logger, SESSION, "work"
        )
        self.assertTrue(result.created_ok)
        return Path(result.path)

    def test_126_digest_with_delimiter_does_not_create_second_boundary(self) -> None:
        """MM-126. Делимитер в тексте дайджеста не создаёт вторую границу."""

        self._build(f"до\n{insert.END_DELIMITER}\nпосле")
        path = digest.digest_path(self.root, "work", SESSION)
        candidate = ret.DigestCandidate(
            path=path, header=digest.read_header(path), last_turn_utc=MOMENT.isoformat()
        )
        text, _ = ret.build_return_text(candidate, self.config, self.logger)
        lines = [line.strip() for line in text.split("\n")]
        self.assertEqual(lines.count(insert.END_DELIMITER), 1)
        self.assertEqual(lines[-1], insert.END_DELIMITER)
        self.assertEqual(lines.count(insert.SANITIZED_END_DELIMITER), 1)

    def test_127_history_measurement_ignores_digest_with_sanitised_delimiter(self) -> None:
        """MM-127. Сообщение с выжимкой не искажает измерение истории."""

        body = f"строка выжимки\n{insert.END_DELIMITER}\nещё строка выжимки"
        block, _ = insert.build_memory_block(body, int(DEFAULTS["max_return_chars"]))
        metrics = session.measure_history(
            [
                {"role": "user", "content": "обычный вопрос пользователя"},
                {"role": "user", "content": block},
            ]
        )
        self.assertEqual(metrics.hist_msgs, 1)
        self.assertEqual(metrics.hist_chars, len("обычный вопрос пользователя"))



class CatchUpSelectionTests(RevisionTestCase):
    """128, 129, 133: бюджеты catch-up и выбор целевой сессии (§18.1)."""

    modes = {"mode_return": True}

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        with self.store.connection:
            self.store.session_upsert(PREVIOUS, "work", last_seen_at="2026-09-25T10:00:00+03:00")
        result = digest.build_digest(
            self.store, self.config, self.root, self.logger, PREVIOUS, "work"
        )
        self.assertTrue(result.created_ok)

    def _first_turn(self) -> session.TurnEvent:
        return session.TurnEvent(
            session_id=SESSION, task_id="task_1", turn_id="1", is_first_turn=True, cwd=CWD
        )

    def test_128_catch_up_does_not_start_when_budget_exceeds_remaining(self) -> None:
        """MM-128. Catch-up не стартует: остаток дедлайна меньше суббюджета."""

        self.prepare_previous()
        result = catchup.run_catch_up(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            self._first_turn(),
            deadline_remaining_ms=10,
        )
        self.assertFalse(result.completed)
        self.assertEqual(result.reason, catchup.REASON_BUDGET_EXHAUSTED)
        self.assertIsNone(result.compaction)
        self.assertIsNone(result.digest)
        self.assertEqual(self.store.suppressed_ids(), set())

    def test_129_return_runs_even_when_catch_up_deferred_by_budget(self) -> None:
        """MM-129. Возврат выполняется, даже если catch-up отложен по бюджету."""

        self.prepare_previous()
        self.close_store()
        catchup.run_catch_up(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            self._first_turn(),
            deadline_remaining_ms=10,
        )
        result = ret.perform_return(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            self._first_turn(),
            session.SessionState(),
            deadline_remaining_ms=10,
        )
        self.assertEqual(result.status, ret.STATUS_INSERTED)
        self.assertIn(insert.INTRO_LINE, result.text)

    def test_133_catch_up_target_is_the_digest_return_would_select(self) -> None:
        """MM-133. Catch-up строит дайджест той же сессии, что выберет Возврат."""

        self.prepare_previous()
        chosen = ret.select_last_digest(self.root, "work", SESSION, self.logger)
        self.assertIsNotNone(chosen)
        target = catchup.select_previous_session(
            self.store, self.root, "work", SESSION, self.logger
        )
        self.assertEqual(target, chosen.session_id)


class SubbudgetValidationTests(unittest.TestCase):
    """130: конфигурация отклоняет набор, где сумма суббюджетов больше дедлайна."""

    def test_130_subbudget_sum_over_deadline_is_rejected(self) -> None:
        """MM-130. Сумма суббюджетов больше hook_deadline_ms даёт config_invalid."""

        values = dict(DEFAULTS)
        values["memory_root"] = "C:/tmp/mm"
        values["return_min_budget_ms"] = 6000
        issues = validate(values)
        self.assertTrue(
            any("сумма суббюджетов" in issue for issue in issues),
            f"ожидалась проблема суммы суббюджетов, получено: {issues}",
        )
        values["return_min_budget_ms"] = DEFAULTS["return_min_budget_ms"]
        self.assertEqual(validate(values), [])


class DigestSelectionAfterRebuildTests(RevisionTestCase):
    """132: rebuild-digest старой сессии не делает её дайджест последним (§11.1)."""

    modes = {"mode_return": True}

    def test_132_rebuilt_old_digest_is_not_selected_as_last(self) -> None:
        """MM-132. Пересборка дайджеста старой сессии не меняет выбор Возврата."""

        self.add_turns(2, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.add_turns(2, session_id=SESSION, base=MOMENT)
        for session_id in (PREVIOUS, SESSION):
            built = digest.build_digest(
                self.store, self.config, self.root, self.logger, session_id, "work"
            )
            self.assertTrue(built.created_ok)
        chosen = ret.select_last_digest(self.root, "work", "current", self.logger)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.session_id, SESSION)

        old_path = digest.digest_path(self.root, "work", PREVIOUS)
        old_path.unlink()
        results = digest.rebuild_digests(
            self.store, self.config, self.root, self.logger, session_id=PREVIOUS
        )
        self.assertTrue(results[0].created_ok)
        self.assertEqual(
            digest.read_header(old_path).last_turn,
            (MOMENT - timedelta(days=1) + timedelta(minutes=5)).isoformat(timespec="seconds"),
        )
        after = ret.select_last_digest(self.root, "work", "current", self.logger)
        self.assertEqual(after.session_id, SESSION)



class SearchThresholdTests(RevisionTestCase):
    """144, 145: относительный отсев и `search --explain` (§12, §21.2)."""

    modes = {"mode_search": True, "search_score_threshold": 0.0, "search_score_ratio": 0.99}

    def setUp(self) -> None:
        super().setUp()
        self.add_turns(1, session_id=PREVIOUS, user="память журнал бэкап восстановление подробно")
        self.add_turns(1, session_id=SESSION, user="память журнал бэкап восстановление подробно")
        self.add_turns(
            1,
            session_id="s3",
            user="память журнал бэкап восстановление и много лишних слов разговор",
        )

    def test_144_relative_threshold_is_measured_from_best_score(self) -> None:
        """MM-144. Относительный порог считается от лучшего score выдачи."""

        plan_result = search.plan(self.store, self.config, "work", SESSION, "память журнал")
        self.assertGreater(len(plan_result.candidates), 1)
        top = max(item.score for item in plan_result.candidates)
        expected = top * float(self.config["search_score_ratio"])
        self.assertAlmostEqual(plan_result.threshold, expected, places=4)
        rejected = [item for item in plan_result.candidates if item.score < expected]
        self.assertTrue(rejected, "выдача должна содержать запись ниже относительного порога")
        for candidate in rejected:
            self.assertEqual(candidate.reject_reason, search.REJECT_BELOW_THRESHOLD)

    def test_145_explain_prints_scores_without_touching_counters(self) -> None:
        """MM-145. `--explain` печатает score и отсев, не меняя счётчики."""

        config_path = write_config(self.root, self.config)
        before = self.store.usage_all()
        returned_before = self.store.session_returned_ids("work", SESSION)
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--config",
                str(config_path),
                "search",
                "память",
                "журнал",
                "--explain",
                "--project",
                CWD,
            ],
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = completed.stdout.decode("utf-8")
        self.assertIn("попаданий: 3", output)
        self.assertIn("score=", output)
        self.assertTrue("отсев:" in output or "выдаётся" in output)
        self.assertEqual(self.store.usage_all(), before)
        self.assertEqual(self.store.session_returned_ids("work", SESSION), returned_before)



class ConfigHashAndInvariantTests(unittest.TestCase):
    """151, 152: отпечаток конфигурации и инварианты (§20.1, П-14)."""

    def test_151_config_hash_changes_with_parameter_and_keeps_journal(self) -> None:
        """MM-151. Правка поведенческого параметра меняет hash, журнал не трогает."""

        base = dict(DEFAULTS)
        changed = dict(DEFAULTS)
        changed["max_user_text"] = DEFAULTS["max_user_text"] + 1
        self.assertNotEqual(config_hash(base), config_hash(changed))

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(base), encoding="utf-8")
            loaded = load_config(config_path)
            journal_file = Path(tmp) / "2026-09-26.md"
            journal_file.write_text(
                "<!-- mm:begin -->\n- event_id: e_x\n<!-- mm:end -->\n", encoding="utf-8"
            )
            before = journal_file.read_bytes()

            config_path.write_text(json.dumps(changed), encoding="utf-8")
            reloaded = load_config(config_path)
            self.assertNotEqual(loaded.hash, reloaded.hash)
            self.assertEqual(journal_file.read_bytes(), before)

    def test_152_invariant_violation_reports_config_invalid(self) -> None:
        """MM-152. Нарушение инварианта даёт config_invalid и безопасное значение."""

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps({"memory_root": tmp, "compaction_unused_age": 400}),
                encoding="utf-8",
            )
            config = load_config(config_path)
        self.assertTrue(any(issue.startswith("config_invalid:") for issue in config.issues))
        self.assertEqual(config["compaction_unused_age"], DEFAULTS["compaction_unused_age"])


class ProjectPathAndSenderTests(RevisionTestCase):
    """155, 160: project_path в записи и sender_id/platform (П-16, П-18)."""

    def test_155_project_path_is_written_and_normalised(self) -> None:
        """MM-155. `project_path` пишется и нормализуется по правилам §8."""

        self.add_turns(1)
        record = journal.read_records(self.journal_file())[0][0]
        self.assertEqual(record.project, "work")
        self.assertEqual(record.metadata["project_path"], normalize_path(CWD))
        self.assertNotIn("\\", record.metadata["project_path"])
        self.assertFalse(record.metadata["project_path"].endswith("/"))

    def test_160_sender_and_platform_are_stored_and_do_not_affect_search(self) -> None:
        """MM-160. sender_id и platform сохраняются и не влияют на поиск."""

        data = capture.TurnData(
            user_message="реплика с отправителем",
            assistant_response="ответ с отправителем",
            session_id=SESSION,
            task_id="task_1",
            turn_id="1",
            cwd=CWD,
            sender_id="user-42",
            platform="telegram",
        )
        result = capture.capture_turn(data, self.config, self.logger, self.root, moment=MOMENT)
        record = journal.read_records(self.journal_file())[0][0]
        self.assertEqual(record.metadata["sender_id"], "user-42")
        self.assertEqual(record.metadata["platform"], "telegram")
        self.assertEqual(record.event_id, result.event_id)

        indexer.index_project(self.store, self.root, self.logger)
        hits = self.store.fts_search('"отправител"*')
        self.assertEqual([event_id for event_id, _ in hits], [result.event_id])
        plan_result = search.plan(self.store, self.config, "work", SESSION, "отправителе")
        self.assertEqual([item.event_id for item in plan_result.candidates], [result.event_id])


class StableEventIdTests(RevisionTestCase):
    """3: event_id стабилен при повторной доставке с изменённым содержимым."""

    def test_3_event_id_stable_while_content_hash_changes(self) -> None:
        """MM-3. event_id зависит от ключа хода, а не от содержимого."""

        first = capture.capture_turn(
            capture.TurnData(
                user_message="первая реплика",
                assistant_response="первый ответ",
                session_id=SESSION,
                task_id="task_1",
                turn_id="1",
                cwd=CWD,
            ),
            self.config,
            self.logger,
            self.root,
            moment=MOMENT,
        )
        again = capture.capture_turn(
            capture.TurnData(
                user_message="первая реплика",
                assistant_response="совсем другой ответ",
                session_id=SESSION,
                task_id="task_1",
                turn_id="1",
                cwd=CWD,
            ),
            self.config,
            self.logger,
            self.root,
            moment=MOMENT + timedelta(minutes=1),
        )
        self.assertEqual(first.event_id, again.event_id)
        self.assertNotEqual(first.content_hash, again.content_hash)
        # Оба физических экземпляра в журнале, каноническая запись одна (MM-2).
        records = journal.read_records(self.journal_file())[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record.event_id for record in records}), 1)


class MissingFmtTests(RevisionTestCase):
    """121: отсутствие `fmt` трактуется как fmt=1."""

    def test_121_record_without_fmt_reads_as_format_one(self) -> None:
        """MM-121. Запись без поля `fmt` читается как запись первой редакции."""

        metadata = {
            "timestamp": MOMENT.isoformat(timespec="seconds"),
            "project": "work",
            "project_path": normalize_path(CWD),
            "session_id": SESSION,
            "task_id": "task_1",
            "turn": "1",
            "turn_source": "hermes",
            "event_id": "e_nofmt00000000000000000000000000000",
            "revision": 1,
            "supersedes": "",
            "content_hash": canonical.content_hash("текст реплики", "текст ответа"),
            "redaction_set": "00000000",
            "body_status": "full",
            "truncated": "none",
            "sender_id": "",
            "platform": "",
        }
        target = paths.journal_file(self.root, "work", MOMENT.date())
        journal.append_record(
            target, canonical.render_record(metadata, "текст реплики", "текст ответа")
        )
        # Убираем строку `fmt` — запись становится записью редакции v1.6.
        text = target.read_text(encoding="utf-8").replace("- fmt: 1\n", "", 1)
        target.write_text(text, encoding="utf-8", newline="\n")

        records, damaged = journal.read_records(target)
        self.assertEqual(damaged, [])
        self.assertEqual(records[0].fmt, canonical.RECORD_FMT)
        self.assertNotIn("fmt", records[0].metadata)

        result = indexer.index_project(self.store, self.root, self.logger)
        self.assertEqual(result.records_indexed, 1)
        meta = self.store.meta_get(metadata["event_id"])
        self.assertEqual(meta["fmt"], canonical.RECORD_FMT)
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(report.format_newer, [])
        self.assertTrue(report.ok)

    def test_97_damaged_session_state_does_not_stop_hermes(self) -> None:
        """MM-97. Повреждённое состояние сессии не останавливает Hermes."""

        self.add_turns(1, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        with self.store.connection:
            self.store.set_meta(session.state_key("work", SESSION), "{это не json")

        turn = session.TurnEvent(
            session_id=SESSION, task_id="task_1", turn_id="1", is_first_turn=True, cwd=CWD
        )
        state = session.load_state(self.store, "work", SESSION)
        self.assertEqual(state, session.SessionState())
        result = ret.perform_return(
            self.store, self.config, self.root, self.logger, "work", turn, state
        )
        # Ход продолжается: пустое состояние не исключает выбор дайджеста.
        self.assertIn(result.status, (ret.STATUS_INSERTED, ret.STATUS_NO_DIGEST))
        self.assertEqual(result.session_id, SESSION)

    def test_99_record_without_content_hash_is_not_indexed_as_valid(self) -> None:
        """MM-99. Запись без content_hash не индексируется как валидная."""

        metadata = {
            "timestamp": MOMENT.isoformat(timespec="seconds"),
            "fmt": canonical.RECORD_FMT,
            "project": "work",
            "project_path": normalize_path(CWD),
            "session_id": SESSION,
            "task_id": "task_1",
            "turn": "1",
            "turn_source": "hermes",
            "event_id": "e_nohash0000000000000000000000000000",
            "revision": 1,
            "supersedes": "",
            "redaction_set": "00000000",
            "body_status": "full",
            "truncated": "none",
            "sender_id": "",
            "platform": "",
        }
        target = paths.journal_file(self.root, "work", MOMENT.date())
        journal.append_record(
            target, canonical.render_record(metadata, "текст реплики", "текст ответа")
        )
        indexer.index_project(self.store, self.root, self.logger)
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertFalse(report.ok)
        self.assertTrue(
            any(metadata["event_id"] in item for item in report.missing_in_index),
            f"запись без content_hash должна попасть в missing_in_index: {report.missing_in_index}",
        )


class FormatNewerTests(RevisionTestCase):
    """120: запись формата новее поддерживаемого индексируется с предупреждением."""

    def _record(self, fmt: int) -> str:
        metadata = {
            "timestamp": MOMENT.isoformat(timespec="seconds"),
            "fmt": fmt,
            "project": "work",
            "project_path": normalize_path(CWD),
            "session_id": SESSION,
            "task_id": "task_1",
            "turn": "1",
            "turn_source": "hermes",
            "event_id": f"e_fmt{fmt}000000000000000000000000000000"[:33],
            "revision": 1,
            "supersedes": "",
            "content_hash": canonical.content_hash("текст реплики", "текст ответа"),
            "redaction_set": "00000000",
            "body_status": "full",
            "truncated": "none",
            "sender_id": "",
            "platform": "",
        }
        return canonical.render_record(metadata, "текст реплики", "текст ответа")

    def test_120_newer_format_is_indexed_and_only_warned_about(self) -> None:
        """MM-120. `fmt` больше поддерживаемого: запись в индексе, verify — предупреждение."""

        target = paths.journal_file(self.root, "work", MOMENT.date())
        journal.append_record(target, self._record(canonical.RECORD_FMT + 1))

        result = indexer.index_project(self.store, self.root, self.logger)
        self.assertEqual(result.records_indexed, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.store.fts_count(), 1)

        report = indexer.verify(self.store, self.root, self.logger)
        self.assertTrue(report.ok, "предупреждение не должно делать проверку проблемной")
        self.assertEqual(len(report.format_newer), 1)
        self.assertIn("fmt=", report.format_newer[0])
        self.assertEqual(report.damaged, [])
        notes = " ".join(report.notes)
        self.assertIn("записи формата новее поддерживаемого: 1", notes)


class DerivedTurnListingTests(RevisionTestCase):
    """139: verify перечисляет записи с производными ключами хода."""

    def test_139_verify_lists_records_with_derived_turn(self) -> None:
        """MM-139. `verify` перечисляет записи с `turn_source=derived`."""

        result = capture.capture_turn(
            capture.TurnData(
                user_message="ход без номера",
                assistant_response="ответ без номера",
                session_id=SESSION,
                task_id="task_1",
                turn_id=None,
                cwd=CWD,
            ),
            self.config,
            self.logger,
            self.root,
            moment=MOMENT,
        )
        indexer.index_project(self.store, self.root, self.logger)
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(report.derived_turns, [result.event_id])
        self.assertIn(result.event_id, " ".join(report.notes))

        config_path = write_config(self.root, self.config)
        self.close_store()
        completed = subprocess.run(
            [sys.executable, str(CLI), "--config", str(config_path), "verify"],
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"производный ключ хода: {result.event_id}", completed.stdout.decode("utf-8"))


class JournalExternalModificationTests(RevisionTestCase):
    """150: внешнее переписывание файла журнала обнаруживается verify."""

    def test_150_external_rewrite_is_reported(self) -> None:
        """MM-150. `verify` сообщает `journal_externally_modified`."""

        self.add_turns(2)
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(report.externally_modified, [])

        path = self.journal_file()
        # Правка внешняя: длина файла не меняется, меняется только mtime.
        text = path.read_text(encoding="utf-8").replace("реплика", "РеПлика")
        path.write_text(text, encoding="utf-8", newline="\n")
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 120))

        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(len(report.externally_modified), 1)
        self.assertIn("journal_externally_modified", log_operations(self.tmp))
        self.assertIn("внешне переписанные файлы журнала: 1", " ".join(report.notes))


class VerifyProjectsTests(RevisionTestCase):
    """156: `verify --projects` сообщает о расхождении project_path и маппинга."""

    def test_156_verify_projects_reports_mapping_mismatch(self) -> None:
        """MM-156. Расхождение привязки перечисляется в `verify --projects`."""

        work_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work_dir, True)
        self.add_turns(1)
        report = indexer.verify(
            self.store, self.root, self.logger, project="work", project_mapping={CWD: "renamed"}
        )
        self.assertEqual(len(report.project_mismatch), 1)
        self.assertIn("work -> renamed", report.project_mismatch[0])

        config = make_config(self.tmp, project_mapping={str(work_dir): "work"})
        config_path = write_config(self.root, config)
        self.close_store()
        completed = subprocess.run(
            [sys.executable, str(CLI), "--config", str(config_path), "verify", "--projects"],
            capture_output=True,
            cwd=str(work_dir),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("расхождение project_path:", completed.stdout.decode("utf-8"))
        self.assertIn("project_mapping_changed", log_operations(self.tmp))




class ReprojectCommandTests(RevisionTestCase):
    """157: `re-project` применяет исправленную привязку с резервной копией."""

    def test_157_reproject_rebinds_with_backup_and_rebuilds_index(self) -> None:
        """MM-157. `re-project` правит журнал, делает копию и перестраивает индекс."""

        results = self.add_turns(2)
        self.assertTrue(all(item.written for item in results))
        path = self.journal_file()
        before = path.read_bytes()

        config = make_config(self.tmp, project_mapping={CWD: "renamed"})
        config_path = write_config(self.root, config)
        self.close_store()
        completed = subprocess.run(
            [sys.executable, str(CLI), "--config", str(config_path), "re-project", "--backup"],
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = completed.stdout.decode("utf-8")
        self.assertIn("резервная копия:", output)
        self.assertIn("перепривязано 2", output)

        after = path.read_text(encoding="utf-8")
        self.assertNotEqual(after.encode("utf-8"), before)
        self.assertEqual(after.count("- project: renamed"), 2)
        # Копия журнала сделана до правки и содержит исходную привязку.
        backups = list((self.root / "backups").glob("re-project-*"))
        self.assertEqual(len(backups), 1)
        copies = list(backups[0].rglob("*.md"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8").count("- project: work"), 2)
        # Индекс перестроен: записи числятся за новым проектом.
        self.assertEqual(self.store.meta_by_project("renamed")[0]["session_id"], SESSION)
        self.assertIn("project_mapping_changed", log_operations(self.tmp))


class SenderChangeTests(RevisionTestCase):
    """161: смена sender_id в пределах сессии логируется (П-18, §22)."""

    def _turn(self, turn_id: str, sender_id: str) -> capture.CaptureResult:
        return capture.capture_turn(
            capture.TurnData(
                user_message=f"реплика {turn_id}",
                assistant_response=f"ответ {turn_id}",
                session_id=SESSION,
                task_id=f"task_{turn_id}",
                turn_id=turn_id,
                cwd=CWD,
                sender_id=sender_id,
            ),
            self.config,
            self.logger,
            self.root,
            moment=MOMENT,
        )

    def test_161_sender_change_is_logged_and_stored_in_record(self) -> None:
        """MM-161. Смена отправителя даёт событие `sender_changed`."""

        self._turn("1", "user-1")
        self.assertNotIn("sender_changed", log_operations(self.tmp))
        self._turn("2", "user-1")
        self.assertNotIn("sender_changed", log_operations(self.tmp))
        self._turn("3", "user-2")
        self.assertIn("sender_changed", log_operations(self.tmp))

    def test_161_empty_sender_does_not_raise_event(self) -> None:
        """MM-161. Пустой `sender_id` события не порождает."""

        self._turn("1", "user-1")
        self._turn("2", "")
        self._turn("3", "")
        self._turn("4", "user-1")
        self.assertNotIn("sender_changed", log_operations(self.tmp))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

