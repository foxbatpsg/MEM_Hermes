"""Тесты этапа 2: Индексатор и служебный слой.

Основание: ТЗ v1.7 §3.6, §4.2, §4.3, §9, §9.1, §9.2, §21.5; план
реализации v1.7, этап 2. Номера тестов соответствуют §23 ТЗ.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import canonical, capture, indexer, journal, paths  # noqa: E402
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


def make_store(tmp: str) -> Store:
    store = Store(Path(tmp) / "minimem.db")
    store.create_schema()
    return store


def capture_turn(tmp: str, index: int, config: dict, **overrides) -> capture.CaptureResult:
    data = capture.TurnData(
        user_message=overrides.get("user", f"реплика номер {index}"),
        assistant_response=overrides.get("answer", f"ответ номер {index}"),
        session_id=overrides.get("session", f"s{index}"),
        task_id=f"task_{index}",
        turn_id=str(index),
        cwd="C:/projects/work",
    )
    return capture.capture_turn(data, config, make_logger(tmp), Path(tmp), moment=MOMENT)


class IndexerTestCase(unittest.TestCase):
    """Общая подготовка: временное хранилище и служебный слой."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = Path(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp)
        self.logger = make_logger(self.tmp)
        self.store = make_store(self.tmp)
        self.addCleanup(self.store.close)

    def journal_path(self) -> Path:
        return paths.existing_journal_files(self.root)[0]

    def run_index(self, **kwargs) -> indexer.IndexResult:
        return indexer.index_project(self.store, self.root, self.logger, **kwargs)


class IndexBasicsTests(IndexerTestCase):
    """Индексация записей журнала (тесты 9, 10, 11, 16, MM-57, MM-70)."""

    def test_9_new_record_appears_in_fts_and_meta(self) -> None:
        """MM-9. Новая запись появляется в FTS5 и memory_meta."""

        result = capture_turn(self.tmp, 1, self.config)
        indexed = self.run_index()
        self.assertEqual(indexed.records_indexed, 1)
        self.assertEqual(self.store.meta_count(), 1)
        self.assertEqual(self.store.fts_count(), 1)
        meta = self.store.meta_get(result.event_id)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["project"], "common")
        self.assertEqual(meta["turn"], "1")
        self.assertEqual(meta["truncated"], "none")
        self.assertEqual(self.store.fts_search("реплика"), [(result.event_id, self.store.fts_search("реплика")[0][1])])

    def test_57_sqlite_keeps_all_identifiers(self) -> None:
        """MM-57. В SQLite хранятся session_id, task_id, turn и truncated."""

        result = capture_turn(self.tmp, 5, self.config)
        self.run_index()
        meta = self.store.meta_get(result.event_id)
        self.assertEqual(meta["session_id"], "s5")
        self.assertEqual(meta["task_id"], "task_5")
        self.assertEqual(meta["turn"], "5")
        self.assertEqual(meta["turn_numeric"], 5)
        self.assertEqual(meta["truncated"], "none")

    def test_11_rebuild_after_deleting_db_restores_index(self) -> None:
        """MM-11. После удаления SQLite rebuild-index полностью восстанавливает индекс."""

        capture_turn(self.tmp, 1, self.config)
        capture_turn(self.tmp, 2, self.config)
        self.run_index()
        self.store.close()
        (self.root / "minimem.db").unlink()
        for suffix in ("-wal", "-shm"):
            side = self.root / f"minimem.db{suffix}"
            if side.exists():
                side.unlink()

        fresh = make_store(self.tmp)
        self.addCleanup(fresh.close)
        result = indexer.rebuild_index(fresh, self.root, self.logger)
        self.assertEqual(result.records_indexed, 2)
        self.assertEqual(fresh.meta_count(), 2)
        self.assertEqual(fresh.fts_count(), 2)

    def test_70_cursor_avoids_full_read(self) -> None:
        """MM-70. Индексатор использует сохранённый курсор и не читает файл целиком."""

        for index in range(3):
            capture_turn(self.tmp, index, self.config)
        first = self.run_index()
        self.assertEqual(first.records_indexed, 3)
        self.assertEqual(first.records_scanned, 3)

        second = self.run_index()
        self.assertEqual(second.records_scanned, 0)
        self.assertEqual(second.records_indexed, 0)
        cursors = self.store.cursor_all()
        self.assertEqual(len(cursors), 1)
        self.assertEqual(cursors[0]["byte_offset"], self.journal_path().stat().st_size)

    def test_15_skipped_record_is_added_on_next_run(self) -> None:
        """MM-15. Пропущенную запись дописывает следующий запуск (курсор не продвинут)."""

        capture_turn(self.tmp, 1, self.config)
        self.run_index()
        capture_turn(self.tmp, 2, self.config)

        key = paths.relative_to_root(self.journal_path(), self.root)
        cursor = self.store.cursor_get(key)
        with self.store.connection:
            self.store.cursor_upsert(
                key, cursor["byte_offset"], cursor["file_size"], cursor["mtime"], "2026-09-26T00:00:00+03:00"
            )
        first = self.run_index()
        self.assertEqual(first.records_indexed, 1)
        second = self.run_index()
        self.assertEqual(second.records_indexed, 0)
        self.assertEqual(self.store.meta_count(), 2)

    def test_16_verify_finds_journal_record_missing_in_index(self) -> None:
        """MM-16. verify находит запись журнала, отсутствующую в индексе."""

        capture_turn(self.tmp, 1, self.config)
        self.run_index()
        capture_turn(self.tmp, 2, self.config)
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(len(report.missing_in_index), 1)
        self.assertFalse(report.ok)
        self.run_index()
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertTrue(report.ok)


