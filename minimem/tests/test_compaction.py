"""Тесты этапа 6: Уплотнение.

Основание: ТЗ v1.7 §3.5, §3.7.7, §4.3, §9.1, §15, §15.1, §16, §19.2, §20,
§21.5, §22; план реализации v1.7, этап 6. Номера тестов соответствуют §23 ТЗ:
34–39, 43, 44, MM-54, MM-60, MM-64, MM-65, MM-79.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import itertools
import json
import random
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import capture, compaction, indexer, paths  # noqa: E402
from mm.config import DEFAULTS  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.store import Store  # noqa: E402

CLI = CODE_DIR / "minimem.py"
MOMENT = datetime.fromisoformat("2026-09-26T07:15:42+03:00")
SESSION = "2026-09-26-01"

#: Опорный «сейчас» для расчёта возраста записей.
NOW = datetime.now(timezone.utc)


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def make_config(tmp: str, **overrides) -> dict:
    config = dict(DEFAULTS)
    config["memory_root"] = tmp
    config["project_mapping"] = {
        "C:/projects/work": "work",
        "C:/projects/other": "other",
    }
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


class CompactionTestCase(unittest.TestCase):
    """Общая подготовка: хранилище, индекс, конфигурация, журнал."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = Path(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp)
        self.logger = make_logger(self.tmp)
        self.store = make_store(self.tmp)
        self.addCleanup(self.store.close)

    def add_turn(
        self,
        index: int = 1,
        moment: datetime | None = None,
        session: str = SESSION,
        user: str | None = None,
        answer: str | None = None,
        cwd: str = "C:/projects/work",
    ) -> str:
        """Записывает ход в журнал и прогоняет индексатор; возвращает event_id."""

        data = capture.TurnData(
            user_message=user if user is not None else f"реплика номер {index}",
            assistant_response=answer if answer is not None else f"ответ номер {index}",
            session_id=session,
            task_id=f"task_{index}",
            turn_id=str(index),
            cwd=cwd,
        )
        result = capture.capture_turn(
            data, self.config, self.logger, self.root, moment=moment or MOMENT
        )
        indexer.index_project(self.store, self.root, self.logger)
        return result.event_id

    def journal_text(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8") for path in paths.existing_journal_files(self.root)
        )

    def run_compaction(self, **kwargs) -> compaction.CompactionResult:
        return compaction.run_compaction(self.store, self.config, self.logger, **kwargs)

    def suppressed(self) -> dict[str, str]:
        return self.store.suppressed_map()

    def set_rebuilt_at(self, moment: datetime) -> None:
        with self.store.connection:
            self.store.set_meta("rebuilt_at", utc_stamp(moment))

    def put_meta(
        self,
        event_id: str,
        content_hash: str,
        timestamp_utc: str,
        turn: str,
        turn_numeric: int | None,
        project: str = "work",
    ) -> None:
        """Кладёт строку `memory_meta` напрямую — для проверки tie-breaker."""

        self.store.meta_upsert(
            {
                "event_id": event_id,
                "content_hash": content_hash,
                "project": project,
                "session_id": SESSION,
                "task_id": "task_tb",
                "turn": turn,
                "turn_numeric": turn_numeric,
                "timestamp": timestamp_utc[:10] + "T00:00:00+00:00",
                "timestamp_utc": timestamp_utc,
                "truncated": "none",
                "journal_file": "work/memory/2026-09-26.md",
                "journal_offset": 0,
                "revision": 1,
                "fmt": 1,
            }
        )
        self.store.fts_upsert(event_id, event_id, f"ответ {event_id}")


