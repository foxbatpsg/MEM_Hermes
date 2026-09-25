"""Тесты этапа 1: Захват и журнал.

Основание: ТЗ v1.7 §3.1, §4.4, §4.5, §5, §5.1, §6, §7, §10; план
реализации v1.7, этап 1. Номера тестов соответствуют §23 ТЗ.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import canonical, capture, insert, journal, paths, redaction  # noqa: E402
from mm.config import DEFAULTS  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.store import Store  # noqa: E402

MOMENT = datetime.fromisoformat("2026-09-26T07:15:42+03:00")


def make_config(tmp: str, **overrides) -> dict:
    config = dict(DEFAULTS)
    config["memory_root"] = tmp
    config["project_mapping"] = {}
    config.update(overrides)
    return config


def make_logger(tmp: str) -> Logger:
    return Logger(Path(tmp) / "logs" / "minimem.log")


def turn(**overrides) -> capture.TurnData:
    data = capture.TurnData(
        user_message="Привет, проверь журнал",
        assistant_response="Проверил, журнал в порядке",
        session_id="2026-09-26-01",
        task_id="task_1",
        turn_id="12",
        cwd="C:/projects/mem_hermes",
    )
    for key, value in overrides.items():
        setattr(data, key, value)
    return data


def run_capture(tmp: str, data: capture.TurnData, config: dict | None = None):
    config = config or make_config(tmp)
    logger = make_logger(tmp)
    return capture.capture_turn(data, config, logger, Path(tmp), moment=MOMENT)


def journal_file_of(tmp: str) -> Path:
    files = paths.existing_journal_files(Path(tmp))
    return files[0] if files else Path(tmp) / "нет"


def journal_text(tmp: str) -> str:
    return journal_file_of(tmp).read_text(encoding="utf-8")


def records_of(tmp: str) -> list[journal.ParsedRecord]:
    return journal.read_records(journal_file_of(tmp))[0]


def all_records(tmp: str) -> list[journal.ParsedRecord]:
    """Записи из всех дневных файлов всех проектов."""

    collected: list[journal.ParsedRecord] = []
    for path in paths.existing_journal_files(Path(tmp)):
        collected.extend(journal.read_records(path)[0])
    return collected


def log_operations(tmp: str) -> list[str]:
    log = Path(tmp) / "logs" / "minimem.log"
    if not log.exists():
        return []
    return [json.loads(line)["operation"] for line in log.read_text("utf-8").splitlines()]


class CaptureBasicsTests(unittest.TestCase):
    """Один завершённый ход создаёт одну запись журнала (тесты 1, 4-8, 13, 14)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_1_one_turn_creates_one_record(self) -> None:
        """MM-1. Один завершённый ход создаёт одну запись журнала."""

        result = run_capture(self.tmp, turn())
        self.assertTrue(result.written)
        records = records_of(self.tmp)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].damaged)

    def test_5_6_project_resolved_and_fallback(self) -> None:
        """MM-5. MM-6. Проект определяется по cwd, неизвестный попадает в common."""

        config = make_config(self.tmp, project_mapping={"C:/projects/mem_hermes": "work"})
        run_capture(self.tmp, turn(), config)
        run_capture(self.tmp, turn(cwd="C:/projects/other", turn_id="2"), config)
        projects = [record.project for record in all_records(self.tmp)]
        self.assertEqual(sorted(projects), ["common", "work"])

    def test_8_truncation_flag_written(self) -> None:
        """MM-8. Признак обрезки записан в записи."""

        config = make_config(self.tmp, max_user_text=10, max_assistant_text=10)
        result = run_capture(self.tmp, turn(), config)
        self.assertEqual(result.truncated, "both")
        self.assertEqual(records_of(self.tmp)[0].metadata["truncated"], "both")

    def test_14_journal_is_valid_markdown(self) -> None:
        """MM-14. Журнал остаётся валидным Markdown."""

        run_capture(self.tmp, turn())
        text = journal_text(self.tmp)
        self.assertTrue(text.startswith("<!-- mm:begin -->"))
        self.assertIn("## 2026-09-26T07:15:42+03:00 · turn 12", text)
        self.assertIn("### User", text)
        self.assertIn("### Assistant", text)
        self.assertNotIn("\r", text)

    def test_13_markers_and_markup_do_not_break_parsing(self) -> None:
        """MM-13. Запись с маркерами и разметкой разбирается без потери текста."""

        tricky = turn(
            user_message="## Заголовок\n<!-- mm:begin -->\n### User\nтекст",
            assistant_response="### Assistant\n<!-- mm:end -->\n---",
        )
        run_capture(self.tmp, tricky)
        records = records_of(self.tmp)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].damaged)
        self.assertEqual(records[0].user_utterance, tricky.user_message)
        self.assertEqual(records[0].assistant_answer, tricky.assistant_response)

    def test_7_identical_turns_get_distinct_event_ids_same_hash(self) -> None:
        """MM-7. Разные ходы дают разные event_id и одинаковый content_hash."""

        first = run_capture(self.tmp, turn(turn_id="1"))
        second = run_capture(self.tmp, turn(turn_id="2"))
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertEqual(first.content_hash, second.content_hash)
        changed = run_capture(self.tmp, turn(turn_id="3", assistant_response="другой ответ"))
        self.assertNotEqual(changed.content_hash, first.content_hash)

    def test_4_secrets_removed_before_write(self) -> None:
        """MM-4. Секреты удаляются до записи."""

        run_capture(
            self.tmp,
            turn(user_message="мой ключ sk-abcdefghijklmnopqrstuvwx", assistant_response="понял"),
        )
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx", journal_text(self.tmp))
        self.assertIn("[REDACTED:api_key]", journal_text(self.tmp))

    def test_101_event_id_stable_on_redelivery(self) -> None:
        """MM-101. event_id не зависит от содержимого при повторной доставке."""

        config = make_config(self.tmp)
        first = run_capture(self.tmp, turn(assistant_response="первый ответ"), config)
        again = run_capture(self.tmp, turn(assistant_response="второй ответ"), config)
        self.assertEqual(first.event_id, again.event_id)
        self.assertNotEqual(first.content_hash, again.content_hash)


