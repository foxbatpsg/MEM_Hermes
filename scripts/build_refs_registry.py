#!/usr/bin/env python3
"""Генератор реестра ссылок корпуса: где что встречается. Python 3.9+, stdlib only.

Отвечает на вопрос «в каких местах упоминается норма» без чтения файлов: для каждой
нормы собираются определения и упоминания с координатами (файл и строка) и короткой
цитатой. Дополнительно собираются разделы корпуса (заголовки с номерами) — навигация.

Отличие от INDEX.md: индекс хранит смысл (канонический источник нормы, решения),
этот реестр — только координаты. Реестр производный: пересобирается из корпуса
и вручную не правится. Область индексации берётся из governance.json (ключ corpus).

Использование:
    python -B scripts/build_refs_registry.py             # записать REFS-REGISTRY.md/.json
    python -B scripts/build_refs_registry.py --format json
    python -B scripts/build_refs_registry.py --check     # сверить, не перезаписывая
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_index as vi  # noqa: E402  — конфигурация, регулярки и разбор Markdown

DEFAULT_OUTPUTS = {"md": "REFS-REGISTRY.md", "json": "REFS-REGISTRY.json"}
QUOTE_LIMIT = 80
SECTION_HEADING = re.compile(r"^(#{2,4})\s+(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$")


def find_root(explicit):
    if explicit:
        return Path(explicit).resolve()
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "governance.json").exists():
            return candidate
    return here.parents[1]


def source_hash(root, config):
    """Хеш индексируемой части: индекс плюс настроенный корпус."""
    digest = hashlib.sha256()
    for name in [config["index"]] + list(config["corpus"]):
        digest.update(name.encode("utf-8"))
        path = vi.local_path(root, name)
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def quote(line):
    text = re.sub(r"\s+", " ", line.strip())
    return text[:QUOTE_LIMIT] + ("…" if len(text) > QUOTE_LIMIT else "")


def collect(root, config):
    """Собирает определения, упоминания и разделы по настроенному корпусу."""
    pattern = config["id_pattern"]
    definition_re = re.compile(r"^\s*[^<\s].+?\((" + pattern + r")\):\s*\S")
    mention_re = re.compile(r"(?<![\w-])(?:" + pattern + r")(?![\w-])")
    norms = {}
    sections = []
    for name in config["corpus"]:
        path = vi.local_path(root, name)
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            print(f"Пропущен {name}: {exc}", file=sys.stderr)
            continue
        for number, line in vi.visible_lines(text):
            heading = SECTION_HEADING.match(line)
            if heading:
                sections.append(
                    {
                        "file": name,
                        "line": number,
                        "level": len(heading[1]),
                        "number": heading[2],
                        "title": heading[3],
                    }
                )
            definition = definition_re.match(line)
            defined = definition[1] if definition else None
            for ident in mention_re.findall(line):
                entry = norms.setdefault(ident, {"definitions": [], "mentions": []})
                kind = "definitions" if ident == defined else "mentions"
                item = {"file": name, "line": number, "quote": quote(line)}
                if item not in entry[kind]:
                    entry[kind].append(item)
    return {
        "source_hash": source_hash(root, config),
        "counts": {
            "норм": len(norms),
            "определений": sum(len(entry["definitions"]) for entry in norms.values()),
            "упоминаний": sum(len(entry["mentions"]) for entry in norms.values()),
            "разделов": len(sections),
        },
        "norms": dict(sorted(norms.items())),
        "sections": sections,
    }


def emit_markdown(payload, root):
    out = [
        "# REFS-REGISTRY — где что встречается в корпусе",
        "",
        "<!-- GENERATED FILE — не редактировать вручную. Источник: corpus из governance.json. -->",
        "<!-- Пересборка: python -B scripts/build_refs_registry.py -->",
        "",
        f"SOURCE-HASH: {payload['source_hash']}",
        " · ".join(f"{key.upper()}: {value}" for key, value in payload["counts"].items()),
        "",
        "**Как пользоваться.** `grep -n \"R-001\" REFS-REGISTRY.md` — все места нормы; "
        "точечный запрос: `python -B scripts/query_refs.py norm R-001`, "
        "`python -B scripts/query_refs.py section 23`.",
        "",
        "## 1. Нормы (определения и упоминания)",
        "",
        "Столбцы: `Вид` · `Норма` · `Файл` · `Строка` · `Фрагмент`",
        "",
        "```text",
    ]
    for ident, entry in payload["norms"].items():
        for kind, label in (("definitions", "definition"), ("mentions", "mention")):
            for item in entry[kind]:
                out.append(f"{label}\t{ident}\t{item['file']}\t{item['line']}\t{item['quote']}")
    out += [
        "```",
        "",
        "## 2. Разделы корпуса",
        "",
        "```text",
    ]
    for section in payload["sections"]:
        out.append(
            f"section\t{section['number']}\t{section['file']}\t{section['line']}\t"
            f"{'#' * section['level']} {section['title']}"
        )
    out += ["```", "", "*Реестр производный. При расхождении с корпусом источником считается корпус.*", ""]
    return "\n".join(out)


def emit_json(payload, root):
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


EMITTERS = {"md": emit_markdown, "json": emit_json}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Пересборка реестра ссылок корпуса")
    parser.add_argument("--root", default=None)
    parser.add_argument("--config", default="governance.json")
    parser.add_argument("--format", choices=sorted(EMITTERS) + ["all"], default="all")
    parser.add_argument("--out", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    root = find_root(args.root)
    try:
        config = vi.load_config(root, args.config)
        payload = collect(root, config)
    except (OSError, UnicodeError, ValueError, TypeError, re.error) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2

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
        print("Реестр ссылок актуален" if not stale else "Реестр ссылок устарел — пересобрать без --check")
        return 1 if stale else 0

    print("Собрано: " + " · ".join(f"{key}={value}" for key, value in payload["counts"].items()))
    print(f"SOURCE-HASH: {payload['source_hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
