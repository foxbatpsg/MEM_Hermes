#!/usr/bin/env python3
"""Генератор реестра карточек задач. Python 3.9+, stdlib only.

Назначение: дать дешёвый поиск по карточкам без чтения файлов целиком.
Реестр — производный артефакт: всегда пересобирается из карточек и не правится
вручную (актуальность проверяет `validate_index.py`).

Слой карточек настраивается файлом `cards.json` в корне проекта:

    {
      "files": ["Docs/cards/Stage-1.md"],
      "id_pattern": "T-[0-9]{3}",
      "title_pattern": "^\\*\\*Название:\\*\\*\\s*(.+)$",
      "task_pattern": "E[0-9]{2}-T[0-9]{2}[a-z]?",
      "markers": ["BLOCKED_BY", "BLOCKED", "DEFER"]
    }

`files` обязателен, остальные ключи необязательны: без `title_pattern` название
берётся из первой непустой строки карточки. Карточка — заголовок уровня 2,
целиком совпадающий с `id_pattern` («## T-001»). Нормы внутри карточки берутся
по `id_pattern` корпуса из governance.json.

Использование:
    python -B scripts/build_cards_registry.py                  # записать CARDS-REGISTRY.md/.json
    python -B scripts/build_cards_registry.py --format json
    python -B scripts/build_cards_registry.py --check          # сверить, не перезаписывая
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_index as vi  # noqa: E402  — конфигурация корпуса и регулярки норм

CARDS_CONFIG = "cards.json"
DEFAULT_OUTPUTS = {"md": "CARDS-REGISTRY.md", "json": "CARDS-REGISTRY.json"}
HEADING = re.compile(r"^##\s+(.+?)\s*$")


def find_root(explicit):
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "governance.json").exists():
            return candidate
    return here.parents[1]


def load_cards_config(root, name=CARDS_CONFIG):
    """Читает настройку слоя карточек. None — слой не настроен."""
    path = vi.local_path(root, name)
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise vi.InvalidConfig("cards.json must be a JSON object")
    files = config.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(x, str) for x in files):
        raise vi.InvalidConfig("cards.json: files must be a nonempty list of Markdown paths")
    if len(set(files)) != len(files):
        raise vi.InvalidConfig("cards.json: duplicate files")
    if any(Path(item).suffix != ".md" for item in files):
        raise vi.InvalidConfig("cards.json: files must be Markdown")
    for key in ("id_pattern", "task_pattern"):
        value = config.get(key)
        if value is not None and not isinstance(value, str):
            raise vi.InvalidConfig(f"cards.json: {key} must be a string")
    markers = config.get("markers")
    if markers is not None and (not isinstance(markers, list) or not all(isinstance(x, str) for x in markers)):
        raise vi.InvalidConfig("cards.json: markers must be a list of strings")
    id_pattern = config.get("id_pattern")
    if not id_pattern:
        raise vi.InvalidConfig("cards.json: id_pattern is required")
    regex = re.compile(id_pattern)
    if regex.groups or regex.fullmatch(""):
        raise vi.InvalidConfig("cards.json: id_pattern must not have capture groups or match an empty string")
    title_pattern = config.get("title_pattern")
    if title_pattern is not None and not isinstance(title_pattern, str):
        raise vi.InvalidConfig("cards.json: title_pattern must be a string")
    if title_pattern and re.compile(title_pattern).groups != 1:
        raise vi.InvalidConfig("cards.json: title_pattern needs exactly one capture group")
    return config


def card_files(root, cards):
    """Пути карточек в виде относительных путей с `/`."""
    return [str(vi.local_path(root, name).relative_to(root)).replace("\\", "/") for name in cards["files"]]


def source_hash(root, cards):
    """Хеш содержимого файлов карточек — признак актуальности реестра."""
    digest = hashlib.sha256()
    for name in cards["files"]:
        digest.update(name.encode("utf-8"))
        path = vi.local_path(root, name)
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def parse_cards(root, cards, gov):
    """Разбирает карточки в записи. Структура не зависит от формата вывода."""
    records = []
    id_re = re.compile(cards["id_pattern"])
    task_re = re.compile(cards["task_pattern"]) if cards.get("task_pattern") else None
    title_re = re.compile(cards["title_pattern"]) if cards.get("title_pattern") else None
    markers = cards.get("markers") or []
    mention_re = re.compile(r"(?<![\w-])(?:" + gov["id_pattern"] + r")(?![\w-])")
    for rel in card_files(root, cards):
        path = vi.local_path(root, rel)
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        heads = [
            (index, match[1])
            for index, line in enumerate(lines)
            for match in [HEADING.match(line)]
            if match and id_re.fullmatch(match[1])
        ]
        for position, (index, card_id) in enumerate(heads):
            end = heads[position + 1][0] if position + 1 < len(heads) else len(lines)
            body_lines = lines[index:end]
            body = "\n".join(body_lines)
            title = ""
            for line in body_lines[1:]:
                match = title_re.search(line) if title_re else None
                if match:
                    title = match[1].strip()
                    break
                if not title_re and line.strip():
                    title = line.strip()
                    break
            records.append(
                {
                    "id": card_id,
                    "file": rel,
                    "line": index + 1,
                    "title": title,
                    "norms": sorted(set(mention_re.findall(body))),
                    "tasks": sorted(set(task_re.findall(body))) if task_re else [],
                    "markers": sorted({marker for marker in markers if marker in body}),
                }
            )
    return records


def build_reverse(records, key):
    """Обратный срез: норма или задача → список карточек."""
    reverse = {}
    for record in records:
        for item in record[key]:
            reverse.setdefault(item, [])
            if record["id"] not in reverse[item]:
                reverse[item].append(record["id"])
    return dict(sorted((item, sorted(ids)) for item, ids in reverse.items()))


def build_payload(records, root, cards):
    """Каноническая структура данных — общая для всех эмиттеров."""
    return {
        "source_hash": source_hash(root, cards),
        "cards": records,
        "norm_to_cards": build_reverse(records, "norms"),
        "task_to_cards": build_reverse(records, "tasks"),
    }


def emit_markdown(payload, root):
    out = [
        "# CARDS-REGISTRY — реестр карточек",
        "",
        "<!-- GENERATED FILE — не редактировать вручную. Источник: cards.json и файлы карточек. -->",
        "<!-- Пересборка: python -B scripts/build_cards_registry.py -->",
        "",
        f"SOURCE-HASH: {payload['source_hash']}",
        f"CARDS: {len(payload['cards'])}",
        "",
        "**Как пользоваться.** `grep -n \"T-001\" CARDS-REGISTRY.md` — карточка по ID; "
        "`grep -n \"R-001\" CARDS-REGISTRY.md` — карточки, связанные с нормой; "
        "точечный запрос: `python -B scripts/query_registry.py norm R-001`.",
        "",
        "## 1. Карточки (одна строка на карточку)",
        "",
        "Столбцы: `ID` · `Файл` · `Строка` · `Название` · `Нормы` · `Задачи` · `Маркеры`",
        "",
        "```text",
    ]
    for card in payload["cards"]:
        out.append(
            f"{card['id']}\t{card['file']}\t{card['line']}\t{card['title']}\t"
            f"{','.join(card['norms'])}\t{','.join(card['tasks'])}\t{','.join(card['markers'])}"
        )
    out += ["```", "", "## 2. Норма → карточки", "", "```text"]
    for norm, ids in payload["norm_to_cards"].items():
        out.append(f"{norm}\t{','.join(ids)}")
    out += ["```", "", "## 3. Задача → карточки", "", "```text"]
    for task, ids in payload["task_to_cards"].items():
        out.append(f"{task}\t{','.join(ids)}")
    out += [
        "```",
        "",
        "*Реестр производный. При расхождении с карточками источником считается карточка, "
        "а реестр пересобирается скриптом.*",
        "",
    ]
    return "\n".join(out)


def emit_json(payload, root):
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


EMITTERS = {"md": emit_markdown, "json": emit_json}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Пересборка реестра карточек")
    parser.add_argument("--root", default=None)
    parser.add_argument("--config", default="governance.json")
    parser.add_argument("--cards", default=CARDS_CONFIG)
    parser.add_argument("--format", choices=sorted(EMITTERS) + ["all"], default="all")
    parser.add_argument("--out", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    root = find_root(args.root)
    try:
        gov = vi.load_config(root, args.config)
        cards = load_cards_config(root, args.cards)
        if cards is None:
            print(f"Слой карточек не настроен: нет {args.cards}. Создать файл по образцу в docstring.")
            return 1
        records = parse_cards(root, cards, gov)
    except (OSError, UnicodeError, ValueError, TypeError, re.error) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

    if not records:
        print(f"Карточки не найдены — проверить {args.cards} и --root")
        return 1

    payload = build_payload(records, root, cards)
    formats = list(DEFAULT_OUTPUTS) if args.format == "all" else [args.format]
    if args.out and len(formats) > 1:
        print("--out допускается только с одним форматом")
        return 2

    stale = False
    for fmt in formats:
        content = EMITTERS[fmt](payload, root)
        out_path = Path(args.out) if args.out else root / DEFAULT_OUTPUTS[fmt]
        if args.check:
            current = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            if current != content:
                print(f"устарел: {out_path.name}")
                stale = True
            continue
        out_path.write_text(content, encoding="utf-8")
        print(f"Записан {out_path.name}")

    if args.check:
        print("Реестр актуален" if not stale else "Реестр устарел — пересобрать без --check")
        return 1 if stale else 0

    print(f"Карточек: {len(records)}")
    print(f"SOURCE-HASH: {payload['source_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