class DuplicateAndRevisionTests(IndexerTestCase):
    """Дубликаты и ревизии (тесты MM-62, MM-103, MM-115, MM-162, MM-163, MM-164)."""

    def _append_physical_duplicate(self) -> None:
        """Дописывает в журнал физический дубль первой записи (обход гарда)."""

        path = self.journal_path()
        text = path.read_text(encoding="utf-8")
        start = text.index(journal.BEGIN_MARKER)
        end = text.index(journal.END_MARKER) + len(journal.END_MARKER)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + text[start:end] + "\n")

    def test_103_physical_duplicate_handled_deterministically(self) -> None:
        """MM-103. Duplicate event_id в журнале обрабатывается детерминированно."""

        result = capture_turn(self.tmp, 1, self.config)
        self._append_physical_duplicate()
        indexed = self.run_index()
        self.assertEqual(indexed.duplicates, 1)
        self.assertEqual(indexed.records_indexed, 1)
        self.assertEqual(self.store.meta_count(), 1)
        meta = self.store.meta_get(result.event_id)
        self.assertEqual(meta["journal_offset"], 0)

    def test_115_rebuild_after_duplicate_is_deterministic(self) -> None:
        """MM-115. rebuild-index после duplicate event_id даёт детерминированный индекс."""

        capture_turn(self.tmp, 1, self.config)
        self._append_physical_duplicate()
        indexer.rebuild_index(self.store, self.root, self.logger)
        first = self.store.meta_event_ids()
        meta_first = self.store.meta_get(sorted(first)[0])
        indexer.rebuild_index(self.store, self.root, self.logger)
        self.assertEqual(self.store.meta_event_ids(), first)
        self.assertEqual(self.store.meta_get(sorted(first)[0]), meta_first)

    def test_104_verify_reports_duplicate_physical_records(self) -> None:
        """MM-104. verify сообщает о duplicate physical journal records."""

        capture_turn(self.tmp, 1, self.config)
        self.run_index()
        self._append_physical_duplicate()
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(len(report.duplicates), 1)
        self.assertIn("2 физических записей", report.duplicates[0])

    def test_162_revision_replaces_index_entry(self) -> None:
        """MM-162. Повторная доставка с новым ответом: в индексе последняя ревизия."""

        result = capture_turn(self.tmp, 1, self.config)
        self.run_index()
        data = capture.TurnData(
            user_message="реплика номер 1",
            assistant_response="иной ответ",
            session_id="s1",
            task_id="task_1",
            turn_id="1",
            cwd="C:/projects/work",
        )
        capture.capture_turn(data, self.config, self.logger, self.root, moment=MOMENT)
        indexed = self.run_index()
        self.assertEqual(indexed.revisions, 1)
        meta = self.store.meta_get(result.event_id)
        self.assertEqual(meta["revision"], 2)
        self.assertEqual(self.store.fts_count(), 1)
        hits = self.store.fts_search("иной")
        self.assertEqual([event for event, _ in hits], [result.event_id])

    def test_163_verify_reports_more_than_one_revision(self) -> None:
        """MM-163. verify сообщает event_id с более чем одной ревизией журнала."""

        capture_turn(self.tmp, 1, self.config)
        data = capture.TurnData(
            user_message="реплика номер 1",
            assistant_response="иной ответ",
            session_id="s1",
            task_id="task_1",
            turn_id="1",
            cwd="C:/projects/work",
        )
        capture.capture_turn(data, self.config, self.logger, self.root, moment=MOMENT)
        self.run_index()
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(len(report.multi_revision), 1)

    def test_64_index_keeps_journal_content_hash(self) -> None:
        """MM-64. Индексатор не пересчитывает content_hash из обрезанного текста."""

        config = make_config(self.tmp, max_assistant_text=5)
        result = capture_turn(self.tmp, 1, config, answer="очень длинный ответ агента")
        self.run_index()
        meta = self.store.meta_get(result.event_id)
        self.assertEqual(meta["content_hash"], result.content_hash)


