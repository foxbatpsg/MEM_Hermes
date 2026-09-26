"""Разовое сравнение основ поиска: свой усечитель против Snowball.

Не тест и не часть системы: запускается вручную для сбора материала в RB-11.
Запуск из `minimem/`: `python -B tools/compare_stemming.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm import query  # noqa: E402

WORDS = [
    "память", "памяти", "памятю", "записками", "записки",
    "журнале", "журнал", "бэкап", "бэкапа", "поиска",
    "поиском", "восстановление", "восстановить", "утраченные",
    "сжимает", "накопление", "накопления", "переписывание",
]

#: Группы форм одного слова: главный критерий — станет ли запрос «памяти»
#: находить запись со словом «память».
FORMS = [
    ("память", "памяти", "памятю"),
    ("журнал", "журнале", "журналы"),
    ("записка", "записки", "записками"),
    ("восстановление", "восстановить", "восстановления"),
    ("накопление", "накопления", "накопить"),
    ("переписывание", "переписывается", "переписать"),
    ("поиск", "поиска", "поиском"),
    ("бэкап", "бэкапа", "бэкапу"),
]


def main() -> int:
    own = {word: query.stem_word(word, 4) for word in WORDS}

    try:
        from snowballstemmer import stemmer
    except ImportError:
        print("snowballstemmer недоступен в этом интерпретаторе")
        for word in WORDS:
            print(f"{word:16} -> {own[word]}")
        return 0

    snow = stemmer("russian")
    print(f"{'слово':16} {'наш':14} {'snowball':14} совпало")
    for word in WORDS:
        other = snow.stemWord(word)
        mark = "да" if other == own[word] else "НЕТ"
        print(f"{word:16} {own[word]:14} {other:14} {mark}")

    print()
    print("Совпадают ли формы одного слова между собой:")
    print(f"{'группа':40} {'наш':6} {'snowball':10}")
    for group in FORMS:
        mine = {own.get(word, query.stem_word(word, 4)) for word in group}
        theirs = {snow.stemWord(word) for word in group}
        print(f"{' / '.join(group):40} {'да' if len(mine) == 1 else 'НЕТ':6} "
              f"{'да' if len(theirs) == 1 else 'НЕТ':10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
