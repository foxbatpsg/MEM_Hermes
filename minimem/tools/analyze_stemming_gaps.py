"""Разбор корпуса: где усечение основ расходится само.

Не тест и не часть системы: измерительный скрипт, его вывод — материал для
RB-05. Запуск из `minimem/`: `python -B tools/analyze_stemming_gaps.py`

Показывает по журналу, к каким основам относятся несколько разных форм слова,
и какие хвосты из них следуют. Из этого получаются кандидаты на дописывание
окончаний — но с оговоркой, что автоматически найденный хвост нельзя брать
без проверки: он склеивает несвязанные слова.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm import paths, query  # noqa: E402

WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


def load_words(memory_root: Path) -> list[str]:
    """Русские слова корпуса из индекса; журнал не читается."""

    connection = sqlite3.connect(str(paths.sqlite_path(memory_root)))
    try:
        rows = connection.execute(
            "SELECT user_utterance, assistant_answer FROM memory_fts"
        ).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - индекс ещё не собран
        print(f"не удалось прочитать индекс: {type(exc).__name__}")
        return []
    finally:
        connection.close()
    words: list[str] = []
    for user, answer in rows:
        for word in WORD_RE.findall(f"{user or ''} {answer or ''}"):
            lowered = word.casefold()
            if len(lowered) >= 4 and CYRILLIC_RE.search(lowered):
                words.append(lowered)
    return words


def common_prefix(group: list[str]) -> str:
    prefix = group[0]
    for word in group[1:]:
        while not word.startswith(prefix):
            prefix = prefix[:-1]
    return prefix


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else paths.config_memory_root(
        {"memory_root": ""}
    )
    words = load_words(root)
    if not words:
        print("корпус пуст — нечего анализировать")
        return 1

    groups: dict[str, set[str]] = defaultdict(set)
    for word in set(words):
        groups[query.stem_word(word, 4)].add(word)
    sizes = Counter(len(forms) for forms in groups.values())

    print(f"русских слов в корпусе: {len(words)} (уникальных {len(set(words))})")
    print(f"основ после усечения: {len(groups)}")
    for count in sorted(sizes):
        print(f"  основ с {count} форм(ами): {sizes[count]}")

    print()
    print("Основы с несколькими формами и хвосты, которые из них следуют:")
    shown = 0
    for stem, forms in sorted(groups.items()):
        if len(forms) < 2:
            continue
        prefix = common_prefix(sorted(forms))
        tails = sorted(word[len(prefix):] for word in sorted(forms))
        print(f"  {stem:14} <- {sorted(forms)} хвосты={tails}")
        shown += 1
        if shown >= 20:
            print("  ... (показаны первые 20)")
            break

    print()
    print("Осторожно: короткий хвост склеивает несвязанные слова.")
    for tail in ("а", "ы", "у"):
        stems = sorted({word[: -len(tail)] for word in ("память", "код", "поиск", "файл") if word.endswith(tail)})
        print(f"  хвост {tail!r} стянул бы в одну основу: {stems}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