class IdempotencyTests(unittest.TestCase):
    """Повторная доставка хода (тесты 2, MM-51, MM-61, MM-62, MM-102)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp)

    def test_2_same_turn_delivered_twice_creates_one_record(self) -> None:
        """MM-2. Повторная доставка не создаёт вторую каноническую запись."""

        run_capture(self.tmp, turn(), self.config)
        again = run_capture(self.tmp, turn(), self.config)
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(again.operation, "capture_duplicate_ignored")
        self.assertEqual(len(records_of(self.tmp)), 1)
        self.assertIn("capture_duplicate_ignored", log_operations(self.tmp))

    def test_61_conflicting_redelivery_keeps_single_canonical_record(self) -> None:
        """MM-61. Повторная доставка с другим содержимым не создаёт вторую запись."""

        run_capture(self.tmp, turn(), self.config)
        second = run_capture(self.tmp, turn(assistant_response="иной ответ"), self.config)
        self.assertEqual(second.status, "ok")
        records = records_of(self.tmp)
        self.assertEqual(len(records), 2)
        self.assertEqual([record.revision for record in records], [1, 2])
        self.assertEqual(records[1].metadata["supersedes"], records[0].event_id)

    def test_62_conflict_is_logged(self) -> None:
        """MM-62. Конфликт event_id логируется как capture_event_conflict."""

        run_capture(self.tmp, turn(), self.config)
        run_capture(self.tmp, turn(assistant_response="иной ответ"), self.config)
        operations = log_operations(self.tmp)
        self.assertIn("capture_event_conflict", operations)
        self.assertIn("capture_event_revision_added", operations)

    def test_162_both_versions_stay_in_journal(self) -> None:
        """MM-162. Обе версии остаются в журнале, актуальна последняя ревизия."""

        run_capture(self.tmp, turn(), self.config)
        run_capture(self.tmp, turn(assistant_response="иной ответ"), self.config)
        records = records_of(self.tmp)
        latest = max(records, key=lambda record: record.revision)
        self.assertEqual(latest.revision, 2)
        self.assertEqual(latest.assistant_answer, "иной ответ")

    def test_51_capture_disabled_writes_nothing(self) -> None:
        """MM-51. При mode_capture=false новые записи не создаются."""

        config = make_config(self.tmp, mode_capture=False)
        result = run_capture(self.tmp, turn(), config)
        self.assertEqual(result.operation, "capture_disabled")
        self.assertFalse(result.written)
        self.assertEqual(paths.existing_journal_files(Path(self.tmp)), [])
        self.assertIn("capture_disabled", log_operations(self.tmp))

    def test_114_disabling_capture_keeps_existing_journal(self) -> None:
        """MM-114. Переключение CAPTURE=OFF не изменяет существующий журнал."""

        run_capture(self.tmp, turn(), self.config)
        before = journal_text(self.tmp)
        run_capture(self.tmp, turn(turn_id="13"), make_config(self.tmp, mode_capture=False))
        self.assertEqual(journal_text(self.tmp), before)

    def test_102_capture_survives_unavailable_sqlite(self) -> None:
        """MM-102. При недоступном SQLite запись сохраняется, гард недоступен."""

        blocker = Path(self.tmp) / "minimem.db"
        blocker.write_text("это не база", encoding="utf-8")
        result = run_capture(self.tmp, turn(), self.config)
        self.assertTrue(result.written)
        self.assertFalse(result.guard_available)
        self.assertEqual(len(records_of(self.tmp)), 1)
        self.assertIn("capture_idempotency_guard_unavailable", log_operations(self.tmp))


class DamagedRecordTests(unittest.TestCase):
    """Повреждённые записи журнала (тесты MM-66, MM-67, MM-69, MM-148)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self.tmp) / "2026-09-26.md"

    def write(self, text: str) -> None:
        self.file.write_text(text, encoding="utf-8", newline="\n")

    def good_record(self, event: str, turn_number: str) -> str:
        return (
            "<!-- mm:begin -->\n"
            f"## 2026-09-26T07:15:42+03:00 · turn {turn_number}\n"
            "- fmt: 1\n"
            "- project: common\n"
            f"- session_id: s\n- task_id: t\n- turn: {turn_number}\n- turn_source: hermes\n"
            f"- event_id: {event}\n- revision: 1\n- supersedes: \n"
            f"- content_hash: c_{'0' * 32}\n- redaction_set: abc\n"
            "- body_status: full\n- truncated: none\n"
            "### User\nреплика\n"
            "### Assistant\nответ\n"
            "<!-- mm:end -->\n"
        )

    def test_66_partial_record_keeps_next_valid_record(self) -> None:
        """MM-66. Частичная запись не приводит к потере следующей корректной."""

        self.write(
            "<!-- mm:begin -->\n## 2026-09-26T07:00:00+03:00 · turn 1\n"
            "- event_id: e_partial\n### User\nобрыв\n"
            + self.good_record("e_good", "2")
        )
        records, damaged = journal.read_records(self.file)
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0].damaged)
        self.assertFalse(records[1].damaged)
        self.assertEqual(records[1].event_id, "e_good")
        # Новый mm:begin после незакрытой записи помечает её повреждённой (§4.4 п.11).
        self.assertEqual([item.reason for item in damaged], ["nested_begin"])

    def test_67_nested_begin_marks_previous_damaged(self) -> None:
        """MM-67. Вложенный mm:begin помечает предыдущую запись повреждённой."""

        self.write(
            "<!-- mm:begin -->\n- event_id: e_first\n### User\nраз\n"
            + self.good_record("e_second", "2")
        )
        records, damaged = journal.read_records(self.file)
        self.assertTrue(records[0].damaged)
        self.assertEqual(records[0].damaged_reason, "nested_begin")
        self.assertFalse(records[1].damaged)
        self.assertEqual([item.reason for item in damaged], ["nested_begin"])

    def test_69_damaged_record_does_not_stop_others(self) -> None:
        """MM-69. Повреждённая запись логируется, остальные разбираются."""

        self.write(
            "<!-- mm:begin -->\n- event_id: e_bad\nмусор без секций\n<!-- mm:end -->\n\n"
            + self.good_record("e_ok", "3")
        )
        records, damaged = journal.read_records(self.file)
        self.assertTrue(records[0].damaged)
        self.assertEqual(records[0].damaged_reason, "sections_order")
        self.assertFalse(records[1].damaged)
        self.assertEqual(records[1].event_id, "e_ok")
        self.assertTrue(damaged)

    def test_148_partial_append_is_reported_by_size_check(self) -> None:
        """MM-148. Незавершённая запись не считается корректной."""

        self.write(self.good_record("e_ok", "1") + "<!-- mm:begin -->\n- event_id: e_cut")
        records, _ = journal.read_records(self.file)
        self.assertEqual(len(records), 2)
        self.assertFalse(records[0].damaged)
        self.assertTrue(records[1].damaged)

    def test_100_unknown_metadata_key_is_not_damage(self) -> None:
        """MM-100. Неизвестный ключ метаданных повреждением не считается."""

        text = self.good_record("e_x", "1").replace(
            "- body_status: full\n", "- body_status: full\n- future_field: значение\n"
        )
        self.write(text)
        records, damaged = journal.read_records(self.file)
        self.assertFalse(records[0].damaged)
        self.assertEqual(records[0].extra_metadata["future_field"], "значение")
        self.assertEqual(damaged, [])

    def test_100_invalid_enum_is_metadata_error(self) -> None:
        """MM-100. Недопустимое truncated даёт ошибку метаданных, не повреждение."""

        self.write(self.good_record("e_x", "1").replace("- truncated: none", "- truncated: maybe"))
        records, damaged = journal.read_records(self.file)
        self.assertFalse(records[0].damaged)
        self.assertEqual(records[0].metadata_error, "truncated=maybe")
        self.assertEqual([item.reason for item in damaged], ["metadata_invalid"])


