"""Тесты этапа 5: Поиск по реплике.

Основание: ТЗ v1.7 §3.3, §9, §12, §12.1, §13, §13.1, §16, §17, §19.5,
§21.2, §22, §26.2; план реализации v1.7, этап 5. Номера тестов
соответствуют §23 ТЗ: 12, 22–29, 48, 49, 50, MM-108.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
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

from mm import capture, indexer, insert, query, search  # noqa: E402
from mm.config import DEFAULTS  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.store import Store  # noqa: E402

HOOK = CODE_DIR / "hooks" / "pre_llm.py"
CLI = CODE_DIR / "minimem.py"
MOMENT = datetime.fromisoformat("2026-09-26T07:15:42+03:00")
PREVIOUS = "2026-09-25-01"
SESSION = "2026-09-26-01"


def make_config(tmp: str, **overrides) -> dict:
    config = dict(DEFAULTS)
    config["memory_root"] = tmp
    config["project_mapping"] = {"C:/projects/work": "work"}
    config.update(overrides)
    return config


def make_logger(tmp: str) -> Logger:
    return Logger(Path(tmp) / "logs" / "minimem.log")


def make_store(tmp: str) -> Store:
    store = Store(Path(tmp) / "minimem.db")
    store.create_schema()
    return store


def log_records(tmp: str) -> list[dict]:
    log = Path(tmp) / "logs" / "minimem.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


def log_operations(tmp: str) -> list[str]:
    return [record["operation"] for record in log_records(tmp)]


def load_hook_module():
    """Загружает `hooks/pre_llm.py`: пакет `hooks` не имеет `__init__`."""

    spec = importlib.util.spec_from_file_location("minimem_hook_pre_llm_search", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(
    session_id: str = SESSION,
    turn_id: str = "2",
    is_first_turn: bool = False,
    user: str = "новый вопрос",
) -> dict:
    """Полезная нагрузка `pre_llm_call` по составу §26.2."""

    return {
        "session_id": session_id,
        "cwd": "C:/projects/work",
        "extra": {
            "task_id": f"task_{turn_id}",
            "turn_id": turn_id,
            "user_message": user,
            "conversation_history": [],
            "is_first_turn": is_first_turn,
            "model": "test-model",
            "platform": "cli",
        },
    }


class SearchTestCase(unittest.TestCase):
    """Общая подготовка этапа: хранилище, индекс, конфигурация, хук."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = Path(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp, mode_search=True)
        self.logger = make_logger(self.tmp)
        self.hook = load_hook_module()
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
        count: int = 1,
        session_id: str = PREVIOUS,
        start: int = 1,
        user: str = "реплика номер",
        answer: str = "ответ номер",
        cwd: str = "C:/projects/work",
    ) -> list[str]:
        """Записывает ходы в журнал и прогоняет индексатор."""

        event_ids: list[str] = []
        for offset in range(count):
            data = capture.TurnData(
                user_message=f"{user} {start + offset}",
                assistant_response=f"{answer} {start + offset}",
                session_id=session_id,
                task_id=f"task_{start + offset}",
                turn_id=str(start + offset),
                cwd=cwd,
            )
            result = capture.capture_turn(
                data, self.config, make_logger(self.tmp), self.root, moment=MOMENT
            )
            event_ids.append(result.event_id)
        indexer.index_project(self.store, self.root, self.logger)
        return event_ids

    def seed_fillers(self, count: int = 8) -> None:
        """Добавляет фоновые записи: bm25 вырождается на 1–2 документах.

        При одной совпавшей записи IDF близок к нулю и score ≈ 0, поэтому
        проверки порога требуют корпуса из нескольких записей.
        """

        topics = [
            "обсуждение архитектуры проекта и модулей",
            "заметка про утреннюю встречу команды",
            "вопрос про интерфейс и кнопки поиска",
            "отчёт по продажам за прошлый квартал",
            "тест производительности индексатора",
            "разговор о погоде и времени отдыха",
            "список задач на следующую неделю",
            "запись без особенно длинного текста",
        ]
        for index in range(count):
            self.add_turns(
                1,
                session_id=f"filler-{index}",
                start=100 + index,
                user=topics[index % len(topics)],
                answer="краткий ответ по теме",
            )

    def handle(self, payload: dict):
        """Один ход через модуль хука; соединение теста на это время закрыто."""

        self.close_store()
        result = self.hook.handle_turn(payload, self.config, self.root, self.logger)
        self.store  # переоткрытие после хука
        return result

    def config_path(self, values: dict | None = None) -> Path:
        path = self.root / "config.json"
        source = self.config if values is None else values
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        return path

    def run_hook(self, raw: bytes, config_path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=raw,
            capture_output=True,
            env={**dict(os.environ), "MINIMEM_CONFIG": str(config_path)},
        )

    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), "--config", str(self.config_path()), *argv],
            capture_output=True,
        )