class CompactionRuleTests(CompactionTestCase):
    """Правила 1 и 3: возраст и точный дубль (тесты 34, 35, 37, 38, 43)."""

    def test_34_journal_records_are_not_deleted(self) -> None:
        """MM-34. Записи журнала не удаляются и не изменяются."""

        self.add_turn(1, moment=days_ago(200))
        self.add_turn(2, moment=days_ago(200), user="тот же текст", answer="тот же ответ")
        before = self.journal_text()
        meta_before = self.store.meta_count()
        fts_before = self.store.fts_count()

        result = self.run_compaction()
        self.assertTrue(result.ok)
        self.assertTrue(result.suppressions)
        self.assertEqual(self.journal_text(), before)
        self.assertEqual(self.store.meta_count(), meta_before)
        self.assertEqual(self.store.fts_count(), fts_before)
        self.assertEqual(len(paths.existing_journal_files(self.root)), 1)

    def test_35_suppressed_records_stored_in_service_layer(self) -> None:
        """MM-35. Снятые записи сохраняются в служебном слое."""

        old = self.add_turn(1, moment=days_ago(200))
        result = self.run_compaction()

        stored = self.suppressed()
        self.assertIn(old, stored)
        self.assertEqual(stored[old], compaction.REASON_AGE)
        self.assertEqual(self.store.suppressed_count(), len(result.suppressions))
        row = self.store.connection.execute(
            "SELECT reason, suppressed_at FROM suppressed_records WHERE event_id = ?", (old,)
        ).fetchone()
        self.assertTrue(row["suppressed_at"])
        self.assertIn(compaction.OPERATION_APPLIED, log_operations(self.tmp))

    def test_37_rules_are_deterministic(self) -> None:
        """MM-37. Механические правила работают детерминированно."""

        self.add_turn(1, moment=days_ago(200))
        self.add_turn(2, moment=days_ago(200), user="одинаковое", answer="содержимое")
        self.add_turn(3, moment=days_ago(200), user="одинаковое", answer="содержимое")

        first = self.run_compaction()
        snapshot = self.suppressed()
        second = self.run_compaction()

        self.assertEqual(first.event_ids(), second.event_ids())
        self.assertEqual(snapshot, self.suppressed())
        # Повторный прогон ничего не меняет: причина и время снятия прежние.
        self.assertEqual(second.already_suppressed, len(second.suppressions))
        self.assertEqual({item.reason for item in first.suppressions}, {compaction.REASON_AGE})

    def test_38_second_of_two_duplicates_is_suppressed(self) -> None:
        """MM-38. Из двух записей одного проекта с одинаковым content_hash
        снимается вторая."""

        first = self.add_turn(1, user="повторяющийся текст", answer="повторяющийся ответ")
        second = self.add_turn(2, user="повторяющийся текст", answer="повторяющийся ответ")

        self.assertEqual(
            self.store.meta_get(first)["content_hash"],
            self.store.meta_get(second)["content_hash"],
        )
        self.run_compaction()
        stored = self.suppressed()
        self.assertNotIn(first, stored)
        self.assertIn(second, stored)
        self.assertEqual(stored[second], compaction.REASON_DUPLICATE)

    def test_43_same_content_in_two_projects_is_not_duplicate(self) -> None:
        """MM-43. Одинаковое содержимое в разных проектах дублем не считается."""

        first = self.add_turn(1, user="общий текст", answer="общий ответ")
        second = self.add_turn(
            1,
            session="2026-09-26-02",
            user="общий текст",
            answer="общий ответ",
            cwd="C:/projects/other",
        )
        self.assertEqual(
            self.store.meta_get(first)["content_hash"],
            self.store.meta_get(second)["content_hash"],
        )

        self.run_compaction()
        self.assertEqual(self.suppressed(), {})

    def test_64_content_hash_not_recomputed_from_truncated_text(self) -> None:
        """MM-64. Уплотнение опирается на content_hash, вычисленный до обрезки."""

        self.config["max_user_text"] = 5
        first = self.add_turn(
            1, user="очень длинная реплика", answer="одинаковый ответ", moment=days_ago(200)
        )
        second = self.add_turn(
            2, user="другая длинная реплика", answer="одинаковый ответ", moment=days_ago(200)
        )
        self.assertNotEqual(
            self.store.meta_get(first)["content_hash"],
            self.store.meta_get(second)["content_hash"],
        )

        self.run_compaction()
        # Обрезанный текст совпадает, но хеши разные: дубля нет, снятие по возрасту.
        self.assertEqual(
            self.suppressed(), {first: compaction.REASON_AGE, second: compaction.REASON_AGE}
        )

    def test_65_rule3_keeps_records_with_different_hash(self) -> None:
        """MM-65. Правило 3 не снимает запись, если content_hash различается
        до обрезки."""

        self.config["max_assistant_text"] = 5
        first = self.add_turn(1, user="реплика", answer="длинный ответ агента один")
        second = self.add_turn(2, user="реплика", answer="длинный ответ агента два")
        self.assertNotEqual(
            self.store.meta_get(first)["content_hash"],
            self.store.meta_get(second)["content_hash"],
        )

        self.run_compaction()
        self.assertEqual(self.suppressed(), {})


