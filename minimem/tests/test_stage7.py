"""Тесты этапа 7: Жизненный цикл, сессии, catch-up, надёжность.

Основание: ТЗ v1.7 §17, §18, §18.1, §19.1, §19.1.1, §19.2, §19.3, §19.4,
§19.5, §20, §21.5, §22, §26.1, §26.2; план реализации v1.7, этап 7.
Номера тестов соответствуют §23 ТЗ: 40, 41, MM-52, MM-55, MM-74, MM-75,
MM-76, MM-110, плюс MM-77, MM-78 (режимы catch-up) и проверки MM-111,
MM-113 на неполомку.

Запуск: python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import catchup, capture, compaction, digest, indexer, ret, session  # noqa: E402
from mm.config import DEFAULTS, hook_deadline_ms  # noqa: E402
from mm.log import Logger  # noqa: E402
from mm.store import Store  # noqa: E402

PRE_LLM_HOOK = CODE_DIR / "hooks" / "pre_llm.py"
SESSION_END_HOOK = CODE_DIR / "hooks" / "session_end.py"
MOMENT = datetime.fromisoformat("2026-09-26T07:15:42+03:00")
PREVIOUS = "2026-09-25-01"
PREVIOUS_TWO = "2026-09-24-01"
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


def load_hook(path: Path, name: str):
    """Загружает скрипт хука как модуль: пакет `hooks` не имеет `__init__`."""

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def session_end_event(session_id: str = SESSION, **extra) -> dict:
    """Полезная нагрузка `on_session_end` по составу §26.2."""

    return {"session_id": session_id, "cwd": "C:/projects/work", "extra": extra}


class LifecycleTestCase(unittest.TestCase):
    """Общая подготовка этапа: хранилище, индекс, конфигурация, хуки.

    Соединение со служебным слоем закрывается перед вызовом хука: хук открывает
    своё соединение к тому же файлу, а два писателя в один момент не
    работают (§19.4).
    """

    modes: dict = {"mode_return": True}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.root = Path(self.tmp)
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(self.tmp, **self.modes)
        self.logger = make_logger(self.tmp)
        self.pre_hook = load_hook(PRE_LLM_HOOK, "minimem_hook_pre_llm_s7")
        self.end_hook = load_hook(SESSION_END_HOOK, "minimem_hook_session_end_s7")
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
            data = capture.TurnData(
                user_message=f"реплика номер {start + offset}",
                assistant_response=f"ответ номер {start + offset}",
                session_id=session_id,
                task_id=f"task_{start + offset}",
                turn_id=str(start + offset),
                cwd="C:/projects/work",
            )
            capture.capture_turn(
                data,
                self.config,
                self.logger,
                self.root,
                moment=moment + timedelta(minutes=5 * offset),
            )
        indexer.index_project(self.store, self.root, self.logger)

    def mark_session(self, session_id: str, last_seen: str, **fields) -> None:
        """Кладёт строку `sessions` вручную — для выбора предыдущей сессии."""

        with self.store.connection:
            self.store.session_upsert(session_id, "work", last_seen_at=last_seen, **fields)

    def handle(self, payload: dict, started: float | None = None):
        """Один ход через модуль хука `pre_llm`."""

        self.close_store()
        result = self.pre_hook.handle_turn(payload, self.config, self.root, self.logger, started)
        self.store  # переоткрытие после хука
        return result

    def end_session(self, payload: dict):
        """Событие `on_session_end` через модуль хука."""

        self.close_store()
        result = self.end_hook.run(payload, self.config, self.root, self.logger)
        self.store
        return result

    def run_hook(self, path: Path, payload: dict, **overrides) -> subprocess.CompletedProcess:
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(make_config(self.tmp, **overrides), ensure_ascii=False), encoding="utf-8"
        )
        return subprocess.run(
            [sys.executable, str(path)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env={**dict(os.environ), "MINIMEM_CONFIG": str(config_path)},
        )

    def _restore_config_env(self, previous: str | None) -> None:
        if previous is None:
            os.environ.pop("MINIMEM_CONFIG", None)
        else:
            os.environ["MINIMEM_CONFIG"] = previous

    def session_row(self, session_id: str = SESSION) -> dict:
        return self.store.session_get(session_id, "work") or {}

    def digest_path(self, session_id: str) -> Path:
        return digest.digest_path(self.root, "work", session_id)



class CatchUpFirstTurnTests(LifecycleTestCase):
    """Первый ход новой сессии: MM-74, MM-110, MM-77, MM-78."""

    def prepare_previous(self, count: int = 3) -> None:
        self.add_turns(count, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def test_74_catch_up_creates_previous_digest_and_clears_pending(self) -> None:
        """MM-74. Catch-up выполняется при отсутствующем дайджесте."""

        self.prepare_previous()
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertTrue(self.digest_path(PREVIOUS).exists())
        self.assertEqual(self.session_row()["catch_up_target_session_id"], PREVIOUS)
        self.assertEqual(self.session_row()["pending_catch_up"], 0)
        self.assertEqual(self.session_row()["catch_up_attempts"], 0)
        self.assertIn(catchup.OPERATION_CATCH_UP, log_operations(self.tmp))
        # Повторный ход той же сессии ничего не создаёт.
        self.handle(event(session_id=SESSION, turn_id="2"))
        self.assertEqual(len(digest.existing_digest_files(self.root, "work")), 1)
        # Возврат прочитал тот же дайджест, который построил catch-up (П-07).
        self.assertEqual(result.digest_file, self.digest_path(PREVIOUS).name)

    def test_74_catch_up_skipped_without_previous_session(self) -> None:
        """MM-74. Предыдущей сессии нет — catch-up нечего делать."""

        self.add_turns(1, session_id=PREVIOUS)
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertEqual(result.status, ret.STATUS_NO_DIGEST)
        self.assertEqual(self.session_row()["pending_catch_up"], 0)
        self.assertFalse(self.digest_path(PREVIOUS).exists())

    def test_74_previous_session_without_digests_chosen_by_last_seen(self) -> None:
        """MM-74, П-07. Без валидных дайджестов берётся последняя сессия."""

        self.add_turns(1, session_id=PREVIOUS_TWO)
        self.add_turns(1, session_id=PREVIOUS, start=9)
        self.mark_session(PREVIOUS_TWO, "2026-09-20T10:00:00+03:00")
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertEqual(self.session_row()["catch_up_target_session_id"], PREVIOUS)
        self.assertTrue(self.digest_path(PREVIOUS).exists())
        self.assertFalse(self.digest_path(PREVIOUS_TWO).exists())

    def test_110_catch_up_not_repeated_when_digest_done(self) -> None:
        """MM-110. При `digest_status = done` и валидном файле дайджест не строится."""

        self.prepare_previous()
        first = digest.build_digest(self.store, self.config, self.root, self.logger, PREVIOUS, "work")
        self.assertTrue(first.created_ok)
        before = self.digest_path(PREVIOUS).read_bytes()

        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertEqual(self.digest_path(PREVIOUS).read_bytes(), before)
        record = next(
            item
            for item in reversed(log_records(self.tmp))
            if item["operation"] == catchup.OPERATION_CATCH_UP
        )
        self.assertEqual(record["error_detail_code"], catchup.REASON_DIGEST_UP_TO_DATE)

    def test_74_interrupted_previous_session_gets_completion_field(self) -> None:
        """MM-74, П-17. Прерванная сессия: дайджест создаётся с полем «Завершение»."""

        self.prepare_previous()
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00", completed=0)
        self.handle(event(session_id=SESSION, is_first_turn=True))
        header = digest.read_header(self.digest_path(PREVIOUS))
        self.assertIsNotNone(header)
        self.assertEqual(header.completion, digest.COMPLETION_INTERRUPTED)

    def test_78_catch_up_without_mode_return_creates_no_digest(self) -> None:
        """MM-78. При `mode_return = false` дайджест не создаётся и не вставляется."""

        self.prepare_previous()
        self.config = make_config(self.tmp, mode_return=False, mode_compaction=True)
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertFalse(result.inserted)
        self.assertFalse(self.digest_path(PREVIOUS).exists())
        self.assertEqual(self.session_row()["pending_catch_up"], 0)



class CatchUpCompactionModeTests(LifecycleTestCase):
    """MM-77: уплотнение и дайджест управляются режимами независимо."""

    modes = {"mode_return": True, "mode_compaction": True}

    def prepare_previous(self, count: int = 3) -> None:
        self.add_turns(count, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def test_77_catch_up_compacts_previous_session(self) -> None:
        """MM-77. При `mode_compaction = true` catch-up выполняет уплотнение."""

        self.prepare_previous()
        # Часть записей уже снята: дайджест уплотнённой сессии строится по
        # оставшимся, снятые в него не попадают (§14, §15).
        suppressed = sorted(self.store.meta_event_ids())[:2]
        with self.store.connection:
            for event_id in suppressed:
                self.store.suppress(
                    event_id, compaction.REASON_UNUSED, "2026-01-01T00:00:00+00:00"
                )
        self.assertEqual(self.store.suppressed_count(), 2)

        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertIn(compaction.OPERATION_APPLIED, log_operations(self.tmp))
        self.assertTrue(self.digest_path(PREVIOUS).exists())

    def test_77_catch_up_without_compaction_mode_creates_digest(self) -> None:
        """MM-77. При `mode_compaction = false` дайджест всё равно создаётся."""

        self.prepare_previous()
        self.config = make_config(self.tmp, mode_return=True, mode_compaction=False)
        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertTrue(self.digest_path(PREVIOUS).exists())
        self.assertNotIn(compaction.OPERATION_APPLIED, log_operations(self.tmp))
        self.assertEqual(self.store.suppressed_count(), 0)




class CatchUpIdempotencyTests(LifecycleTestCase):
    """MM-76, MM-110: повторный прогон не дублирует операции."""

    modes = {"mode_return": True, "mode_compaction": True}

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def test_76_repeated_catch_up_creates_one_digest(self) -> None:
        """MM-76. Повторный прогон не создаёт дублирующиеся дайджесты."""

        self.prepare_previous()
        for index in range(3):
            self.handle(
                event(session_id=SESSION, turn_id=str(index + 1), is_first_turn=index == 0)
            )
        files = digest.existing_digest_files(self.root, "work")
        self.assertEqual([item.name for item in files], [self.digest_path(PREVIOUS).name])
        self.assertEqual(self.session_row()["pending_catch_up"], 0)

    def test_76_repeated_catch_up_is_idempotent_for_compaction(self) -> None:
        """MM-76. Уплотнение идемпотентно по suppression state."""

        self.prepare_previous()
        with self.store.connection:
            for event_id in self.store.meta_event_ids():
                self.store.suppress(
                    event_id, compaction.REASON_UNUSED, "2026-01-01T00:00:00+00:00"
                )
        before = self.store.suppressed_map()

        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertEqual(self.store.suppressed_map(), before)

    def test_110_catch_up_skipped_when_digest_valid_and_done(self) -> None:
        """MM-110. При `digest_status = done` и валидном файле дайджест не строится."""

        self.prepare_previous()
        first = digest.build_digest(
            self.store, self.config, self.root, self.logger, PREVIOUS, "work"
        )
        before = Path(first.path).read_bytes()
        suppressed_before = self.store.suppressed_map()

        self.handle(event(session_id=SESSION, is_first_turn=True))
        # Дайджест не пересоздаётся, уплотнение остаётся идемпотентным.
        self.assertEqual(Path(first.path).read_bytes(), before)
        self.assertEqual(self.store.suppressed_map(), suppressed_before)

    def test_110_damaged_digest_is_rebuilt_by_catch_up(self) -> None:
        """MM-110. Повреждённый файл даёт `digest_status != done` — файл строится заново."""

        self.prepare_previous()
        first = digest.build_digest(
            self.store, self.config, self.root, self.logger, PREVIOUS, "work"
        )
        Path(first.path).write_text("не дайджест вовсе", encoding="utf-8")

        self.handle(event(session_id=SESSION, is_first_turn=True))
        header = digest.read_header(Path(first.path))
        self.assertIsNotNone(header)
        self.assertIn("ответ номер 3", Path(first.path).read_text(encoding="utf-8"))

class CatchUpBudgetTests(LifecycleTestCase):
    """MM-75: таймаут catch-up откладывает работу, повтор ограничен."""

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def test_75_budget_exhausted_sets_pending_and_increments_attempts(self) -> None:
        """MM-75. Нехватка бюджета: `pending_catch_up` и attempts растут."""

        self.prepare_previous()
        result = catchup.run_catch_up(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            session.turn_from_event(event(session_id=SESSION, is_first_turn=True)),
            deadline_remaining_ms=10,
        )
        self.assertEqual(result.status, catchup.STATUS_TIMEOUT)
        row = self.session_row()
        self.assertEqual(row["pending_catch_up"], 1)
        self.assertEqual(row["catch_up_attempts"], 1)
        self.assertEqual(row["catch_up_target_session_id"], PREVIOUS)
        self.assertFalse(self.digest_path(PREVIOUS).exists())
        self.assertIn(catchup.OPERATION_TIMEOUT, log_operations(self.tmp))

    def test_75_retry_succeeds_on_next_turn(self) -> None:
        """MM-75. Повтор выполняется на следующем ходе той же сессии."""

        self.prepare_previous()
        turn = session.turn_from_event(event(session_id=SESSION, is_first_turn=True))
        catchup.run_catch_up(
            self.store, self.config, self.root, self.logger, "work", turn,
            deadline_remaining_ms=10,
        )
        retry = catchup.run_catch_up(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            session.turn_from_event(event(session_id=SESSION, turn_id="2")),
            deadline_remaining_ms=catchup.catch_up_budget_ms(self.config),
        )
        self.assertEqual(retry.status, catchup.STATUS_DONE)
        self.assertTrue(self.digest_path(PREVIOUS).exists())


class CatchUpStateLossTests(LifecycleTestCase):
    """§18.1 п.4: после rebuild-index журнал не сканируется."""

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))

    def test_state_unavailable_after_rebuild_index(self) -> None:
        """§18.1 п.4. Пустое состояние сессий: уплотнение и дайджест не создаются."""

        self.prepare_previous()
        self.close_store()
        indexer.rebuild_index(self.store, self.root, self.logger, "work", self.config)
        self.assertEqual(self.store.sessions_all("work"), [])

        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertIsNone(self.session_row().get("catch_up_target_session_id"))
        self.assertFalse(self.digest_path(PREVIOUS).exists())
        self.assertIn(catchup.OPERATION_STATE_UNAVAILABLE, log_operations(self.tmp))
        # Возврат читает только существующие валидные дайджесты — их нет.
        self.assertFalse(result.inserted)

    def test_state_unavailable_keeps_return_working_with_existing_digest(self) -> None:
        """§18.1 п.4. Имеющийся дайджест Возврат всё равно вставляет."""

        self.prepare_previous()
        built = digest.build_digest(
            self.store, self.config, self.root, self.logger, PREVIOUS, "work"
        )
        self.assertTrue(built.created_ok)
        self.close_store()
        indexer.rebuild_index(self.store, self.root, self.logger, "work", self.config)

        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertTrue(result.inserted)
        self.assertIn("ответ номер 3", result.text)
        self.assertIn(catchup.OPERATION_STATE_UNAVAILABLE, log_operations(self.tmp))

    def test_state_unavailable_does_not_read_journal(self) -> None:
        """§18.1 п.4. Журнал не сканируется: чтение дневных файлов запрещено."""

        self.prepare_previous()
        self.close_store()
        indexer.rebuild_index(self.store, self.root, self.logger, "work", self.config)

        original = indexer.index_journal_file

        def forbidden(*args, **kwargs):  # pragma: no cover - сработать не должно
            raise AssertionError("catch-up не читает журнал")

        indexer.index_journal_file = forbidden  # type: ignore[assignment]
        self.addCleanup(setattr, indexer, "index_journal_file", original)

        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertIn(catchup.OPERATION_STATE_UNAVAILABLE, log_operations(self.tmp))


class SessionEndLifecycleTests(LifecycleTestCase):
    """§18: уплотнение, затем дайджест; MM-52, MM-111, MM-113."""

    modes = {"mode_return": False, "mode_compaction": True}

    def prepare_session(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def test_52_digest_created_on_session_end_with_mode_return_false(self) -> None:
        """MM-52. При `mode_return = false` дайджест на on_session_end создаётся."""

        self.prepare_session()
        result = self.end_session(session_end_event(PREVIOUS, completed=True))
        self.assertTrue(result.created_ok)
        self.assertTrue(self.digest_path(PREVIOUS).exists())
        self.assertIn(compaction.OPERATION_APPLIED, log_operations(self.tmp))

    def test_session_end_runs_compaction_before_digest(self) -> None:
        """§18, §3.5. Порядок шагов: уплотнение, затем дайджест."""

        self.prepare_session()
        self.end_session(session_end_event(PREVIOUS, completed=True))
        operations = log_operations(self.tmp)
        self.assertLess(
            operations.index(compaction.OPERATION_APPLIED),
            operations.index("digest_created"),
        )
        header = digest.read_header(self.digest_path(PREVIOUS))
        self.assertIsNotNone(header)
        self.assertEqual(header.completion, digest.COMPLETION_FULL)
        self.assertEqual(self.session_row(PREVIOUS)["completed"], 1)

    def test_session_end_compaction_disabled_keeps_digest(self) -> None:
        """§3.7.7. При `mode_compaction = false` состояние снятия не меняется."""

        self.prepare_session()
        self.config = make_config(self.tmp, mode_return=False, mode_compaction=False)
        result = self.end_session(session_end_event(PREVIOUS, completed=True))
        self.assertTrue(result.created_ok)
        self.assertEqual(self.store.suppressed_count(), 0)
        self.assertIn(compaction.OPERATION_SKIPPED, log_operations(self.tmp))

    def test_interrupted_session_end_writes_completion(self) -> None:
        """П-17. Прерванная сессия: `completed = 0` и «Завершение: прервано»."""

        self.prepare_session()
        self.end_session(session_end_event(PREVIOUS, interrupted=True))
        self.assertEqual(self.session_row(PREVIOUS)["completed"], 0)
        header = digest.read_header(self.digest_path(PREVIOUS))
        self.assertIsNotNone(header)
        self.assertEqual(header.completion, digest.COMPLETION_INTERRUPTED)

    def test_session_end_hook_process_exits_zero_with_empty_output(self) -> None:
        """§19.2, §26.2. Хук всегда код 0 и пустой объект в stdout."""

        self.prepare_session()
        process = self.run_hook(SESSION_END_HOOK, session_end_event(PREVIOUS, completed=True))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout.decode("utf-8").strip(), "{}")




class ReliabilityTests(LifecycleTestCase):
    """Изоляция ошибок (40) и таймаут `pre_llm_call` (41)."""

    modes = {"mode_return": True, "mode_search": True, "mode_compaction": True}

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def _break(self, module, name: str) -> None:
        original = getattr(module, name)

        def broken(*args, **kwargs):
            raise RuntimeError("искусственный отказ модуля")

        setattr(module, name, broken)
        self.addCleanup(setattr, module, name, original)

    def test_40_catch_up_failure_does_not_break_turn(self) -> None:
        """MM-40. Отказ catch-up не выходит наружу и не ломает Возврат."""

        self.prepare_previous()
        built = digest.build_digest(
            self.store, self.config, self.root, self.logger, PREVIOUS, "work"
        )
        self.assertTrue(built.created_ok)
        self._break(catchup, "run_catch_up")

        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertTrue(result.inserted)
        self.assertIn("ответ номер 3", result.text)

    def test_40_compaction_failure_does_not_break_turn(self) -> None:
        """MM-40. Отказ уплотнения внутри catch-up не мешает Возврату."""

        self.prepare_previous()
        self._break(compaction, "run_compaction")
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        # Дайджест и вставка состоялись: отказ уплотнения изолирован.
        self.assertTrue(self.digest_path(PREVIOUS).exists())
        self.assertTrue(result.inserted)

    def test_40_digest_failure_does_not_break_turn(self) -> None:
        """MM-40. Отказ построения дайджеста не останавливает ход."""

        self.prepare_previous()
        self._break(digest, "build_digest")
        result = self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertFalse(result.inserted)
        row = self.session_row()
        self.assertEqual(row["pending_catch_up"], 1)
        self.assertEqual(row["catch_up_attempts"], 1)

    def test_40_session_end_failure_keeps_hook_exit_zero(self) -> None:
        """MM-40. Отказ шага `on_session_end` не выходит из хука."""

        self.prepare_previous()
        self._break(digest, "build_digest")
        process = self.run_hook(SESSION_END_HOOK, session_end_event(PREVIOUS, completed=True))
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout.decode("utf-8").strip(), "{}")

    def test_40_pre_llm_hook_exits_zero_on_broken_modules(self) -> None:
        """MM-40. Хук `pre_llm_call` завершается кодом 0 при отказе модулей.

        Отказ проверяется в том же процессе: `main()` — граница хука (§19.2),
        и подмена модуля должна дойти именно до неё.
        """

        import io
        from contextlib import redirect_stdout

        self.prepare_previous()
        self._break(ret, "perform_return")
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(make_config(self.tmp, **self.modes), ensure_ascii=False), encoding="utf-8"
        )
        previous_env = os.environ.get("MINIMEM_CONFIG")
        os.environ["MINIMEM_CONFIG"] = str(config_path)
        self.addCleanup(self._restore_config_env, previous_env)
        payload = json.dumps(event(session_id=SESSION, is_first_turn=True), ensure_ascii=False)
        stdout = io.StringIO()
        original_stdin = sys.stdin
        self.addCleanup(setattr, sys, "stdin", original_stdin)
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(stdout):
                code = self.pre_hook.main()
        finally:
            sys.stdin = original_stdin
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), "{}")
        record = log_records(self.tmp)[-1]
        self.assertEqual(record["status"], "error")



    def test_41_pre_llm_hook_fits_timeout(self) -> None:
        """MM-41. Полный набор шагов укладывается в предел 15 с."""

        self.prepare_previous()
        history = [{"role": "user", "content": "x" * 200} for _ in range(200)]
        payload = event(
            session_id=SESSION, is_first_turn=True, history=history, user="вопрос с поиском"
        )
        started = time.monotonic()
        process = self.run_hook(PRE_LLM_HOOK, payload, **self.modes)
        elapsed = time.monotonic() - started
        self.assertEqual(process.returncode, 0)
        self.assertLess(elapsed, float(DEFAULTS["pre_llm_timeout"]))

    def test_41_deadline_remaining_is_logged(self) -> None:
        """П-06. `deadline_remaining_ms` заполняется в основных операциях хода."""

        self.prepare_previous()
        self.handle(event(session_id=SESSION, is_first_turn=True))
        # Проверяются операции, ради которых ведётся дедлайн; вспомогательные
        # записи модулей (повреждённый файл, санитизация) его не несут.
        main_operations = {"compaction_detected", catchup.OPERATION_CATCH_UP, "return_injected"}
        records = [item for item in log_records(self.tmp) if item["operation"] in main_operations]
        self.assertTrue(records)
        for record in records:
            self.assertIn("deadline_remaining_ms", record)
        self.assertLess(
            min(item["deadline_remaining_ms"] for item in records),
            hook_deadline_ms(self.config, "pre_llm"),
        )


class ModeSwitchTests(LifecycleTestCase):
    """MM-55, MM-111, MM-113: переключение режимов и смежные проверки."""

    def prepare_previous(self) -> None:
        self.add_turns(3, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        self.mark_session(PREVIOUS, "2026-09-25T10:00:00+03:00")

    def test_55_switching_modes_needs_no_rebuild(self) -> None:
        """MM-55. Включение режимов не требует пересборки индекса."""

        self.prepare_previous()
        indexed_before = len(self.store.meta_event_ids())

        self.config = make_config(self.tmp, mode_return=True, mode_compaction=True)
        result = self.handle(event(session_id="2026-09-26-02", is_first_turn=True))
        self.assertTrue(self.digest_path(PREVIOUS).exists())
        self.assertTrue(result.inserted)
        # Индекс не перестраивался: те же event_id, тот же объём.
        self.assertEqual(len(self.store.meta_event_ids()), indexed_before)
        self.assertNotIn("rebuild_index", log_operations(self.tmp))

    def test_55_compaction_mode_toggle_keeps_records(self) -> None:
        """MM-55. Включение уплотнения не удаляет записи индекса."""

        self.prepare_previous()
        self.config = make_config(self.tmp, mode_return=True, mode_compaction=True)
        self.handle(event(session_id=SESSION, is_first_turn=True))
        self.assertEqual(len(self.store.meta_event_ids()), 3)
        self.assertEqual(self.store.suppressed_count(), 0)

    def test_111_digest_timeout_leaves_return_without_insert(self) -> None:
        """MM-111. Таймаут дайджеста при сжатии: вставки нет, причина в логе.

        Суббюджет дайджеста — 30 % от `hook_deadline_ms` (§19.1.1): при
        меньшем остатке файл не создаётся, а Возврат ничего не вставляет.
        """

        self.add_turns(3, session_id=SESSION)
        budget = catchup.digest_budget_ms(self.config)
        turn = session.turn_from_event(
            event(
                session_id=SESSION,
                turn_id="2",
                is_first_turn=False,
                history=[{"role": "user", "content": "y" * 50}],
            )
        )
        result = ret.perform_return(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            turn,
            session.SessionState(),
            decision=session.CompactionDecision(detected=True),
            deadline_remaining_ms=budget - 1,
        )
        self.assertFalse(result.inserted)
        self.assertEqual(result.reason, "digest_not_found")
        self.assertFalse(self.digest_path(SESSION).exists())
        record = next(
            item
            for item in reversed(log_records(self.tmp))
            if item["operation"] == "return_digest_timeout"
        )
        self.assertEqual(record["error_detail_code"], "digest_budget_exhausted")

    def test_111_digest_created_when_budget_sufficient(self) -> None:
        """MM-111. При достаточном остатке дайджест при сжатии создаётся."""

        self.add_turns(3, session_id=SESSION)
        turn = session.turn_from_event(
            event(session_id=SESSION, turn_id="2", is_first_turn=False)
        )
        result = ret.perform_return(
            self.store,
            self.config,
            self.root,
            self.logger,
            "work",
            turn,
            session.SessionState(),
            decision=session.CompactionDecision(detected=True),
            deadline_remaining_ms=catchup.digest_budget_ms(self.config),
        )
        self.assertTrue(result.inserted)
        self.assertTrue(self.digest_path(SESSION).exists())

    def test_113_cli_compact_ignores_mode_compaction_flag(self) -> None:
        """MM-113. CLI `compact` — административное действие вне флага режима."""

        self.add_turns(2, session_id=PREVIOUS, base=MOMENT - timedelta(days=1))
        config_path = self.root / "config.json"
        config = make_config(self.tmp, mode_compaction=False, compaction_rule2_enabled=False)
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        self.close_store()
        process = subprocess.run(
            [sys.executable, str(CODE_DIR / "minimem.py"), "compact"],
            capture_output=True,
            env={**dict(os.environ), "MINIMEM_CONFIG": str(config_path)},
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8"))
        self.assertIn("уплотнение", process.stdout.decode("utf-8").lower())
        self.assertEqual(self.store.suppressed_count(), 0)
