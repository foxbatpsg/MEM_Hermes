"""Тесты этапа 4: Возврат и хук pre_llm_call.

Основание: ТЗ v1.7 §3.2, §11, §11.1, §13, §13.1, §17, §18, §19.5, §22,
§26.2; план реализации v1.7, этап 4. Номера тестов соответствуют §23 ТЗ:
17, 18, 19, 20, 21, 30, 45, 46, MM-109.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import capture, digest, indexer, insert, paths, ret, session  # noqa: E402
from mm.config import DEFAULTS  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.store import Store  # noqa: E402

MOMENT = datetime.fromisoformat("2026-09-26T07:15:42+03:00")
HOOK = CODE_DIR / "hooks" / "pre_llm.py"
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


def capture_turn(
    tmp: str,
    index: int,
    config: dict,
    session_id: str = SESSION,
    moment: datetime | None = None,
    user: str | None = None,
    answer: str | None = None,
) -> capture.CaptureResult:
    data = capture.TurnData(
        user_message=user if user is not None else f"реплика номер {index}",
        assistant_response=answer if answer is not None else f"ответ номер {index}",
        session_id=session_id,
        task_id=f"task_{index}",
        turn_id=str(index),
        cwd="C:/projects/work",
    )
    return capture.capture_turn(
        data, config, make_logger(tmp), Path(tmp), moment=moment or MOMENT
    )


def log_records(tmp: str) -> list[dict]:
    log = Path(tmp) / "logs" / "minimem.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


def log_operations(tmp: str) -> list[str]:
    return [record["operation"] for record in log_records(tmp)]


def event(
    session_id: str = SESSION,
    turn_id: str = "1",
    is_first_turn: bool = False,
    history: list | None = None,
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
            "conversation_history": history or [],
            "is_first_turn": is_first_turn,
            "model": "test-model",
            "platform": "cli",
        },
    }


def memory_block_text() -> str:
    """Текст вставленного блока памяти MiniMem с завершающим переводом строки."""

    block, _ = insert.build_memory_block("выжимка прошлой сессии", int(DEFAULTS["max_return_chars"]))
    return block + "\n"


def memory_message(body: str) -> dict:
    """Сообщение истории, целиком состоящее из блока памяти MiniMem (§13)."""

    block, _ = insert.build_memory_block(body, int(DEFAULTS["max_return_chars"]))
    return {"role": "user", "content": block}


def plain_history(count: int, size: int = 50) -> list[dict]:
    return [{"role": "user", "content": "x" * size} for _ in range(count)]


def load_hook_module():
    """Загружает `hooks/pre_llm.py` как модуль: пакет `hooks` не имеет __init__."""

    spec = importlib.util.spec_from_file_location("minimem_hook_pre_llm", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReturnTestCase(unittest.TestCase):
    """Общая подготовка этапа: хранилище, индекс, конфигурация.

    Соединение со служебным слоем открывается по требованию и закрывается
    перед вызовом хука: хук открывает своё соединение к тому же файлу,
    а два писателя в один момент не работают (§19.4).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = Path(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp, mode_return=True)
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
        count: int = 3,
        session_id: str = SESSION,
        start: int = 1,
        base: datetime | None = None,
    ) -> None:
        moment = base or MOMENT
        for offset in range(count):
            capture_turn(
                self.tmp,
                start + offset,
                self.config,
                session_id=session_id,
                moment=moment + timedelta(minutes=5 * offset),
            )
        indexer.index_project(self.store, self.root, self.logger)

    def build_digest(self, session_id: str = SESSION, **kwargs) -> digest.DigestResult:
        return digest.build_digest(
            self.store, self.config, self.root, self.logger, session_id, "work", **kwargs
        )

    def handle(self, payload: dict):
        """Один ход через модуль хука; соединение теста на это время закрыто."""

        self.close_store()
        result = self.hook.handle_turn(payload, self.config, self.root, self.logger)
        self.store  # переоткрытие после хука
        return result

    def write_config(self, **overrides) -> Path:
        path = self.root / "config.json"
        path.write_text(
            json.dumps(make_config(self.tmp, **overrides), ensure_ascii=False), encoding="utf-8"
        )
        return path

    def run_hook(self, raw: bytes, config_path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=raw,
            capture_output=True,
            env={**dict(os.environ), "MINIMEM_CONFIG": str(config_path)},
        )