class RedactionTests(unittest.TestCase):
    """Redaction секретов (тесты MM-85, MM-89, MM-106, MM-107, MM-134, MM-135)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_85_typical_api_keys_replaced(self) -> None:
        """MM-85. Типовые API-ключи заменяются placeholder'ами до записи."""

        samples = {
            "sk-abcdefghijklmnopqrstuvwxyz012345": "[REDACTED:api_key]",
            "sk-ant-api03-abcdefghijklmnopqrstuvwxyz": "[REDACTED:anthropic_key]",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789": "[REDACTED:access_token]",
            "xoxb-1234567890-abcdefghij": "[REDACTED:access_token]",
            "AIzaSyA1234567890abcdefghijklmnopqrstuvw": "[REDACTED:api_key]",
            "AKIAIOSFODNN7EXAMPLE": "[REDACTED:api_key]",
        }
        for secret, placeholder in samples.items():
            with self.subTest(secret=secret[:8]):
                result = redaction.redact(f"вот ключ {secret} конец")
                self.assertIn(placeholder, result.text)
                self.assertNotIn(secret, result.text)

    def test_85b_bearer_jwt_pem_connection_string(self) -> None:
        """MM-85. Bearer, JWT, PEM и connection string заменяются."""

        cases = {
            f"Authorization: Bearer {'a' * 20}": "[REDACTED:bearer]",
            "eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM": "[REDACTED:jwt]",
            "postgresql://user:secretpass@host/db": "[REDACTED:connection_string]",
        }
        for text, placeholder in cases.items():
            with self.subTest(placeholder=placeholder):
                self.assertIn(placeholder, redaction.redact(text).text)
        pem = redaction.redact(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        )
        self.assertIn("[REDACTED:private_key]", pem.text)
        self.assertNotIn("MIIabc", pem.text)

    def test_85c_key_value_forms(self) -> None:
        """MM-85. Формы password=..., token=..., secret=... заменяются."""

        result = redaction.redact("password=hunter2 secret=abc123 token: zzz999")
        self.assertIn("[REDACTED:password]", result.text)
        self.assertIn("[REDACTED:admin_secret]", result.text)
        self.assertIn("[REDACTED:access_token]", result.text)

    def test_86_plain_word_password_is_kept(self) -> None:
        """MM-86. Обычный текст со словом password без значения не удаляется."""

        text = "я забыл свой password, подскажи"
        result = redaction.redact(text)
        self.assertEqual(result.text, text)
        self.assertEqual(result.total, 0)

    def test_106_placeholder_is_deterministic(self) -> None:
        """MM-106. Redaction заменяет секреты детерминированными placeholder'ами."""

        first = redaction.redact("ключ sk-abcdefghijklmnopqrstuvwxyz")
        second = redaction.redact("другой текст, ключ sk-abcdefghijklmnopqrstuvwxyz")
        self.assertIn("[REDACTED:api_key]", first.text)
        self.assertIn("[REDACTED:api_key]", second.text)
        self.assertEqual(first.redaction_set, second.redaction_set)
        self.assertEqual(len(first.redaction_set), 8)
        self.assertEqual(first.counts, second.counts)

    def test_107_same_secret_same_hash(self) -> None:
        """MM-107. Повторная доставка того же секрета даёт тот же content_hash."""

        config = make_config(self.tmp)
        secret_answer = "токен ghp_abcdefghijklmnopqrstuvwxyz01"
        first = run_capture(self.tmp, turn(assistant_response=secret_answer), config)
        second = run_capture(self.tmp, turn(turn_id="13", assistant_response=secret_answer), config)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz01", journal_text(self.tmp))

    def test_89_no_secret_fragments_in_log(self) -> None:
        """MM-89. В логах redaction не появляются фрагменты секретов."""

        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        run_capture(self.tmp, turn(user_message=f"ключ {secret}"))
        log_text = (Path(self.tmp) / "logs" / "minimem.log").read_text(encoding="utf-8")
        self.assertNotIn(secret, log_text)
        self.assertIn("capture_record_written", log_text)

    def _with_broken_redaction(self, action) -> None:
        original = redaction.redact_pair

        def boom(*args, **kwargs):
            raise RuntimeError("сбой redaction")

        redaction.redact_pair = boom  # type: ignore[assignment]
        try:
            action()
        finally:
            redaction.redact_pair = original  # type: ignore[assignment]

    def test_134_redaction_failure_withholds_body(self) -> None:
        """MM-134. Сбой redaction приводит к body_status=withheld."""

        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        holder: list = []

        def run() -> None:
            holder.append(
                run_capture(self.tmp, turn(user_message=f"ключ {secret}"), make_config(self.tmp))
            )

        self._with_broken_redaction(run)
        self.assertEqual(holder[0].body_status, "withheld")
        record = records_of(self.tmp)[0]
        self.assertEqual(record.body_status, "withheld")
        self.assertEqual(record.user_utterance, "")
        self.assertNotIn(secret, journal_text(self.tmp))
        self.assertIn("record_body_withheld_redaction_failed", log_operations(self.tmp))

    def test_135_redaction_timeout_withholds_body(self) -> None:
        """MM-135. Таймаут redaction приводит к тому же поведению."""

        ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        with self.assertRaises(redaction.RedactionTimeout):
            redaction.redact("ключ sk-abcdefghijklmnopqrstuvwxyz", timeout_ms=200, clock=lambda: next(ticks))

        original = redaction.redact_pair

        def timeout(*args, **kwargs):
            raise redaction.RedactionTimeout("превышен redaction_timeout_ms")

        redaction.redact_pair = timeout  # type: ignore[assignment]
        try:
            result = run_capture(self.tmp, turn(), make_config(self.tmp))
        finally:
            redaction.redact_pair = original  # type: ignore[assignment]
        self.assertEqual(result.body_status, "withheld")
        self.assertEqual(records_of(self.tmp)[0].body_status, "withheld")

    def test_136_traceback_not_in_main_log(self) -> None:
        """MM-136. Traceback с телом реплики не попадает в основной лог."""

        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        self._with_broken_redaction(
            lambda: run_capture(self.tmp, turn(user_message=f"ключ {secret}"), make_config(self.tmp))
        )
        log_text = (Path(self.tmp) / "logs" / "minimem.log").read_text(encoding="utf-8")
        self.assertNotIn("Traceback", log_text)
        self.assertNotIn(secret, log_text)