class CompactionTieBreakerTests(CompactionTestCase):
    """Выбор первой записи группы дублей (тест MM-60)."""

    def kept(self, members: list[tuple[str, str, str, int | None]]) -> str:
        """Снимает группу одинаковых хешей и возвращает оставшийся event_id."""

        self.store.clear_suppressed()
        for event_id, timestamp, turn, numeric in members:
            self.put_meta(event_id, "c_same", timestamp, turn, numeric)
        self.run_compaction()
        return sorted({item[0] for item in members} - set(self.suppressed()))[0]

    def test_60_first_kept_by_timestamp_turn_and_event_id(self) -> None:
        """MM-60. Правило 3 выбирает первую запись по timestamp_utc,
        turn_numeric, turn, event_id."""

        stamp = utc_stamp(days_ago(1))
        later = utc_stamp(days_ago(0.5))
        # 1. Меньший timestamp_utc.
        self.assertEqual(self.kept([("e_a", later, "5", 5), ("e_b", stamp, "5", 5)]), "e_b")
        # 2. При равном времени — меньший turn_numeric, а не строка.
        self.assertEqual(self.kept([("e_a", stamp, "10", 10), ("e_b", stamp, "2", 2)]), "e_b")
        # 3. При равном turn_numeric — меньший turn как строка.
        self.assertEqual(self.kept([("e_a", stamp, "9", 9), ("e_b", stamp, "10", 9)]), "e_b")
        # 4. При полном равенстве — меньший event_id.
        self.assertEqual(self.kept([("e_b", stamp, "7", 7), ("e_a", stamp, "7", 7)]), "e_a")
        # 5. Отсутствие turn_numeric переводит сравнение на turn и event_id (§15).
        self.assertEqual(self.kept([("e_a", stamp, "7", None), ("e_b", stamp, "7", 7)]), "e_a")
        self.assertEqual(self.kept([("e_a", stamp, "8", None), ("e_b", stamp, "7", 7)]), "e_b")


    def test_60_tie_breaker_is_antisymmetric_and_order_independent(self) -> None:
        """Сравнение §15 симметрично, а выбор первой записи не зависит от порядка."""

        rows = [
            {
                "event_id": f"e_{index}",
                "timestamp_utc": utc_stamp(days_ago(1 + index % 3)),
                "turn": ["1", "10", "?", "07"][index % 4],
                "turn_numeric": [1, 10, None, 7][index % 4],
            }
            for index in range(12)
        ]
        for left, right in itertools.permutations(rows, 2):
            self.assertEqual(
                compaction._compare(left, right), -compaction._compare(right, left)
            )
        # Порядок группы не зависит от того, в каком порядке записи пришли.
        first = [item["event_id"] for item in sorted(rows, key=compaction.tie_break_key)]
        for seed in range(5):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(
                [item["event_id"] for item in sorted(shuffled, key=compaction.tie_break_key)],
                first,
            )

    def test_60_derived_turn_does_not_disturb_order(self) -> None:
        """Смешанная группа: числовые ходы и производные (`turn` = «?»).

        Инвариант индексатора: `turn_numeric` заполнен тогда и только тогда,
        когда `turn` — цифровая строка. Поэтому производные записи с `turn` = «?»
        оказываются в конце группы при сравнении по строке, и группа остаётся
        упорядоченной одинаково при любом порядке на входе.
        """

        rows = [
            {"event_id": "e_10", "timestamp_utc": utc_stamp(days_ago(1)), "turn": "10", "turn_numeric": 10},
            {"event_id": "e_2", "timestamp_utc": utc_stamp(days_ago(1)), "turn": "2", "turn_numeric": 2},
            {"event_id": "e_q1", "timestamp_utc": utc_stamp(days_ago(1)), "turn": "?", "turn_numeric": None},
            {"event_id": "e_q2", "timestamp_utc": utc_stamp(days_ago(1)), "turn": "?", "turn_numeric": None},
        ]
        orderings = set()
        for seed in range(6):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            orderings.add(
                tuple(item["event_id"] for item in sorted(shuffled, key=compaction.tie_break_key))
            )
        self.assertEqual(orderings, {("e_2", "e_10", "e_q1", "e_q2")})