class QueryBuildTests(unittest.TestCase):
    """Построение запроса: §12 п.3, §12.1; номера 27, 49, 50, MM-108."""

    def setUp(self) -> None:
        self.config = dict(DEFAULTS)

    def test_49_short_word_does_not_enter_query(self) -> None:
        """MM-49. Слово короче search_min_word_len в запрос не попадает."""

        stems = query.extract_stems("всё да нет про память", self.config)
        self.assertEqual(stems, ["памят"])
        self.assertNotIn("нет", stems)

    def test_49_min_word_len_is_configurable(self) -> None:
        """MM-49. Порог длины слова берётся из конфигурации."""

        config = dict(DEFAULTS)
        config["search_min_word_len"] = 3
        config["search_stem_min_len"] = 3
        self.assertIn("нет", query.extract_stems("всё да нет", config))

    def test_108_zero_terms_after_normalization(self) -> None:
        """MM-108. Реплика без значимых слов не даёт ни одного термина.

        Служебные реплики отсекаются единственным нормативным механизмом —
        порогом `search_min_word_len` (§12 п.3): стоп-слов в ТЗ нет, и
        список служебных слов не вводится.
        """

        for text in ("да", "ок", "ага", "угу", "  ,,,  ", ""):
            with self.subTest(text=text):
                self.assertEqual(query.extract_stems(text, self.config), [])

    def test_27_user_text_cannot_change_query_structure(self) -> None:
        """MM-27. Операторы FTS5 в реплике не попадают в запрос."""

        stems = query.extract_stems('память" OR journal NEAR(', self.config)
        self.assertEqual(stems, ["памят", "journal", "near"])
        built = query.build_match_query(stems)
        # Каждая основа заключена в кавычки: оператор внутри кавычек — текст.
        self.assertEqual(built.count('"'), 2 * len(stems))
        for stem in stems:
            self.assertIn(f'"{stem}"*', built)

    def test_50_stem_prefix_is_not_substring_search(self) -> None:
        """MM-50. Усечённая основа ищется префиксом токена, не подстрокой."""

        self.assertEqual(query.stem_word("память", 4), "памят")
        self.assertEqual(query.stem_word("памяти", 4), "памят")
        self.assertEqual(query.build_match_query(["памят"], "all"), '"памят"*')

    def test_50_terms_are_limited_and_unique(self) -> None:
        """MM-50, §12.1. Дубликаты основ удаляются, количество ограничено."""

        config = dict(DEFAULTS)
        config["max_search_terms"] = 3
        text = "память памяти памяти журнал дайджест проект захват"
        stems = query.extract_stems(text, config)
        self.assertEqual(len(stems), 3)
        self.assertEqual(len(set(stems)), 3)
        self.assertEqual(stems[0], "памят")

    def test_81_long_utterance_respects_max_search_terms(self) -> None:
        """MM-81. Длинная реплика не создаёт запрос длиннее max_search_terms."""

        text = " ".join(f"слово{index}" for index in range(200))
        stems = query.extract_stems(text, self.config)
        self.assertEqual(len(stems), DEFAULTS["max_search_terms"])

    def test_query_modes_use_expected_operator(self) -> None:
        """П-11. Первая ступень — AND, вторая — релаксация до OR."""

        self.assertEqual(
            query.build_match_query(["памят", "журнал"], search.MODE_ALL),
            '"памят"* AND "журнал"*',
        )
        self.assertEqual(
            query.build_match_query(["памят", "журнал"], search.MODE_ANY),
            '"памят"* OR "журнал"*',
        )