class MemoryBlockStripTests(unittest.TestCase):
    """Удаление блока памяти из реплики (П-01, тесты MM-116, MM-117, MM-118)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def block(self, body: str = "**Вы:** старое\n**Агент:** прошлое") -> str:
        return (
            f"{insert.INTRO_LINE}\n\n{insert.START_DELIMITER}\n{body}\n{insert.END_DELIMITER}\n"
        )

    def test_116_block_removed_and_hash_matches_clean_text(self) -> None:
        """MM-116. Реплика сохраняется без блока, content_hash совпадает с чистой."""

        message = "вопрос пользователя\n" + self.block() + "\nвопрос пользователя"
        result = run_capture(self.tmp, turn(user_message=message))
        record = records_of(self.tmp)[0]
        self.assertEqual(record.user_utterance, "вопрос пользователя\n\nвопрос пользователя")
        self.assertNotIn(insert.START_DELIMITER, record.user_utterance)
        self.assertIn("insert_stripped", log_operations(self.tmp))
        self.assertEqual(
            result.content_hash,
            canonical.content_hash(
                "вопрос пользователя\n\nвопрос пользователя", turn().assistant_response
            ),
        )

    def test_117_strip_does_not_change_answer_or_truncated(self) -> None:
        """MM-117. Факт удаления блока не меняет ответ и не влияет на truncated."""

        plain = run_capture(self.tmp, turn(), make_config(self.tmp))
        self.tmp2 = tempfile.TemporaryDirectory()
        other = self.tmp2.name
        with_block = run_capture(other, turn(user_message="вопрос\n" + self.block()))
        self.addCleanup(self.tmp2.cleanup)
        self.assertEqual(plain.truncated, with_block.truncated)
        self.assertEqual(
            records_of(other)[0].assistant_answer, records_of(self.tmp)[0].assistant_answer
        )

    def test_118_delimiter_without_intro_is_kept(self) -> None:
        """MM-118. Реплика, начинающаяся с делимитера, но не блок, сохраняется."""

        message = f"{insert.START_DELIMITER}\nне блок памяти\n{insert.END_DELIMITER}"
        result = run_capture(self.tmp, turn(user_message=message))
        self.assertEqual(records_of(self.tmp)[0].user_utterance, message)
        self.assertEqual(result.content_hash, canonical.content_hash(message, turn().assistant_response))

    def test_nested_fake_delimiters_do_not_create_second_block(self) -> None:
        """Вложенные ложные делимитеры внутри блока второй блок не создают (П-05)."""

        body = f"**Агент:** {insert.SANITIZED_START_DELIMITER}\nещё текст"
        message = (
            "хвост\n"
            f"{insert.INTRO_LINE}\n{insert.START_DELIMITER}\n{body}\n{insert.END_DELIMITER}"
        )
        result = insert.strip_memory_block(message)
        self.assertEqual(result.blocks, 1)
        self.assertEqual(result.text, "хвост\n")

    def test_unclosed_block_is_not_removed(self) -> None:
        """Незакрытый блок не вырезается: реплика пользователя не теряется."""

        message = f"{insert.INTRO_LINE}\n{insert.START_DELIMITER}\nбез конца"
        result = insert.strip_memory_block(message)
        self.assertEqual(result.blocks, 0)
        self.assertTrue(result.malformed)
        self.assertEqual(result.text, message)

    def test_sanitized_delimiters_are_recognised(self) -> None:
        """Распознаются и sanitized-делимитеры (§10 п.3)."""

        message = (
            f"{insert.INTRO_LINE}\n{insert.SANITIZED_START_DELIMITER}\nданные\n"
            f"{insert.SANITIZED_END_DELIMITER}\nосталось"
        )
        result = insert.strip_memory_block(message)
        self.assertEqual(result.blocks, 1)
        self.assertEqual(result.text, "осталось")


class DerivedIdentityTests(unittest.TestCase):
    """Отсутствие идентификаторов хода (П-09, тесты MM-137, MM-138)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_137_missing_turn_id_still_saved(self) -> None:
        """MM-137. Отсутствие turn_id не приводит к потере хода."""

        result = run_capture(self.tmp, turn(turn_id=None))
        record = records_of(self.tmp)[0]
        self.assertTrue(result.written)
        self.assertEqual(record.metadata["turn_source"], "derived")
        self.assertTrue(record.metadata["turn"].startswith(canonical.DERIVED_TURN))
        self.assertIn("capture_turn_derived", log_operations(self.tmp))

    def test_138_turn_counters_present_in_log(self) -> None:
        """MM-138. Счётчики turns_* присутствуют в логе каждого запуска Захвата."""

        run_capture(self.tmp, turn(turn_id=None))
        lines = (Path(self.tmp) / "logs" / "minimem.log").read_text("utf-8").splitlines()
        payload = json.loads(lines[-1])
        for field in ("turns_seen", "turns_captured", "turns_skipped", "turns_derived"):
            self.assertIn(field, payload)
        self.assertEqual(payload["turns_derived"], 1)

    def test_missing_session_id_uses_unknown_prefix(self) -> None:
        """П-09: при отсутствии session_id используется сессия unknown-<дата>."""

        run_capture(self.tmp, turn(session_id=""))
        record = records_of(self.tmp)[0]
        self.assertTrue(record.session_id.startswith("unknown-2026-09-26"))

    def test_normal_turn_source_is_hermes(self) -> None:
        """При наличии turn_id и session_id turn_source = hermes (§10)."""

        run_capture(self.tmp, turn())
        self.assertEqual(records_of(self.tmp)[0].metadata["turn_source"], "hermes")