class CompactionRule2Tests(CompactionTestCase):
    """Правило 2 и его отключающий флаг (тесты 39, 44, MM-79)."""

    def test_79_rule2_disabled_by_default(self) -> None:
        """MM-79. При compaction_rule2_enabled=false правило 2 не снимает записи."""

        self.set_rebuilt_at(days_ago(60))
        unused = self.add_turn(1, moment=days_ago(40))

        self.assertFalse(self.config["compaction_rule2_enabled"])
        self.run_compaction()
        self.assertEqual(self.suppressed(), {})

        # Та же запись снимается при включённом правиле 2.
        self.config["compaction_rule2_enabled"] = True
        self.run_compaction()
        self.assertEqual(self.suppressed(), {unused: compaction.REASON_UNUSED})

    def test_rule2_requires_record_created_after_rebuild(self) -> None:
        """Правило 2 работает только для записей, созданных после rebuilt_at."""

        self.config["compaction_rule2_enabled"] = True
        self.set_rebuilt_at(days_ago(60))
        before_rebuild = self.add_turn(1, moment=days_ago(100))
        after_rebuild = self.add_turn(2, moment=days_ago(40))

        self.run_compaction()
        stored = self.suppressed()
        self.assertNotIn(before_rebuild, stored)
        self.assertIn(after_rebuild, stored)
        self.assertEqual(stored[after_rebuild], compaction.REASON_UNUSED)

    def test_rule2_skips_used_records(self) -> None:
        """Использованная запись правилом 2 не снимается (§16)."""

        self.config["compaction_rule2_enabled"] = True
        self.set_rebuilt_at(days_ago(60))
        event_id = self.add_turn(1, moment=days_ago(40))
        with self.store.connection:
            self.store.bump_usage(event_id, utc_stamp(MOMENT))

        self.run_compaction()
        self.assertEqual(self.suppressed(), {})

    def test_39_rule2_does_not_apply_to_records_before_rebuild(self) -> None:
        """MM-39. После rebuild-index правило 2 не снимает записи, созданные
        до пересборки."""

        self.config["compaction_rule2_enabled"] = True
        unused = self.add_turn(1, moment=days_ago(40))
        used = self.add_turn(2, moment=days_ago(40))
        with self.store.connection:
            self.store.bump_usage(used, utc_stamp(MOMENT))
        self.set_rebuilt_at(days_ago(60))
        self.run_compaction()
        self.assertIn(unused, self.suppressed())

        indexer.rebuild_index(self.store, self.root, self.logger, config=self.config)
        self.assertNotIn(unused, self.suppressed())
        self.assertTrue(self.store.get_meta("rebuilt_at"))

    def test_44_rebuild_restores_rules_1_and_3_only(self) -> None:
        """MM-44. После rebuild-index записи, снятые по правилам 1 и 3, остаются
        снятыми; снятая только по правилу 2 возвращается в поиск."""

        self.config["compaction_rule2_enabled"] = True
        self.set_rebuilt_at(days_ago(60))
        by_age = self.add_turn(1, moment=days_ago(200))
        first_duplicate = self.add_turn(2, user="повтор", answer="повтор")
        second_duplicate = self.add_turn(3, user="повтор", answer="повтор")
        by_rule2 = self.add_turn(4, moment=days_ago(40))
        self.assertNotIn(first_duplicate, self.suppressed())

        self.run_compaction()
        before = self.suppressed()
        self.assertNotIn(first_duplicate, before)
        self.assertEqual(before[second_duplicate], compaction.REASON_DUPLICATE)
        self.assertEqual(before[by_age], compaction.REASON_AGE)
        self.assertEqual(before[by_rule2], compaction.REASON_UNUSED)

        indexer.rebuild_index(self.store, self.root, self.logger, config=self.config)
        after = self.suppressed()
        self.assertIn(by_age, after)
        self.assertNotIn(first_duplicate, after)
        self.assertIn(second_duplicate, after)
        self.assertNotIn(by_rule2, after)
        self.assertIn(compaction.OPERATION_REBUILT, log_operations(self.tmp))