class SearchPipelineTests(SearchTestCase):
    """Пайплайн поиска: номера 12, 22, 23, 25, 26, 28, 29, 48."""

    def test_22_substantive_turn_finds_relevant_records(self) -> None:
        """MM-22. Содержательная реплика находит релевантные записи."""

        target = self.add_turns(
            1,
            user="настройка ежедневного бэкапа журнала через robocopy",
            answer="скрипт и задача Планировщика созданы",
        )[0]
        self.add_turns(1, start=2, user="совсем другой разговор про погоду", answer="ок")
        self.seed_fillers()

        result = self.handle(event(user="как настроить ежедневный бэкап журнала"))
        self.assertTrue(result.inserted)
        self.assertIn(target, result.event_ids)
        self.assertIn(insert.START_DELIMITER, result.text)

    def test_23_short_service_turn_does_not_search(self) -> None:
        """MM-23. Короткая служебная реплика поиск не запускает."""

        self.add_turns(1, user="настройка бэкапа журнала")
        result = self.handle(event(user="да"))
        self.assertFalse(result.inserted)
        self.assertEqual(result.status, search.STATUS_NO_TERMS)
        records = [
            item for item in log_records(self.tmp) if item["operation"] == "search_skipped"
        ]
        self.assertTrue(records)
        self.assertEqual(records[-1]["error_detail_code"], "no_search_terms")
        self.assertEqual(records[-1]["search_terms"], 0)

    def test_24_already_returned_record_is_not_returned_again(self) -> None:
        """MM-24. Уже возвращённая запись повторно не выдаётся."""

        target = self.add_turns(
            1, user="настройка ежедневного бэкапа журнала", answer="robocopy"
        )[0]
        self.seed_fillers()
        first = self.handle(event(turn_id="2", user="настройка бэкапа журнала"))
        self.assertIn(target, first.event_ids)
        second = self.handle(event(turn_id="3", user="настройка бэкапа журнала"))
        self.assertNotIn(target, second.event_ids)
        self.assertEqual(self.store.usage_of(target), 1)

    def test_25_suppressed_record_is_not_returned(self) -> None:
        """MM-25. Запись, снятая с поиска, не возвращается."""

        target = self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")[0]
        self.seed_fillers()
        with self.store.connection:
            self.store.suppress(target, "rule1_age", "2026-09-26T08:00:00+00:00")
        result = self.handle(event(user="настройка бэкапа журнала"))
        self.assertNotIn(target, result.event_ids)
        self.assertIn("search_completed", log_operations(self.tmp))

    def test_26_result_limited_by_count_and_size(self) -> None:
        """MM-26. Результат ограничен количеством и суммарным размером."""

        for offset in range(6):
            self.add_turns(
                1,
                start=10 + offset,
                user="настройка бэкапа журнала robocopy",
                answer="подробный ответ про бэкап " * 40,
            )
        self.seed_fillers()
        self.config["max_return_records"] = 2
        self.config["max_return_chars"] = 400
        result = self.handle(event(user="настройка бэкапа журнала robocopy"))
        self.assertLessEqual(len(result.event_ids), 2)
        self.assertLessEqual(len(result.text), 400)
        self.assertTrue(result.truncated)

    def test_28_search_is_skipped_on_first_turn(self) -> None:
        """MM-28. На первом ходе сессии Поиск не запускается."""

        self.add_turns(1, user="настройка бэкапа журнала")
        result = self.handle(event(is_first_turn=True, user="настройка бэкапа журнала"))
        self.assertFalse(result.inserted)
        records = [
            item for item in log_records(self.tmp) if item["operation"] == "search_skipped"
        ]
        self.assertEqual(records[-1]["error_detail_code"], "return_turn")

    def test_mode_search_false_inserts_nothing(self) -> None:
        """§3.7.6. При mode_search=false поиск не выполняется."""

        self.add_turns(1, user="настройка бэкапа журнала")
        self.config["mode_search"] = False
        result = self.handle(event(user="настройка бэкапа журнала"))
        self.assertFalse(result.inserted)
        self.assertNotIn("search_injected", log_operations(self.tmp))


    def test_29_returned_record_contains_date_project_and_source(self) -> None:
        """MM-29. Каждая возвращённая запись содержит дату, проект и источник."""

        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.seed_fillers()
        result = self.handle(event(user="настройка бэкапа журнала"))
        self.assertIn("источник: MiniMem", result.text)
        self.assertIn("проект work", result.text)
        self.assertIn("2026-09-26 07:15", result.text)

    def test_48_word_form_variant_is_found(self) -> None:
        """MM-48. «память» находит запись со словом «памяти»."""

        target = self.add_turns(
            1, user="как устроена память проекта", answer="ответ про хранилище"
        )[0]
        self.seed_fillers()
        result = self.handle(event(user="расскажи про память"))
        self.assertIn(target, result.event_ids)

    def test_12_other_project_is_not_returned(self) -> None:
        """MM-12. Поиск не видит записи другого проекта."""

        other = self.add_turns(
            1, user="настройка бэкапа журнала", answer="robocopy", cwd="C:/projects/other"
        )[0]
        self.add_turns(1, start=2, user="настройка бэкапа журнала", answer="robocopy")
        self.seed_fillers()
        result = self.handle(event(user="настройка бэкапа журнала"))
        self.assertNotIn(other, result.event_ids)
        for event_id in result.event_ids:
            self.assertEqual(self.store.meta_get(event_id)["project"], "work")

    def test_16_usage_counted_only_for_inserted_records(self) -> None:
        """MM-16. Счётчик растёт только у фактически вставленных записей."""

        target = self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")[0]
        other = self.add_turns(1, start=2, user="совсем другой разговор", answer="ок")[0]
        self.seed_fillers()
        self.assertEqual(self.store.usage_of(target), 0)
        result = self.handle(event(user="настройка бэкапа журнала"))
        self.assertIn(target, result.event_ids)
        self.assertEqual(self.store.usage_of(target), 1)
        self.assertEqual(self.store.usage_of(other), 0)

    def test_17_returned_records_are_marked_in_session_state(self) -> None:
        """MM-17. Возвращённые записи отмечаются в состоянии сессии."""

        target = self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")[0]
        self.seed_fillers()
        self.handle(event(user="настройка бэкапа журнала"))
        self.assertEqual(self.store.session_returned_ids("work", SESSION), {target})
        self.assertEqual(self.store.session_returned_ids("work", "другая"), set())

    def test_relaxation_to_or_keeps_terms_and_logs_mode(self) -> None:
        """П-11. Релаксация до OR не меняет состав основ и пишется в лог."""

        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.seed_fillers()
        result = self.handle(event(user="настройка бэкапа журнала и настройка дайджеста"))
        self.assertTrue(result.inserted)
        records = [
            item for item in log_records(self.tmp) if item["operation"] == "search_injected"
        ]
        self.assertEqual(records[-1]["search_query_mode"], search.MODE_ANY)
        self.assertIn("бэкап", result.plan.stems)
        self.assertIn("дайджест", result.plan.stems)

    def test_threshold_direction_is_score_not_bm25(self) -> None:
        """§9. Запись проходит при score >= search_score_threshold."""

        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.seed_fillers()
        kept = search.plan(self.store, self.config, "work", "", "настройка бэкапа журнала")
        self.assertTrue(kept.selected)
        self.assertGreaterEqual(kept.selected[0].score, kept.threshold)

        strict = make_config(self.tmp)
        strict["search_score_threshold"] = 1000.0
        dropped = search.plan(self.store, strict, "work", "", "настройка бэкапа журнала")
        self.assertEqual(dropped.selected, [])
        self.assertEqual(dropped.candidates[0].reject_reason, search.REJECT_BELOW_THRESHOLD)