class SizeLimitTests(unittest.TestCase):
    """Ограничения размера записи (§6, тесты MM-63, MM-64, MM-105)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_105_record_respects_max_memory_record(self) -> None:
        """MM-105. Запись дополнительно урезается и не теряет метаданные."""

        config = make_config(
            self.tmp, max_user_text=2000, max_assistant_text=2000, max_memory_record=600
        )
        result = run_capture(
            self.tmp, turn(user_message="я" * 900, assistant_response="о" * 900), config
        )
        record = records_of(self.tmp)[0]
        self.assertLessEqual(len(journal_text(self.tmp).encode("utf-8")) - 1, 600)
        self.assertEqual(result.truncated, "both")
        self.assertEqual(record.metadata["truncated"], "both")
        self.assertTrue(record.event_id)
        self.assertTrue(record.content_hash)

    def test_105_metadata_is_never_dropped(self) -> None:
        """Метаданные сохраняются, даже если тело пришлось урезать полностью."""

        config = make_config(
            self.tmp, max_user_text=2000, max_assistant_text=2000, max_memory_record=400
        )
        run_capture(
            self.tmp, turn(user_message="я" * 900, assistant_response="о" * 900), config
        )
        record = records_of(self.tmp)[0]
        self.assertEqual(record.user_utterance, "")
        self.assertEqual(record.assistant_answer, "")
        self.assertTrue(record.content_hash)
        self.assertEqual(record.metadata["truncated"], "both")

    def test_metadata_survives_truncation(self) -> None:
        """Метаданные сохраняются полностью при обрезке тела (§6 п.3)."""

        config = make_config(self.tmp, max_user_text=5, max_assistant_text=5)
        run_capture(
            self.tmp, turn(user_message="я" * 200, assistant_response="о" * 200), config
        )
        record = records_of(self.tmp)[0]
        for field in ("fmt", "project", "session_id", "task_id", "turn", "event_id", "content_hash"):
            self.assertTrue(record.metadata.get(field), field)

    def test_63_hash_uses_full_content_not_truncated(self) -> None:
        """MM-63. Разные записи, ставшие одинаковыми после обрезки, различаются."""

        config = make_config(self.tmp, max_assistant_text=5)
        first = run_capture(
            self.tmp, turn(turn_id="1", assistant_response="одинаковый ответ"), config
        )
        second = run_capture(
            self.tmp, turn(turn_id="2", assistant_response="одинаковый другой"), config
        )
        self.assertNotEqual(first.content_hash, second.content_hash)
        records = records_of(self.tmp)
        self.assertEqual(records[0].assistant_answer, records[1].assistant_answer)
        self.assertNotEqual(records[0].content_hash, records[1].content_hash)

    def test_64_stored_hash_is_not_recomputed_from_body(self) -> None:
        """MM-64. content_hash не пересчитывается из обрезанного текста."""

        config = make_config(self.tmp, max_assistant_text=5)
        result = run_capture(self.tmp, turn(assistant_response="очень длинный ответ"), config)
        record = records_of(self.tmp)[0]
        self.assertEqual(record.content_hash, result.content_hash)
        self.assertNotEqual(
            record.content_hash,
            canonical.content_hash(record.user_utterance, record.assistant_answer),
        )


class JournalWriteTests(unittest.TestCase):
    """Атомарная запись и разбор (§4.5, тесты MM-68, MM-149)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self.tmp) / "2026-09-26.md"

    def sample(self, index: int) -> str:
        metadata = {
            "timestamp": "2026-09-26T07:15:42+03:00",
            "fmt": 1,
            "project": "common",
            "project_path": "",
            "session_id": "s",
            "task_id": "",
            "turn": str(index),
            "turn_source": "hermes",
            "event_id": f"e_{index}",
            "revision": 1,
            "supersedes": "",
            "content_hash": f"c_{index}",
            "redaction_set": "abcd1234",
            "truncated": "none",
            "sender_id": "",
            "platform": "",
        }
        return canonical.render_record(
            metadata, f"реплика {index}", f"ответ {index}"
        )

    def test_records_separated_by_blank_line(self) -> None:
        journal.append_record(self.file, self.sample(1))
        journal.append_record(self.file, self.sample(2))
        text = self.file.read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- mm:begin -->"), 2)
        self.assertIn("<!-- mm:end -->\n\n<!-- mm:begin -->", text)
        records, damaged = journal.read_records(self.file)
        self.assertEqual(len(records), 2)
        self.assertEqual(damaged, [])

    def test_offsets_point_to_record_start(self) -> None:
        first = journal.append_record(self.file, self.sample(1))
        second = journal.append_record(self.file, self.sample(2))
        records, _ = journal.read_records(self.file)
        self.assertEqual([record.offset for record in records], [first, second])

    def test_68_writes_do_not_mix(self) -> None:
        """MM-68. Последовательные записи не перемешиваются."""

        for index in range(5):
            journal.append_record(self.file, self.sample(index))
        records, damaged = journal.read_records(self.file)
        self.assertEqual(damaged, [])
        self.assertEqual(
            [record.user_utterance for record in records],
            [f"реплика {index}" for index in range(5)],
        )

    def test_149_damaged_key_is_stable_across_reads(self) -> None:
        """MM-149. Повторные обнаружения той же связки не порождают новых событий."""

        self.file.write_text(
            "<!-- mm:begin -->\n- event_id: e_bad\nтекст\n" + self.sample(1),
            encoding="utf-8",
            newline="\n",
        )
        _, first = journal.read_records(self.file)
        _, second = journal.read_records(self.file)
        self.assertEqual(
            [(item.offset, item.reason) for item in first],
            [(item.offset, item.reason) for item in second],
        )

    def test_body_escaping_roundtrip(self) -> None:
        """Строки тела с `<` и секциями экранируются и читаются (§4.4 п.8-9)."""

        user_text = "<div>html</div>\n### User\nобычный"
        answer = "<!-- mm:end -->"
        text = canonical.render_record(
            {"timestamp": "2026-09-26T07:15:42+03:00", "turn": "1", "truncated": "none"},
            user_text,
            answer,
        )
        self.assertIn("\\<div>html</div>", text)
        self.assertIn("\\### User", text)
        records, _ = journal.parse_records(text + "\n")
        self.assertEqual(records[0].user_utterance, user_text)
        self.assertEqual(records[0].assistant_answer, answer)


