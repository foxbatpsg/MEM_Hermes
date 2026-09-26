"""Построение поискового запроса по реплике пользователя.

Основание: ТЗ v1.7 §3.3, §9 (FTS5, шкала score), §12 (хук Поиска, п.3),
§12.1 (ограничения построения запроса), §6 (`max_search_query`,
`max_search_terms`, `search_min_word_len`, `search_stem_min_len`).

Запрос строится кодом, без модели и без внешних библиотек: реплика
разбивается на слова, слова короче `search_min_word_len` отбрасываются,
каждое слово усекается до основы по фиксированному списку окончаний,
дубликаты основ удаляются с сохранением порядка появления, количество
основ ограничивается `max_search_terms`. Пользовательский текст в FTS5-запрос
не попадает: операторы FTS5 нейтрализуются самой конструкцией `"основа"*`.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

#: Окончания для детерминированного усечения русских слов (§9, §12).
#:
#: Это не морфологический стеммер: список неизбежно даёт ошибки, но даёт
#: и recall — «память» и «памяти» получают одну основу «памят». Порядок
#: сравнения не влияет на результат: при равенстве длины окончаний выбирается
#: лексикографически первое, самые длинные проверяются раньше остальных.
STEM_ENDINGS: tuple[str, ...] = (
    "иями",
    "ями",
    "ами",
    "ыми",
    "ими",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "ах",
    "ях",
    "ой",
    "ей",
    "ый",
    "ий",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ую",
    "юю",
    "ов",
    "ев",
    "ью",
    "ия",
    "ем",
    "ом",
    "ам",
    "ям",
    "ла",
    "ло",
    "ли",
    "на",
    "но",
    "ны",
    "ся",
    "сь",
    "у",
    "ю",
    "а",
    "я",
    "ы",
    "и",
    "о",
    "е",
    "ь",
    "й",
)

#: Разделители слов: всё, кроме букв и цифр (§12 п.3).
#: `\w` в Python включает подчёркивание и буквы Unicode (кириллица равноправна
#: латинице), поэтому подчёркивание исключается отдельно.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def split_words(text: str) -> list[str]:
    """Разбивает текст на слова: разделители — всё, кроме букв и цифр (§12)."""

    return _WORD_RE.findall(text or "")


def stem_word(word: str, min_len: int) -> str:
    """Усекает слово до основы по списку окончаний (§12 п.3).

    Окончание отбрасывается, только если остаток не короче `min_len`
    (`search_stem_min_len`): иначе основа была бы короче любого слова,
    прошедшего отсев по `search_min_word_len`, и искала бы слишком широко.
    Слово без подходящего окончания остаётся как есть.
    """

    lowered = word.casefold()
    for ending in sorted(STEM_ENDINGS, key=len, reverse=True):
        if len(ending) >= len(lowered):
            continue
        if not lowered.endswith(ending):
            continue
        if len(lowered) - len(ending) >= min_len:
            return lowered[: -len(ending)]
    return lowered


def extract_stems(text: str, config: Mapping[str, Any]) -> list[str]:
    """Основы реплики в порядке появления без дублей (§12, §12.1).

    Пустой результат означает, что поиск не запускается (MM-108).
    """

    min_word = int(config["search_min_word_len"])
    min_stem = int(config["search_stem_min_len"])
    limit = int(config["max_search_terms"])
    stems: list[str] = []
    for word in split_words(text or ""):
        if len(word) < min_word:
            continue
        stem = stem_word(word, min_stem)
        if len(stem) < min_word or stem in stems:
            continue
        stems.append(stem)
        if len(stems) >= limit:
            break
    return stems


def build_match_query(stems: Sequence[str], mode: str = "all") -> str:
    """Собирает FTS5-запрос из основ: `"основа"*` (§12 п.3).

    `mode = "all"` соединяет основы через AND, `mode = "any"` — через OR.
    Текст пользователя в запрос не попадает, поэтому кавычки достаточно
    для нейтрализации операторов FTS5 (§12.1 п.4, п.6).
    """

    operator = " OR " if mode == "any" else " AND "
    return operator.join(f'"{stem}"*' for stem in stems)