class SearchLogTests(SearchTestCase):
    """Контракт измеримости: §19.5, §22, П-10."""

    def test_log_contains_measurability_fields(self) -> None:
        """П-10. Лог поиска содержит все нормативные поля."""

        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.seed_fillers()
        self.handle(event(user="настройка бэкапа журнала"))
        records = [
            item for item in log_records(self.tmp) if item["operation"] == "search_injected"
        ]
        self.assertEqual(len(records), 1)
        fields = records[0]
        for name in (
            "search_terms",
            "search_query_mode",
            "search_hits",
            "search_returned",
            "returned_event_ids",
            "top5_scores",
            "threshold_applied",
            "deadline_remaining_ms",
        ):
            self.assertIn(name, fields)
        self.assertEqual(fields["module"], "search")
        self.assertIsInstance(fields["deadline_remaining_ms"], int)

    def test_log_does_not_contain_user_text(self) -> None:
        """§19.5, §22. Текст реплики в лог не пишется."""

        phrase = "особенная фраза про версию девять"
        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.handle(event(user=f"настройка бэкапа журнала {phrase}"))
        log_text = (Path(self.tmp) / "logs" / "minimem.log").read_text("utf-8")
        self.assertNotIn(phrase, log_text)


class SearchHookBoundaryTests(SearchTestCase):
    """Граница хука: §18, §19.2, §26.2; номера 22, 46."""

    def test_46_empty_search_output_keeps_exit_zero(self) -> None:
        """MM-46. Пустой вывод хука не меняет сообщение и не роняет ход."""

        payload = json.dumps(event(user="да")).encode("utf-8")
        result = self.run_hook(payload, self.config_path())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")

    def test_46_hook_outputs_search_context_json(self) -> None:
        """MM-22, §26.2. Вывод pre_llm_call — JSON {"context": ...}."""

        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.seed_fillers()
        self.close_store()
        payload = json.dumps(event(user="настройка бэкапа журнала")).encode("utf-8")
        result = self.run_hook(payload, self.config_path())
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(sorted(output), ["context"])
        self.assertIn(insert.START_DELIMITER, output["context"])

    def test_search_failure_does_not_break_turn(self) -> None:
        """§19.2. Ошибка Поиска перехватывается на границе хука."""

        self.add_turns(1, user="настройка бэкапа журнала")
        self.close_store()
        broken_values = dict(self.config)
        broken_values["memory_root"] = str(self.root / "нет-хранилища")
        broken = self.config_path(broken_values)
        payload = json.dumps(event(user="настройка бэкапа журнала")).encode("utf-8")
        result = self.run_hook(payload, broken)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")