class PostHookTests(unittest.TestCase):
    """Хук post_llm_call: тонкий скрипт, всегда код 0 (§10, §19.2, §26.2)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.config_file = Path(self.tmp) / "config.json"
        self.config_file.write_text(
            json.dumps({"memory_root": self.tmp, "project_mapping": {}}, ensure_ascii=False),
            encoding="utf-8",
        )

    def run_hook(self, payload: dict) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["MINIMEM_CONFIG"] = str(self.config_file)
        return subprocess.run(
            [sys.executable, str(CODE_DIR / "hooks" / "post_llm.py")],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )

    def event(self, **extra) -> dict:
        return {
            "session_id": "2026-09-26-01",
            "cwd": "C:/projects/mem_hermes",
            "extra": {
                "task_id": "task_1",
                "turn_id": "7",
                "user_message": "вопрос из хука",
                "assistant_response": "ответ хука",
                **extra,
            },
        }

    def test_hook_writes_record_and_returns_empty_object(self) -> None:
        result = self.run_hook(self.event())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{}")
        records = all_records(self.tmp)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].user_utterance, "вопрос из хука")

    def test_hook_survives_broken_payload(self) -> None:
        env = dict(os.environ)
        env["MINIMEM_CONFIG"] = str(self.config_file)
        result = subprocess.run(
            [sys.executable, str(CODE_DIR / "hooks" / "post_llm.py")],
            input="{это не json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "{}")

    def test_hook_respects_capture_mode_off(self) -> None:
        self.config_file.write_text(
            json.dumps(
                {"memory_root": self.tmp, "mode_capture": False}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        result = self.run_hook(self.event())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(paths.existing_journal_files(Path(self.tmp)), [])
        self.assertIn("capture_disabled", log_operations(self.tmp))





