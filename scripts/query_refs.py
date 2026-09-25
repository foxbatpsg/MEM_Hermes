#!/usr/bin/env python3
"""Запросы к реестру ссылок корпуса: где встречается норма или раздел. Python 3.9+.

Отвечает одной командой — без чтения корпуса целиком:

    python -B scripts/query_refs.py norm R-001        # определение и все упоминания нормы
    python -B scripts/query_refs.py section 23        # якорь раздела
    python -B scripts/query_refs.py where "режимы"   # текстовый поиск по корпусу
    python -B scripts/query_refs.py stats            # сводка реестра

Цитаты берутся из исходных файлов по сохранённым координатам, поэтому в
машиночитаемом реестре хранятся только координаты. Только стандартная библиотека.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_index as vi  # noqa: E402  — путь к корпусу и разбор конфигурации

REFS_JSON = "REFS-REGISTRY.json"
# Кроме нормативного корпуса поиск идёт по процедуре проекта
SEARCH_EXTRA = ["AGENTS.md"]

_line_cache = {}


def find_root(explicit):
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "governance.json").exists():
            return candidate
    return here.parents[1]


def load_json(root, name):
    path = root / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_line(root, rel, number):
    lines = _line_cache.get(rel)
    if lines is None:
        path = root / rel
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        _line_cache[rel] = lines
    if 1 <= number <= len(lines):
        return lines[number - 1].strip()
    return ""


def show(root, kind, items, limit=None):
    total = len(items)
    shown = items if limit is None else items[:limit]
    print(f"{kind}: {total}" + (f" (показано {len(shown)})" if limit and total > len(shown) else ""))
    for item in shown:
        text = source_line(root, item["file"], item["line"])
        print(f"  {item['file']}:{item['line']}\t{text[:110]}")


def cmd_norm(root, data, value, limit):
    ident = value.upper()
    entry = data["norms"].get(ident)
    if not entry:
        print(f"Норма не встречается в корпусе: {value}")
        return 1
    show(root, "определения", entry["definitions"])
    show(root, "упоминания", entry["mentions"], limit)
    return 0


def cmd_section(root, data, value, limit):
    needle = value.lower()
    found = [
        section
        for section in data["sections"]
        if section["number"].startswith(needle) or needle in section["title"].lower()
    ]
    if not found:
        print(f"Раздел не найден: {value}")
        return 1
    shown = found if limit is None else found[:limit]
    print(f"разделы: {len(found)}" + (f" (показано {len(shown)})" if len(shown) < len(found) else ""))
    for section in shown:
        print(f"  {section['file']}:{section['line']}\t§{section['number']} {section['title']}")
    return 0


def cmd_where(root, config, value, limit):
    needle = value.lower()
    files = list(config["corpus"]) + SEARCH_EXTRA
    hits = []
    for name in files:
        path = vi.local_path(root, name)
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if needle in line.lower():
                hits.append((name, number, line.strip()))
    print(f"найдено: {len(hits)}")
    limit = limit or 40
    for name, number, line in hits[:limit]:
        print(f"  {name}:{number}\t{line[:110]}")
    if len(hits) > limit:
        print(f"  … и ещё {len(hits) - limit}; уточнить запрос или поднять --limit")
    return 0


def cmd_stats(root, data, value="", limit=None):
    print(f"SOURCE-HASH: {data['source_hash']}")
    print("  " + " · ".join(f"{key}: {value}" for key, value in data["counts"].items()))
    return 0


COMMANDS = {"norm": cmd_norm, "section": cmd_section}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Запросы к реестру ссылок корпуса")
    parser.add_argument("command", choices=["norm", "section", "where", "stats"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--root", default=None)
    parser.add_argument("--config", default="governance.json")
    args = parser.parse_args(argv)

    root = find_root(args.root)
    try:
        config = vi.load_config(root, args.config)
    except (OSError, UnicodeError, ValueError, TypeError, re.error) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    data = load_json(root, REFS_JSON)
    if data is None:
        print(
            f"Файл {REFS_JSON} отсутствует. Пересобрать:\n"
            f"  python -B scripts/build_refs_registry.py",
            file=sys.stderr,
        )
        return 1

    if args.command == "stats":
        return cmd_stats(root, data)
    if not args.value:
        print("Для этой команды нужно значение")
        return 2
    if args.command == "where":
        return cmd_where(root, config, args.value, args.limit)
    return COMMANDS[args.command](root, data, args.value, args.limit)


if __name__ == "__main__":
    sys.exit(main())