class FirstTurnReturnTests(ReturnTestCase):
    """Первый ход новой сессии: 17, 19, 20, 21, 30."""

    def prepare_previous(self) -> str:
        """Дайджест предыдущей сессии: он и есть кандидат на Возврат."""

        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        result = self.build_digest(PREVIOUS)
        self.assertTrue(result.created_ok)
        return Path(result.path).name

    def test_17_return_inserts_previous_session_digest(self) -> None:
        """MM-17. Выжимка возвращается на первом ходу новой сессии."""

        digest_file = self.prepare_previous()
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertEqual(result.status, ret.STATUS_INSERTED)
        self.assertEqual(result.source, ret.SOURCE_FIRST_TURN)
        self.assertEqual(result.digest_file, digest_file)
        self.assertIn(insert.INTRO_LINE, result.text)
        self.assertIn(insert.START_DELIMITER, result.text)
        self.assertIn(insert.END_DELIMITER, result.text)
        self.assertIn("ответ номер 3", result.text)
        # Блок памяти вставляется в сообщение пользователя (§13).
        self.assertTrue(result.text.startswith(insert.INTRO_LINE))

    def test_17_return_without_digest_inserts_nothing(self) -> None:
        """Файла дайджеста нет — вставляется пустой объект, ход продолжается."""

        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertFalse(result.inserted)
        self.assertEqual(result.status, ret.STATUS_NO_DIGEST)
        self.assertEqual(result.text, "")

    def test_19_return_does_not_run_fts_search(self) -> None:
        """MM-19. Возврат не выполняет FTS-поиск."""

        self.prepare_previous()

        def forbidden(*args, **kwargs):  # pragma: no cover - сработать не должно
            raise AssertionError("Возврат не обращается к FTS5")

        original = Store.fts_search
        Store.fts_search = forbidden  # type: ignore[assignment]
        self.addCleanup(setattr, Store, "fts_search", original)

        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertTrue(result.inserted)

    def test_20_return_does_not_call_model(self) -> None:
        """MM-20. Возврат не использует модель: текст берётся из файла."""

        self.prepare_previous()

        def forbidden(*args, **kwargs):  # pragma: no cover - сработать не должно
            raise AssertionError("на первом ходе дайджест не создаётся")

        original = digest.build_digest
        digest.build_digest = forbidden  # type: ignore[assignment]
        self.addCleanup(setattr, digest, "build_digest", original)

        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertTrue(result.inserted)
        self.assertIn("детерминированная выжимка, без модели", result.text)

    def test_21_return_size_within_max_return_chars(self) -> None:
        """MM-21. Размер Возврата не превышает max_return_chars."""

        self.add_turns(4, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.build_digest(PREVIOUS)
        for limit in (400, 800, 1500):
            with self.subTest(max_return_chars=limit):
                self.config["max_return_chars"] = limit
                result = self.handle(event(session_id=SESSION, is_first_turn=True))
                self.assertTrue(result.inserted)
                self.assertLessEqual(result.chars, limit)
                self.assertIn(insert.END_DELIMITER, result.text)

    def test_30_selection_uses_last_turn_and_excludes_current_session(self) -> None:
        """MM-30. Выбор по «Последнему ходу» в UTC, текущая сессия исключена."""

        self.prepare_previous()
        # Более ранний по времени создания дайджест не должен выиграть:
        # ключ выбора — «Последний ход», а не «Создан» (П-07).
        other = "2026-09-24-01"
        self.add_turns(2, session_id=other, base=MOMENT - timedelta(days=3))
        built = self.build_digest(other, created="2026-01-01T00:00:00+00:00")
        self.assertTrue(built.created_ok)

        chosen = ret.select_last_digest(self.root, "work", SESSION, self.logger)
        self.assertEqual(chosen.session_id, PREVIOUS)
        self.assertEqual(
            chosen.last_turn_utc,
            digest.read_header(
                digest.digest_path(self.root, "work", PREVIOUS)
            ).last_turn_utc,
        )
        # Файл текущей сессии кандидатом не является (§11.1 п.1).
        self.build_digest(SESSION, created="2027-01-01T00:00:00+00:00")
        again = ret.select_last_digest(self.root, "work", SESSION, self.logger)
        self.assertEqual(again.session_id, PREVIOUS)

    def test_30_tie_break_prefers_smaller_file_name(self) -> None:
        """MM-30. При равенстве «Последнего хода» выбирается меньшее имя файла."""

        stamp = "2026-09-20T10:00:00+00:00"
        names = []
        for session_id in ("2026-09-19-02", "2026-09-19-01"):
            path = digest.digest_path(self.root, "work", session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n".join(
                    [
                        digest.TITLE,
                        f"- Сессия: {session_id}",
                        "- Дата: 2026-09-19",
                        "- Проект: work",
                        "- Ходов в сессии: 2",
                        f"- Последний ход: {stamp}",
                        "- Создан: 2026-09-20T12:00:00+00:00",
                        digest.METHOD_LINE,
                    ]
                ),
                encoding="utf-8",
            )
            names.append(path.name)
        chosen = ret.select_last_digest(self.root, "work", SESSION, self.logger)
        self.assertEqual(chosen.file, min(names))

    def test_damaged_digest_is_skipped_and_logged(self) -> None:
        """MM-96 (подготовка). Повреждённый дайджест не мешает выбору (§11.1 п.6)."""

        self.prepare_previous()
        broken = digest.digest_path(self.root, "work", "2026-09-23-01")
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("не дайджест вовсе", encoding="utf-8")
        chosen = ret.select_last_digest(self.root, "work", SESSION, self.logger)
        self.assertEqual(chosen.session_id, PREVIOUS)
        self.assertIn("return_digest_damaged", log_operations(self.tmp))

    def test_unreadable_last_turn_falls_back_to_mtime(self) -> None:
        """MM-96 (подготовка). Fallback на mtime и событие §11.1 п.5."""

        path = digest.digest_path(self.root, "work", "2026-09-19-01")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    digest.TITLE,
                    "- Сессия: 2026-09-19-01",
                    "- Дата: 2026-09-19",
                    "- Проект: work",
                    "- Ходов в сессии: 1",
                    "- Последний ход: вчера вечером",
                    "- Создан: 2026-09-19T10:00:00+00:00",
                    digest.METHOD_LINE,
                ]
            ),
            encoding="utf-8",
        )
        candidate = ret.read_digest_candidate(path, self.logger)
        self.assertTrue(candidate.fallback_used)
        self.assertTrue(candidate.last_turn_utc)
        self.assertIn("digest_created_field_fallback", log_operations(self.tmp))