class SearchCliTests(SearchTestCase):
    """Команда `minimem search`: §21.2, П-11."""

    def test_search_command_prints_result_without_counters(self) -> None:
        """П-11. CLI печатает результат и не трогает счётчики и индекс."""

        target = self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")[0]
        self.seed_fillers()
        self.close_store()
        result = self.run_cli(
            "search", "настройка бэкапа журнала", "--project", "C:/projects/work"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode("utf-8")
        self.assertIn(target, output)
        self.assertIn("источник: MiniMem", output)
        self.assertEqual(self.store.usage_of(target), 0)
        self.assertEqual(self.store.session_returned_ids("work", ""), set())

    def test_search_explain_prints_query_and_reasons(self) -> None:
        """П-11. `--explain` показывает основы, запрос, режим и отсев."""

        target = self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")[0]
        self.close_store()
        result = self.run_cli(
            "search",
            "настройка бэкапа журнала",
            "--explain",
            "--project",
            "C:/projects/work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode("utf-8")
        self.assertIn("основы:", output)
        self.assertIn("fts-запрос:", output)
        self.assertIn("режим запроса:", output)
        self.assertIn(target, output)

    def test_search_explain_reports_threshold_rejection(self) -> None:
        """П-11. Причина отсева по порогу видна в `--explain`."""

        self.add_turns(1, user="настройка бэкапа журнала", answer="robocopy")
        self.close_store()
        strict = dict(self.config)
        strict["search_score_threshold"] = 1000.0
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--config",
                str(self.config_path(strict)),
                "search",
                "настройка бэкапа журнала",
                "--explain",
                "--project",
                "C:/projects/work",
            ],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(search.REJECT_BELOW_THRESHOLD, result.stdout.decode("utf-8"))

    def test_search_without_terms_reports_zero_terms(self) -> None:
        """§12.1 п.3. При нуле терминов поиск не запускается."""

        self.add_turns(1, user="настройка бэкапа журнала")
        self.close_store()
        result = self.run_cli("search", "да", "--project", "C:/projects/work")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ни одного термина", result.stdout.decode("utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
