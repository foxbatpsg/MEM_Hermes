"""Служебный слой SQLite.

Основание: ТЗ v1.7 §4.2, §9.1 (схема служебного слоя), §9.2
(инкрементальная индексация), §5.1 (гард захвата), §19.3 (WAL, транзакции).

База — производный слой: потеря файла не означает потерю памяти (§4.3).
Гард идемпотентности работает в режиме best-effort: при недоступной базе
захват всё равно пишет запись в журнал (§5.1, MM-102).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

#: Версия схемы служебного слоя.
SCHEMA_VERSION = 2

#: Таймаут ожидания блокировки SQLite (§19.4).
BUSY_TIMEOUT_MS = 5000

_META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
)
"""

_CAPTURE_GUARD_DDL = """
CREATE TABLE IF NOT EXISTS capture_guard (
  event_id TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  journal_file TEXT NOT NULL,
  journal_offset INTEGER NOT NULL,
  created_at TEXT NOT NULL
)
"""

_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  user_utterance,
  assistant_answer,
  event_id UNINDEXED,
  tokenize = 'unicode61'
)
"""

_META_RECORD_DDL = """
CREATE TABLE IF NOT EXISTS memory_meta (
  event_id TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  project TEXT NOT NULL,
  session_id TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  turn TEXT NOT NULL,
  turn_numeric INTEGER,
  timestamp TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  truncated TEXT NOT NULL CHECK (truncated IN ('none', 'user', 'assistant', 'both')),
  journal_file TEXT NOT NULL,
  journal_offset INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  fmt INTEGER NOT NULL DEFAULT 1
)
"""

_INDEXES_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_meta_project_session_turn "
    "ON memory_meta (project, session_id, turn_numeric, turn)",
    "CREATE INDEX IF NOT EXISTS idx_meta_project_timestamp_utc "
    "ON memory_meta (project, timestamp_utc)",
    "CREATE INDEX IF NOT EXISTS idx_meta_content_hash_project "
    "ON memory_meta (content_hash, project)",
)

_USAGE_DDL = """
CREATE TABLE IF NOT EXISTS usage_counters (
  event_id TEXT PRIMARY KEY,
  usage_count INTEGER NOT NULL DEFAULT 0,
  last_used_at TEXT
)
"""

_SUPPRESSED_DDL = """
CREATE TABLE IF NOT EXISTS suppressed_records (
  event_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  suppressed_at TEXT NOT NULL
)
"""

_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  last_known_turn TEXT,
  max_history_len INTEGER,
  max_msgs INTEGER,
  max_chars INTEGER,
  digest_file TEXT,
  digest_status TEXT NOT NULL DEFAULT 'none',
  pending_catch_up INTEGER NOT NULL DEFAULT 0,
  catch_up_target_session_id TEXT,
  catch_up_attempts INTEGER NOT NULL DEFAULT 0,
  completed INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  PRIMARY KEY (session_id, project)
)
"""

_SESSION_RETURNS_DDL = """
CREATE TABLE IF NOT EXISTS session_returns (
  session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  event_id TEXT NOT NULL,
  returned_at TEXT NOT NULL,
  PRIMARY KEY (session_id, project, event_id)
)
"""

_CURSORS_DDL = """
CREATE TABLE IF NOT EXISTS journal_cursors (
  journal_file TEXT PRIMARY KEY,
  byte_offset INTEGER NOT NULL,
  file_size INTEGER,
  mtime REAL,
  last_error TEXT,
  updated_at TEXT
)
"""

#: Полный набор таблиц служебного слоя в порядке создания (§9.1).
SCHEMA_STATEMENTS: tuple[str, ...] = (
    _META_DDL,
    _CAPTURE_GUARD_DDL,
    _FTS_DDL,
    _META_RECORD_DDL,
    *_INDEXES_DDL,
    _USAGE_DDL,
    _SUPPRESSED_DDL,
    _SESSIONS_DDL,
    _SESSION_RETURNS_DDL,
    _CURSORS_DDL,
)