class CompactionReturnTests(ReturnTestCase):
    """Сжатие контекста: 18, 45, MM-109."""

    def grow_then_shrink(self, count: int = 1, cooldown: int = 0) -> None:
        """Ходы с длинной историей, затем ход с резко сократившейся.

        Кулдаун по умолчанию отключён: иначе он маскировал бы сам факт
        сжатия на следующих ходах (его проверяет отдельный тест).
        """

        self.config["compaction_cooldown_turns"] = cooldown
        for number in (1, 2):
            self.handle(event(turn_id=str(number), history=plain_history(20, 500)))
        self.handle(event(turn_id="3", history=plain_history(count, 40)))

    def test_18_return_current_session_digest_after_compaction(self) -> None:
        """MM-18. После сжатия возвращается выжимка текущей сессии."""

        self.grow_then_shrink()
        self.assertEqual(session.load_state(self.store, "work", SESSION).max_msgs, 1)

        self.add_turns(2, session_id=SESSION, base=MOMENT)
        # История выросла, затем Hermes снова её сжал.
        self.handle(event(turn_id="4", history=plain_history(20, 500)))
        result = self.handle(event(turn_id="5", history=plain_history(1, 40)))
        self.assertEqual(result.source, ret.SOURCE_COMPACTION)
        self.assertEqual(result.status, ret.STATUS_INSERTED)
        self.assertEqual(result.session_id, SESSION)
        self.assertIn("ответ номер 2", result.text)

    def test_18_digest_created_on_compaction_when_missing(self) -> None:
        """§11. Файла дайджеста текущей сессии нет — его создаёт Дайджест."""

        self.grow_then_shrink()
        path = digest.digest_path(self.root, "work", SESSION)
        self.assertFalse(path.exists())

        self.add_turns(1, session_id=SESSION, base=MOMENT)
        self.handle(event(turn_id="4", history=plain_history(20, 500)))
        result = self.handle(event(turn_id="5", history=plain_history(1, 40)))
        self.assertTrue(path.is_file())
        self.assertTrue(result.inserted)
        self.assertIn("return_digest_rebuild", log_operations(self.tmp))

    def test_45_decision_uses_thresholds_and_is_logged(self) -> None:
        """MM-45. Решение о сжатии считается по порогам и попадает в лог."""

        config = self.config
        big = session.HistoryMetrics(hist_msgs=20, hist_chars=20_000)
        # Без точек отсчёта сжатия быть не может: сравнивать не с чем.
        self.assertFalse(session.detect_compaction(config, big, 0, 0).detected)
        self.assertFalse(session.detect_compaction(config, big, 20, 20_000).detected)
        shrunk = session.HistoryMetrics(hist_msgs=2, hist_chars=300)
        decision = session.detect_compaction(config, shrunk, 20, 20_000)
        self.assertTrue(decision.detected)
        self.assertIn("hist_msgs", decision.threshold_applied)
        # Порог по числу сообщений не достигнут, сработал порог по длине.
        chars_only = session.detect_compaction(
            config, session.HistoryMetrics(hist_msgs=19, hist_chars=300), 20, 20_000
        )
        self.assertTrue(chars_only.detected)
        self.assertIn("hist_chars", chars_only.threshold_applied)
        self.assertNotIn("hist_msgs<", chars_only.threshold_applied)

        self.grow_then_shrink()
        records = [
            item for item in log_records(self.tmp) if item["operation"] == "compaction_detected"
        ]
        self.assertTrue(records)
        last = records[-1]
        for field_name in ("hist_msgs", "hist_chars", "compaction_decision", "threshold_applied"):
            self.assertIn(field_name, last)
        self.assertEqual(last["compaction_decision"], session.DECISION_COMPACTED)

    def test_45_cooldown_blocks_repeat_detection(self) -> None:
        """MM-123. Кулдаун не допускает повторную вставку на следующих ходах."""

        self.grow_then_shrink(cooldown=2)
        for turn_id in ("4", "5"):
            with self.subTest(turn=turn_id):
                # История после сжатия снова короткая: без кулдауна это было бы
                # новое сжатие, а с кулдауном — нет.
                again = self.handle(event(turn_id=turn_id, history=plain_history(1, 40)))
                self.assertFalse(again.inserted)
        decisions = [
            item["compaction_decision"]
            for item in log_records(self.tmp)
            if item["operation"] == "compaction_detected"
        ]
        self.assertEqual(decisions[-2:], [session.DECISION_COOLDOWN] * 2)

    def test_45_injection_limit_is_enforced_and_logged(self) -> None:
        """MM-124. Лимит вставок за сессию соблюдается и логируется."""

        self.config["max_return_injections_per_session"] = 1
        self.grow_then_shrink()
        self.add_turns(3, session_id=SESSION, base=MOMENT)
        self.handle(event(turn_id="4", history=plain_history(20, 500)))
        self.assertTrue(self.handle(event(turn_id="5", history=plain_history(1, 40))).inserted)
        for turn_id in ("6", "7"):
            self.handle(event(turn_id=turn_id, history=plain_history(20, 500)))
            with self.subTest(turn=turn_id):
                limited = self.handle(event(turn_id=turn_id, history=plain_history(1, 40)))
                self.assertEqual(limited.status, ret.STATUS_LIMIT)
        self.assertIn("return_injection_limit", log_operations(self.tmp))

    def test_109_history_length_measured_without_minimem_blocks(self) -> None:
        """MM-109. Длина conversation_history измеряется без блоков MiniMem."""

        history = [
            memory_message("выжимка прошлой сессии"),
            {"role": "assistant", "content": "ответ без памяти"},
            memory_message("выжимка прошлой сессии"),
        ]
        metrics = session.measure_history(history)
        self.assertEqual(metrics.hist_msgs, 1)
        self.assertEqual(metrics.hist_chars, len("ответ без памяти"))
        # С блоками MiniMem измерение было бы другим.
        raw_chars = sum(len(item["content"]) for item in history)
        self.assertGreater(raw_chars, metrics.hist_chars)

    def test_109_blocks_do_not_shift_compaction_decision(self) -> None:
        """MM-109. Блоки памяти не искажают решение о сжатии контекста."""

        source = plain_history(20, 500)
        decorated = session.measure_history(
            [
                {"role": item["role"], "content": memory_block_text() + item["content"]}
                for item in source
            ]
        )
        self.assertEqual(decorated, session.measure_history(source))


