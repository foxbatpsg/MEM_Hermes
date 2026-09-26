"""Проверка поддержки FTS5 в интерпретаторе Hermes: диагностика окружения.

Вспомогательная проверка, не часть этапов плана.

Запуск: python -B tools/check_fts5.py
"""

from __future__ import annotations

import sqlite3
import sys

STATEMENTS = (
    "CREATE VIRTUAL TABLE t1 USING fts5(x)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS t2 USING fts5(x)",
    "CREATE VIRTUAL TABLE t3 USING fts5(x, tokenize = 'unicode61')",
    "CREATE VIRTUAL TABLE t4 USING fts5(x, y, z UNINDEXED, tokenize = 'unicode61')",
)


def main() -> int:
    print("python:", sys.version.split()[0], sys.executable)
    print("sqlite:", sqlite3.sqlite_version)
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        print("fts5: доступен")
    except sqlite3.Error as exc:
        print("fts5: недоступен —", type(exc).__name__, exc)
    for statement in STATEMENTS:
        try:
            connection.execute(statement)
            print("ok  ", statement)
        except sqlite3.Error as exc:
            print("FAIL", type(exc).__name__, exc, "|", statement)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