class CompactionModeTests(CompactionTestCase):
    """Gate `mode_compaction` и его независимость от других режимов (§3.7.7)."""

    def test_54_disabled_mode_does_not_change_suppression_state(self) -> None:
        """MM-54. При mode_compaction=false существующие suppression state
        не изменяются."""

        self.add_turn(1, moment=days_ago(200))
        self.add_turn(2, user="одинаковое", answer="содержимое")
        self.add_turn(3, user="одинаковое", answer="содержимое")
        before = self.suppressed()

        self.assertFalse(self.config["mode_compaction"])
        self.assertFalse(compaction.auto_compaction_enabled(self.config))
        result = compaction.note_disabled(self.config, self.logger, session_id=SESSION)

        self.assertEqual(result.status, compaction.STATUS_DISABLED)
        self.assertEqual(self.suppressed(), before)
        self.assertIn(compaction.OPERATION_SKIPPED, log_operations(self.tmp))

    def test_disabled_mode_does_not_apply_new_suppressions(self) -> None:
        """Автоматическое уплотнение при mode_compaction=false не выполняется."""

        self.add_turn(1, moment=days_ago(200))
        if not compaction.auto_compaction_enabled(self.config):
            compaction.note_disabled(self.config, self.logger)
        self.assertEqual(self.suppressed(), {})

    def test_36_compaction_does_not_call_model_or_read_journal(self) -> None:
        """MM-36. Уплотнение не вызывает модель и не читает журнал."""

        source = (CODE_DIR / "mm" / "compaction.py").read_text(encoding="utf-8")
        forbidden = re.search(
            r"\b(openai|anthropic|requests|httpx|urllib|socket|subprocess|hermes)\b", source
        )
        self.assertIsNone(forbidden, f"уплотнение не должно обращаться к сети: {forbidden}")

        def forbidden_journal(*args, **kwargs):  # pragma: no cover - не должно сработать
            raise AssertionError("уплотнение не должно читать журнал")

        original = indexer.journal.read_records
        indexer.journal.read_records = forbidden_journal  # type: ignore[assignment]
        self.addCleanup(setattr, indexer.journal, "read_records", original)

        self.add_turn(1, moment=days_ago(200))
        result = self.run_compaction()
        self.assertTrue(result.ok)
        self.assertTrue(result.suppressions)


class CompactionConsumersTests(CompactionTestCase):
    """Снятые записи не возвращаются Поиском и не попадают в дайджест."""

    def test_suppressed_records_excluded_from_search_and_digest(self) -> None:
        """Снятые правилом 1 записи отсеиваются Поиском и Дайджестом (§12, §14)."""

        from mm import digest as digest_module
        from mm import search as search_module

        self.config["mode_search"] = True
        self.config["search_score_threshold"] = 0.0
        old = self.add_turn(1, moment=days_ago(200))
        self.add_turn(2, moment=MOMENT)
        self.run_compaction()
        self.assertIn(old, self.suppressed())

        plan = search_module.plan(
            self.store, self.config, "work", session_id="другая", text="реплика номер 1"
        )
        rejected = {c.event_id: c.reject_reason for c in plan.candidates}
        self.assertEqual(rejected.get(old), search_module.REJECT_SUPPRESSED)
        self.assertNotIn(old, {c.event_id for c in plan.selected})

        result = digest_module.build_digest(
            self.store, self.config, self.root, self.logger, SESSION, "work"
        )
        text = Path(result.path).read_text(encoding="utf-8")
        self.assertNotIn(f"event_id: {old}", text)


class CompactionCliTests(CompactionTestCase):
    """Команда `minimem compact` (§21.5, §3.7.7)."""

    def run_cli(self, argv: list[str]) -> subprocess.CompletedProcess:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(CLI), "--config", str(config_path), *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_cli_compact_runs_with_mode_disabled(self) -> None:
        """CLI compact выполняется при mode_compaction=false (MM-54, §3.5)."""

        self.add_turn(1, moment=days_ago(200))
        self.assertFalse(self.config["mode_compaction"])

        outcome = self.run_cli(["compact", "--dry-run"])
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertIn("пробный прогон", outcome.stdout)
        self.assertEqual(self.suppressed(), {})

        outcome = self.run_cli(["compact"])
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertIn("уплотнение", outcome.stdout)
        self.assertIn("правило 2: выключено", outcome.stdout)
        self.assertEqual(self.store.suppressed_count(), 1)

    def test_cli_compact_respects_rule2_flag(self) -> None:
        """CLI compact не включает правило 2 самостоятельно (MM-79, §15.1)."""

        self.set_rebuilt_at(days_ago(60))
        self.add_turn(1, moment=days_ago(40))
        outcome = self.run_cli(["compact"])
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertEqual(self.suppressed(), {})

    def test_cli_rebuild_index_records_rebuilt_at(self) -> None:
        """CLI rebuild-index пишет rebuilt_at и пересчитывает снятые (§15)."""

        self.add_turn(1, moment=days_ago(200))
        self.run_compaction()
        self.assertEqual(self.store.suppressed_count(), 1)

        outcome = self.run_cli(["rebuild-index"])
        self.assertEqual(outcome.returncode, 0, outcome.stdout + outcome.stderr)
        self.assertTrue(self.store.get_meta("rebuilt_at"))
        self.assertEqual(self.store.suppressed_count(), 1)

