#!/usr/bin/env python3
"""Запросы к машиночитаемому реестру карточек CARDS-REGISTRY.json. Python 3.9+.

Цель — отвечать на вопрос по карточкам одной командой, не читая файлы карточек
и не загружая markdown-реестр целиком.

    python -B scripts/query_registry.py card T-001      # одна карточка: где и о чём
    python -B scripts/query_registry.py norm R-001      # все карточки, связанные с нормой
    python -B scripts/query_registry.py task E01-T02    # карточки, связанные с задачей
    python -B scripts/query_registry.py find "журнал"   # поиск по названию, ID, нормам, задачам
    python -B scripts/query_registry.py stats           # сводка по реестру

Формат вывода — одна строка на карточку: `ID · файл:строка · название · нормы`.
Только стандартная библиотека; внешних зависимостей нет.
"""
import argparse
import json
import sys
from pathlib import Path

REGISTRY_JSON = "CARDS-REGISTRY.json"
REBUILD_HINT = "python -B scripts/build_cards_registry.py"


def find_root(explicit):
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "governance.json").exists():
            return candidate
    return here.parents[1]


def load(root):
    path = root / REGISTRY_JSON
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def line(card):
    norms = ",".join(card["norms"]) or "—"
    tasks = ",".join(card["tasks"])
    tail = f" · задачи: {tasks}" if tasks else ""
    return f"{card['id']}\t{card['file']}:{card['line']}\t{card['title']}\tнормы: {norms}{tail}"


def print_cards(cards, header):
    print(f"{header}: {len(cards)}")
    for card in cards:
        print(line(card))


def cmd_card(data, value):
    needle = value.upper()
    cards = [card for card in data["cards"] if card["id"].upper() == needle]
    if not cards:
        print(f"Карточка не найдена: {value}")
        return 1
    for card in cards:
        print(line(card))
        print(f"  маркеры: {','.join(card['markers']) or '—'}")
    return 0


def _ci_get(mapping, value):
    """Регистронезависимый доступ к срезам: идентификаторы бывают с суффиксом."""
    for key, ids in mapping.items():
        if key.upper() == value.upper():
            return key, ids
    return value.upper(), []


def _by_slice(data, value, key, label):
    found_key, ids = _ci_get(data[key], value)
    if not ids:
        print(f"{label} не встречается в карточках: {value}")
        return 1
    cards = [card for card in data["cards"] if card["id"] in ids]
    print_cards(cards, f"Карточки по {label.lower()} {found_key}")
    return 0


def cmd_find(data, value):
    needle = value.lower()
    cards = [
        card
        for card in data["cards"]
        if needle in card["title"].lower()
        or needle in card["id"].lower()
        or any(needle in item.lower() for item in card["norms"])
        or any(needle in item.lower() for item in card["tasks"])
        or any(needle in item.lower() for item in card["markers"])
    ]
    if not cards:
        print(f"Ничего не найдено: {value}")
        return 1
    print_cards(cards, f"Найдено по «{value}»")
    return 0


def cmd_stats(data, value=None):
    by_file = {}
    for card in data["cards"]:
        by_file[card["file"]] = by_file.get(card["file"], 0) + 1
    print(f"Карточек: {len(data['cards'])} · SOURCE-HASH: {data['source_hash']}")
    for name in sorted(by_file):
        print(f"  {name}: {by_file[name]}")
    print(
        f"  норм со связями: {len(data['norm_to_cards'])} · "
        f"задач со связями: {len(data['task_to_cards'])}"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Запросы к реестру карточек")
    parser.add_argument("command", choices=["card", "norm", "task", "find", "stats"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--root", default=None)
    args = parser.parse_args(argv)

    root = find_root(args.root)
    data = load(root)
    if data is None:
        print(f"Файл {REGISTRY_JSON} отсутствует. Пересобрать:\n  {REBUILD_HINT}", file=sys.stderr)
        return 1

    if args.command == "stats":
        return cmd_stats(data)
    if not args.value:
        print("Для этой команды нужно значение")
        return 2
    if args.command == "card":
        return cmd_card(data, args.value)
    if args.command == "find":
        return cmd_find(data, args.value)
    key, label = ("norm_to_cards", "норма") if args.command == "norm" else ("task_to_cards", "задача")
    return _by_slice(data, args.value, key, label)


if __name__ == "__main__":
    sys.exit(main())