class StoreUnavailable(Exception):
    """SQLite недоступен: захват продолжает работу без гарда (§5.1)."""


class Store:
    """Обёртка над SQLite-файлом служебного слоя."""

    def __init__(self, db_path: Path, create: bool = True) -> None:
        self.path = Path(db_path)
        self._connection: sqlite3.Connection | None = None
        if create:
            self._connection = self._connect()

    def _connect(self) -> sqlite3.Connection:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_MS / 1000)
        except (sqlite3.Error, OSError) as exc:
            raise StoreUnavailable(type(exc).__name__) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except sqlite3.Error as exc:
            connection.close()
            raise StoreUnavailable(type(exc).__name__) from exc
        return connection

    @property
    def available(self) -> bool:
        return self._connection is not None

    @property
    def connection(self) -> sqlite3.Connection:
        """Открытое соединение; используется индексатором для транзакций."""

        return self._require()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreUnavailable("соединение не открыто")
        return self._connection

    def create_schema(self) -> None:
        """Создаёт таблицы служебного слоя и пишет `schema_version` (§9.1).

        `schema_version` записывается отдельной транзакцией до остальных
        объектов: иначе откат при ошибке на позднем DDL откатывал бы и его,
        и база оставалась бы без версии при формально созданных таблицах.
        """

        connection = self._require()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            with connection:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"{type(exc).__name__}: {exc}") from exc

        try:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise StoreUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def schema_version(self) -> int | None:
        """Версия схемы из `meta` или None, если она не записана."""

        raw = self.get_meta("schema_version")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def fts5_available(self) -> bool:
        """Доступен ли FTS5 в этой сборке SQLite (проверено при установке)."""

        try:
            self._require().execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS temp._fts_probe USING fts5(x)"
            )
            return True
        except sqlite3.Error:
            return False

    def drop_index_layer(self) -> None:
        """Удаляет индексный слой, оставляя `capture_guard` и `meta` (§4.3).

        `capture_guard` — best-effort состояние идемпотентности: его можно
        частично восстановить из `memory_meta` после пересборки.
        """

        connection = self._require()
        with connection:
            for statement in (
                "DROP TABLE IF EXISTS memory_fts",
                "DROP TABLE IF EXISTS memory_meta",
                "DROP TABLE IF EXISTS usage_counters",
                "DROP TABLE IF EXISTS suppressed_records",
                "DROP TABLE IF EXISTS sessions",
                "DROP TABLE IF EXISTS session_returns",
                "DROP TABLE IF EXISTS journal_cursors",
            ):
                connection.execute(statement)

    # --- memory_meta ------------------------------------------------------

    def meta_get(self, event_id: str) -> dict[str, Any] | None:
        """Строка `memory_meta` по event_id или None."""

        row = self._require().execute(
            "SELECT * FROM memory_meta WHERE event_id = ?", (event_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def meta_upsert(self, record: Mapping[str, Any]) -> None:
        """Вставляет или заменяет строку `memory_meta` (§9.1)."""

        self._require().execute(
            "INSERT INTO memory_meta (event_id, content_hash, project, session_id, task_id, "
            "turn, turn_numeric, timestamp, timestamp_utc, truncated, journal_file, "
            "journal_offset, revision, fmt) "
            "VALUES (:event_id, :content_hash, :project, :session_id, :task_id, :turn, "
            ":turn_numeric, :timestamp, :timestamp_utc, :truncated, :journal_file, "
            ":journal_offset, :revision, :fmt) "
            "ON CONFLICT(event_id) DO UPDATE SET content_hash = excluded.content_hash, "
            "project = excluded.project, session_id = excluded.session_id, "
            "task_id = excluded.task_id, turn = excluded.turn, "
            "turn_numeric = excluded.turn_numeric, timestamp = excluded.timestamp, "
            "timestamp_utc = excluded.timestamp_utc, truncated = excluded.truncated, "
            "journal_file = excluded.journal_file, journal_offset = excluded.journal_offset, "
            "revision = excluded.revision, fmt = excluded.fmt",
            dict(record),
        )

    def meta_event_ids(self) -> set[str]:
        """Все event_id, присутствующие в индексе."""

        rows = self._require().execute("SELECT event_id FROM memory_meta").fetchall()
        return {row["event_id"] for row in rows}

    def meta_count(self) -> int:
        row = self._require().execute("SELECT COUNT(*) AS n FROM memory_meta").fetchone()
        return int(row["n"] if row else 0)

    def meta_by_project(self, project: str) -> list[dict[str, Any]]:
        rows = self._require().execute(
            "SELECT * FROM memory_meta WHERE project = ? ORDER BY journal_offset", (project,)
        ).fetchall()
        return [dict(row) for row in rows]

    def meta_by_session(self, project: str, session_id: str) -> list[dict[str, Any]]:
        """Записи сессии в порядке ходов; ключ сортировки включает event_id.

        Производный `turn` (`turn_numeric IS NULL`) упорядочивается после
        числовых, иначе порядок зависел бы от типа сортировки SQLite.
        """

        rows = self._require().execute(
            "SELECT * FROM memory_meta WHERE project = ? AND session_id = ? "
            "ORDER BY (turn_numeric IS NULL), turn_numeric, turn, journal_offset, event_id",
            (project, session_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def meta_sessions(self, project: str | None = None) -> list[dict[str, Any]]:
        """Список пар (project, session_id), представленных в индексе.

        Источник списка сессий для Дайджеста и `rebuild-digest`: журнал при
        этом не сканируется (ТЗ v1.7 §14, MM-58).
        """

        sql = "SELECT DISTINCT project, session_id FROM memory_meta"
        params: list[Any] = []
        if project is not None:
            sql += " WHERE project = ?"
            params.append(project)
        sql += " ORDER BY project, session_id"
        rows = self._require().execute(sql, params).fetchall()
        return [{"project": row["project"], "session_id": row["session_id"]} for row in rows]

    def body_get(self, event_id: str) -> dict[str, Any] | None:
        """Текст записи из `memory_fts` по event_id или None.

        `body_status = withheld` (П-08) даёт пустой текст: пустые строки —
        признак отсутствия тела, а не пустой реплики.
        """

        row = self._require().execute(
            "SELECT user_utterance, assistant_answer FROM memory_fts WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    # --- memory_fts -------------------------------------------------------

    def fts_upsert(self, event_id: str, user_text: str, assistant_text: str) -> None:
        """Добавляет или заменяет индексную единицу FTS5 (§9)."""

        connection = self._require()
        connection.execute("DELETE FROM memory_fts WHERE event_id = ?", (event_id,))
        connection.execute(
            "INSERT INTO memory_fts (user_utterance, assistant_answer, event_id) "
            "VALUES (?, ?, ?)",
            (user_text, assistant_text, event_id),
        )

    def fts_delete(self, event_id: str) -> None:
        self._require().execute("DELETE FROM memory_fts WHERE event_id = ?", (event_id,))

    def fts_count(self) -> int:
        row = self._require().execute("SELECT COUNT(*) AS n FROM memory_fts").fetchone()
        return int(row["n"] if row else 0)

    def fts_search(self, match_query: str, project: str | None = None) -> list[tuple[str, float]]:
        """Поиск по FTS5; возвращает `(event_id, score)`, где больше — лучше (§9)."""

        sql = (
            "SELECT f.event_id AS event_id, -bm25(memory_fts) AS score FROM memory_fts f "
            "JOIN memory_meta m ON m.event_id = f.event_id WHERE memory_fts MATCH ?"
        )
        params: list[Any] = [match_query]
        if project is not None:
            sql += " AND m.project = ?"
            params.append(project)
        sql += " ORDER BY score DESC"
        rows = self._require().execute(sql, params).fetchall()
        return [(row["event_id"], float(row["score"])) for row in rows]

    # --- journal_cursors --------------------------------------------------

    def cursor_get(self, journal_file: str) -> dict[str, Any] | None:
        """Курсор индексатора по файлу журнала или None (§9.1)."""

        row = self._require().execute(
            "SELECT * FROM journal_cursors WHERE journal_file = ?", (journal_file,)
        ).fetchone()
        return dict(row) if row is not None else None

    def cursor_upsert(
        self,
        journal_file: str,
        byte_offset: int,
        file_size: int | None,
        mtime: float | None,
        updated_at: str,
        last_error: str | None = None,
    ) -> None:
        """Пишет курсор; вызывается только после успешной индексации (§9.2 п.11)."""

        self._require().execute(
            "INSERT INTO journal_cursors "
            "(journal_file, byte_offset, file_size, mtime, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(journal_file) DO UPDATE SET byte_offset = excluded.byte_offset, "
            "file_size = excluded.file_size, mtime = excluded.mtime, "
            "last_error = excluded.last_error, updated_at = excluded.updated_at",
            (journal_file, int(byte_offset), file_size, mtime, last_error, updated_at),
        )

    def cursor_all(self) -> list[dict[str, Any]]:
        rows = self._require().execute(
            "SELECT * FROM journal_cursors ORDER BY journal_file"
        ).fetchall()
        return [dict(row) for row in rows]

    # --- suppressed_records -----------------------------------------------

    def suppress(self, event_id: str, reason: str, suppressed_at: str) -> None:
        self._require().execute(
            "INSERT INTO suppressed_records (event_id, reason, suppressed_at) "
            "VALUES (?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET reason = excluded.reason",
            (event_id, reason, suppressed_at),
        )

    def suppressed_ids(self) -> set[str]:
        rows = self._require().execute("SELECT event_id FROM suppressed_records").fetchall()
        return {row["event_id"] for row in rows}

    def clear_suppressed(self) -> None:
        self._require().execute("DELETE FROM suppressed_records")

    # --- sessions ---------------------------------------------------------

    def session_upsert(
        self,
        session_id: str,
        project: str,
        **fields: Any,
    ) -> None:
        """Создаёт или обновляет строку сессии; `None` в `fields` не пишется.

        Список колонок фиксирован (§9.1): неизвестное имя приводит к
        `TypeError`, а не к молчаливой потере значения.
        """

        allowed = (
            "last_known_turn",
            "max_history_len",
            "max_msgs",
            "max_chars",
            "digest_file",
            "digest_status",
            "pending_catch_up",
            "catch_up_target_session_id",
            "catch_up_attempts",
            "completed",
            "last_seen_at",
        )
        unknown = set(fields) - set(allowed)
        if unknown:
            raise TypeError(f"Неизвестные поля sessions: {sorted(unknown)}")
        updates = {key: value for key, value in fields.items() if value is not None}
        self._require().execute(
            "INSERT OR IGNORE INTO sessions (session_id, project) VALUES (?, ?)",
            (session_id, project),
        )
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        self._require().execute(
            f"UPDATE sessions SET {assignments} WHERE session_id = ? AND project = ?",
            (*updates.values(), session_id, project),
        )

    def session_get(self, session_id: str, project: str) -> dict[str, Any] | None:
        row = self._require().execute(
            "SELECT * FROM sessions WHERE session_id = ? AND project = ?",
            (session_id, project),
        ).fetchone()
        return dict(row) if row is not None else None

    def sessions_all(self, project: str | None = None) -> list[dict[str, Any]]:
        """Строки `sessions`; без `project` — по всем проектам (§9.1)."""

        sql = "SELECT * FROM sessions"
        params: list[Any] = []
        if project is not None:
            sql += " WHERE project = ?"
            params.append(project)
        sql += " ORDER BY project, session_id"
        rows = self._require().execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def session_by_digest_file(self, digest_file: str) -> list[dict[str, Any]]:
        """Сессии, отображающиеся в указанный файл дайджеста (П-12)."""

        rows = self._require().execute(
            "SELECT session_id, project, digest_file FROM sessions "
            "WHERE digest_file = ? ORDER BY project, session_id",
            (digest_file,),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- usage_counters ---------------------------------------------------

    def usage_of(self, event_id: str) -> int:
        row = self._require().execute(
            "SELECT usage_count FROM usage_counters WHERE event_id = ?", (event_id,)
        ).fetchone()
        return int(row["usage_count"] if row else 0)

    def bump_usage(self, event_id: str, used_at: str) -> None:
        self._require().execute(
            "INSERT INTO usage_counters (event_id, usage_count, last_used_at) VALUES (?, 1, ?) "
            "ON CONFLICT(event_id) DO UPDATE SET usage_count = usage_count + 1, "
            "last_used_at = excluded.last_used_at",
            (event_id, used_at),
        )


    def set_meta(self, key: str, value: str) -> None:
        self._require().execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._require().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def all_meta(self) -> dict[str, str]:
        rows = self._require().execute("SELECT key, value FROM meta").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def guard_get(self, event_id: str) -> dict[str, Any] | None:
        """Запись гарда по event_id или None, если ход ещё не записывался."""

        row = self._require().execute(
            "SELECT event_id, content_hash, journal_file, journal_offset, created_at "
            "FROM capture_guard WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def guard_put(
        self,
        event_id: str,
        content_hash: str,
        journal_file: str,
        journal_offset: int,
        created_at: str,
    ) -> None:
        self._require().execute(
            "INSERT INTO capture_guard "
            "(event_id, content_hash, journal_file, journal_offset, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(event_id) DO UPDATE SET content_hash = excluded.content_hash, "
            "journal_file = excluded.journal_file, journal_offset = excluded.journal_offset, "
            "created_at = excluded.created_at",
            (event_id, content_hash, journal_file, int(journal_offset), created_at),
        )

    def guard_count(self) -> int:
        row = self._require().execute("SELECT COUNT(*) AS n FROM capture_guard").fetchone()
        return int(row["n"] if row else 0)

    def set_meta_many(self, values: Mapping[str, str]) -> None:
        connection = self._require()
        with connection:
            for key, value in values.items():
                self.set_meta(key, value)

    def set_meta_many_if_changed(self, values: Mapping[str, str]) -> list[str]:
        """Записывает изменившиеся ключи и возвращает их список (П-14)."""

        current = self.all_meta()
        changed = [key for key, value in values.items() if current.get(key) != value]
        if changed:
            self.set_meta_many({key: values[key] for key in changed})
        return changed

    def init_meta_from_config(
        self,
        config_hash: str,
        minimem_version: str,
        project_mapping_hash: str,
    ) -> list[str]:
        """Записывает `config_hash`, `minimem_version`, `project_mapping_hash` (§9.1)."""

        return self.set_meta_many_if_changed(
            {
                "config_hash": config_hash,
                "minimem_version": minimem_version,
                "project_mapping_hash": project_mapping_hash,
            }
        )

    # --- отметки о повреждённых записях (П-13) ----------------------------

    def damaged_reported(self, key: str) -> bool:
        """Писалась ли уже строка лога для этой повреждённой записи (П-13).

        Ключ — связка (файл, смещение, mtime): повторные обнаружения той же
        связки не порождают новых строк лога (§4.5.3 п.7).
        """

        return self.get_meta(f"damaged:{key}") is not None

    def mark_damaged_reported(self, key: str) -> None:
        """Помечает связку (файл, смещение, mtime) как уже залогированную."""

        self.set_meta(f"damaged:{key}", "1")

