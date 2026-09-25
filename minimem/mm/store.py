"""Служебный слой SQLite: на этапе 1 — `meta` и `capture_guard`.

Основание: ТЗ v1.7 §4.2, §9.1 (схема служебного слоя), §5.1 (идемпотентный
гард захвата), §19.3 (WAL, транзакции).

База — производный слой: потеря файла не означает потерю памяти. Гард
идемпотентности работает в режиме best-effort: при недоступной базе
захват всё равно пишет запись в журнал (§5.1, MM-102).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

#: Версия схемы служебного слоя на этапе 1.
SCHEMA_VERSION = 1

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
        """Создаёт таблицы этапа 1 и записывает `schema_version` (§9.1)."""

        connection = self._require()
        try:
            with connection:
                connection.execute(_META_DDL)
                connection.execute(_CAPTURE_GUARD_DDL)
                self.set_meta("schema_version", str(SCHEMA_VERSION))
        except sqlite3.Error as exc:
            raise StoreUnavailable(type(exc).__name__) from exc

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