class CursorTests(IndexerTestCase):
    """Курсоры индексатора (тесты 10, MM-71, MM-72, MM-73, MM-149)."""

    def test_71_skipped_record_indexed_after_failure(self) -> None:
        """MM-71. После сбоя индексатора пропущенная запись индексируется следующим запуском."""

        capture_turn(self.tmp, 1, self.config)
        capture_turn(self.tmp, 2, self.config)
        path = self.journal_path()
        key = paths.relative_to_root(path, self.root)

        first = indexer.index_journal_file(
            self.store, path, self.root, self.logger, indexer.IndexResult(), 1
        )
        self.assertEqual(first, 1)
        self.assertEqual(self.store.meta_count(), 1)

        second = self.run_index()
        self.assertEqual(second.records_indexed, 1)
        self.assertEqual(self.store.meta_count(), 2)
        self.assertEqual(self.store.cursor_get(key)["byte_offset"], path.stat().st_size)

    def test_72_cursor_invalid_when_file_shrinks(self) -> None:
        """MM-72. При уменьшении файла журнала курсор считается недействительным."""

        capture_turn(self.tmp, 1, self.config)
        capture_turn(self.tmp, 2, self.config)
        self.run_index()
        path = self.journal_path()
        key = paths.relative_to_root(path, self.root)
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: len(text) // 2], encoding="utf-8", newline="\n")

        result = self.run_index()
        self.assertEqual(result.cursor_resets, 1)
        cursor = self.store.cursor_get(key)
        self.assertLessEqual(int(cursor["byte_offset"]), path.stat().st_size)
        # Индекс не теряется: усечение файла не удаляет записи (MM-72).
        self.assertEqual(self.store.meta_count(), 2)

    def test_149_damaged_record_reported_once_per_file_offset(self) -> None:
        """MM-149. О повреждённой записи пишется одна строка на файл и смещение."""

        capture_turn(self.tmp, 1, self.config)
        path = self.journal_path()
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("<!-- mm:begin -->\n- event_id: e_bad\nтекст без секций\n")
        first = self.run_index()
        second = self.run_index()
        self.assertEqual(first.records_damaged, 1)
        self.assertEqual(second.records_damaged, 0)
        self.assertEqual(first.records_indexed, 1)

    def test_73_batch_limit_stops_pass(self) -> None:
        """MM-73. Один запуск ограничен числом записей за проход."""

        for index in range(1, 6):
            capture_turn(self.tmp, index, self.config)
        result = self.run_index(batch_limit=2)
        self.assertTrue(result.batch_limited)
        self.assertEqual(result.records_indexed, 2)

        second = self.run_index(batch_limit=2)
        self.assertEqual(second.records_indexed, 2)
        third = self.run_index(batch_limit=2)
        self.assertEqual(third.records_indexed, 1)
        self.assertEqual(self.store.meta_count(), 5)

    def test_10_indexer_error_does_not_delete_journal_record(self) -> None:
        """MM-10. Ошибка Индексатора не удаляет запись журнала."""

        capture_turn(self.tmp, 1, self.config)
        path = self.journal_path()
        before = path.read_text(encoding="utf-8")

        original = indexer.index_record

        def boom(*args, **kwargs):
            raise RuntimeError("сбой индексатора")

        indexer.index_record = boom  # type: ignore[assignment]
        try:
            indexer.index_project(self.store, self.root, self.logger)
        except RuntimeError:
            pass
        finally:
            indexer.index_record = original  # type: ignore[assignment]

        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.meta_count(), 0)


class ProjectFilterTests(IndexerTestCase):
    """Фильтрация по проекту (тесты 12, 43, MM-59)."""

    def setUp(self) -> None:
        super().setUp()
        self.config = make_config(self.tmp, project_mapping={"C:/projects/work": "work"})

    def _other_project_turn(self, index: int, session: str | None = None) -> capture.CaptureResult:
        data = capture.TurnData(
            user_message=f"реплика номер {index}",
            assistant_response=f"ответ номер {index}",
            session_id=session or f"s{index}",
            task_id=f"task_{index}",
            turn_id=str(index),
            cwd="C:/projects/other",
        )
        return capture.capture_turn(data, self.config, self.logger, self.root, moment=MOMENT)

    def test_12_search_does_not_see_other_project(self) -> None:
        """MM-12. Поиск не видит записи другого проекта."""

        capture_turn(self.tmp, 1, self.config)
        self._other_project_turn(2)
        self.run_index()
        work_hits = self.store.fts_search("реплика", project="work")
        common_hits = self.store.fts_search("реплика", project="common")
        self.assertEqual(len(work_hits), 1)
        self.assertEqual(len(common_hits), 1)
        self.assertNotEqual(work_hits[0][0], common_hits[0][0])

    def test_43_same_content_in_two_projects_is_not_duplicate(self) -> None:
        """MM-43. Одинаковое содержимое в разных проектах дублем не считается."""

        first = capture_turn(self.tmp, 1, self.config)
        second = self._other_project_turn(1, session="s-other")
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.event_id, second.event_id)
        self.run_index()
        self.assertEqual(self.store.meta_count(), 2)
        report = indexer.verify(self.store, self.root, self.logger)
        self.assertEqual(report.duplicates, [])

    def test_59_session_excluded_filter_uses_session_id(self) -> None:
        """MM-59. Поиск исключает записи текущей сессии по session_id."""

        first = capture_turn(self.tmp, 1, self.config, session="session-A")
        capture_turn(self.tmp, 2, self.config, session="session-B")
        self.run_index()
        current = {
            row["event_id"]
            for row in self.store.meta_by_project("work")
            if row["session_id"] == "session-B"
        }
        self.assertEqual(len(current), 1)
        self.assertNotIn(first.event_id, current)