class ModeAndHookTests(ReturnTestCase):
    """Режимы и граница хука: 46, §3.7.5, §18."""

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.build_digest(PREVIOUS)

    def test_return_disabled_inserts_nothing(self) -> None:
        """§3.7.5. При mode_return=false дайджест в контекст не вставляется."""

        self.prepare_previous()
        self.config["mode_return"] = False
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertFalse(result.inserted)
        self.assertIn("return_disabled", log_operations(self.tmp))

    def test_mode_search_gate_is_logged_without_results(self) -> None:
        """§12. Поиск этого этапа не реализован: gate виден в логе, выдачи нет."""

        self.config["mode_return"] = False
        self.config["mode_search"] = True
        result = self.handle(event(session_id=SESSION, turn_id="2", user="память и дайджест"))
        self.assertFalse(result.inserted)
        operations = log_operations(self.tmp)
        self.assertIn("search_not_implemented", operations)

    def test_46_empty_output_does_not_change_user_message(self) -> None:
        """MM-46. Пустой вывод хука не меняет сообщение пользователя."""

        config_path = self.write_config(mode_return=True)
        payload = json.dumps(event(session_id="2026-09-27-01", is_first_turn=True)).encode("utf-8")
        result = self.run_hook(payload, config_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")

    def test_46_broken_input_and_bad_config_exit_zero(self) -> None:
        """MM-46. Битый stdin и недоступная конфигурация не прерывают ход."""

        self.prepare_previous()
        for payload, config_path in (
            (b"\xff\xfe not json", self.write_config(mode_return=True)),
            (b"", self.write_config(mode_return=True)),
            (b"[]", self.write_config(mode_return=True)),
            (b'{"session_id": "2026-09-26-01"}', self.root / "нет-конфига.json"),
        ):
            with self.subTest(payload=payload[:12]):
                result = self.run_hook(payload, config_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.decode("utf-8").strip(), "{}")

    def test_46_hook_outputs_context_json(self) -> None:
        """§26.2. Вывод pre_llm_call — JSON {"context": ...}."""

        self.prepare_previous()
        config_path = self.write_config(mode_return=True)
        payload = json.dumps(event(session_id=SESSION, is_first_turn=True)).encode("utf-8")
        result = self.run_hook(payload, config_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(sorted(output), ["context"])
        self.assertIn(insert.START_DELIMITER, output["context"])
        self.assertIn("ответ номер 3", output["context"])

    def test_deadline_remaining_is_logged(self) -> None:
        """§19.5, П-06. Остаток дедлайна хука пишется в лог."""

        self.prepare_previous()
        self.handle(event(session_id=SESSION, is_first_turn=True))
        for record in log_records(self.tmp):
            if record["operation"] in ("compaction_detected", "return_injected"):
                self.assertIsInstance(record["deadline_remaining_ms"], int)
