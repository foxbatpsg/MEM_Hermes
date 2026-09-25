#!/usr/bin/env python3
"""Portable documentation registry validator. Python 3.9+, standard library only."""
import argparse
import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


class InvalidConfig(ValueError):
    pass


def local_path(root, name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise InvalidConfig("Paths must be nonempty strings using forward slashes")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or ":" in name:
        raise InvalidConfig("Only relative paths within the project are allowed: " + name)
    resolved = (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        raise InvalidConfig("Path escapes project root: " + name)
    return resolved


def load_config(root, config_name):
    config = json.loads(local_path(root, config_name).read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise InvalidConfig("Config must be a JSON object")
    expected = {"index", "corpus", "id_pattern", "required_ids", "registry_heading", "strict"}
    if set(config) != expected:
        raise InvalidConfig("Config keys must be exactly: " + ", ".join(sorted(expected)))
    corpus = config["corpus"]
    if not isinstance(corpus, list) or not all(isinstance(x, str) for x in corpus):
        raise InvalidConfig("corpus must be a list of explicit Markdown paths; empty is valid before the first specification")
    if len(set(corpus)) != len(corpus):
        raise InvalidConfig("Duplicate corpus paths")
    paths = [local_path(root, item) for item in corpus]
    index = local_path(root, config["index"])
    if len(set(paths)) != len(paths) or index in paths:
        raise InvalidConfig("Corpus must not contain aliases or the index itself")
    if any(path.suffix != ".md" for path in paths + [index]):
        raise InvalidConfig("Corpus and index must be Markdown files")
    pattern = config["id_pattern"]
    if not isinstance(pattern, str):
        raise InvalidConfig("id_pattern must be a string")
    regex = re.compile(pattern)
    if regex.groups or regex.fullmatch(""):
        raise InvalidConfig("id_pattern must not have capture groups or match an empty string")
    required = config["required_ids"]
    if not isinstance(required, list) or not all(isinstance(x, str) and regex.fullmatch(x) for x in required):
        raise InvalidConfig("required_ids must contain valid IDs")
    if len(set(required)) != len(required):
        raise InvalidConfig("Duplicate required_ids")
    heading = config["registry_heading"]
    if not isinstance(heading, str) or not heading.startswith("## ") or "\n" in heading:
        raise InvalidConfig("registry_heading must be a single level-2 heading")
    if not isinstance(config["strict"], bool):
        raise InvalidConfig("strict must be boolean")
    return config


def visible_lines(text):
    """Ignore fenced examples and standalone comments, but retain explicit HTML anchors."""
    fence = None
    comment = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", stripped):
                fence = None
            continue
        match = re.match(r"^(`{3,}|~{3,})", stripped)
        if match:
            fence = (match[1][0], len(match[1]))
            continue
        if comment:
            if "-->" in stripped:
                comment = False
            continue
        if stripped.startswith("<!--"):
            comment = "-->" not in stripped
            continue
        yield number, line


def registry(text, config, errors):
    records = {}
    active = False
    heading_count = 0
    header_seen = False
    id_re = re.compile(config["id_pattern"])
    for number, line in visible_lines(text):
        if line.startswith("## "):
            active = line.strip() == config["registry_heading"]
            heading_count += int(active)
        if not active or not line.strip().startswith("|"):
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", x) for x in cells):
            continue
        if cells == ["ID", "Title", "Canonical", "Status"]:
            header_seen = True
            continue
        if len(cells) != 4 or not id_re.fullmatch(cells[0]):
            errors.append(f"INDEX:{number}: invalid registry row")
            continue
        ident, title, canonical, status = cells
        if ident in records:
            errors.append(f"Duplicate registry row: {ident}")
        if not title or status not in {"active", "open", "gap", "retired"}:
            errors.append(f"{ident}: empty title or invalid status")
        records[ident] = (canonical, status)
    if heading_count != 1 or not header_seen:
        errors.append("Index requires exactly one registry section with ID/Title/Canonical/Status header")
    return records


GENERATED_REGISTRIES = (
    ("build_refs_registry", {"md": "REFS-REGISTRY.md", "json": "REFS-REGISTRY.json"}),
    ("build_cards_registry", {"md": "CARDS-REGISTRY.md", "json": "CARDS-REGISTRY.json"}),
)
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+?\.md)(?:#[^)\s]*)?\)")
MD_BACKTICK = re.compile(r"`([^`\s]+?\.md)(?:#[^`\s]*)?`")


def load_sibling(name):
    """Загружает соседний скрипт из каталога валидатора (генератор реестра)."""
    path = Path(__file__).with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("generator_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generated_payload(name, module, root, config):
    """Собирает данные реестра так же, как это делает его генератор."""
    if name == "build_refs_registry":
        return module.collect(root, config)
    cards = module.load_cards_config(root)
    if cards is None:
        return None
    return module.build_payload(module.parse_cards(root, cards, config), root, cards)


def check_generated_registries(root, config, errors):
    """Присутствующий генерируемый реестр обязан совпадать со своей пересборкой."""
    for name, outputs in GENERATED_REGISTRIES:
        present = sorted(
            (fmt, root / filename) for fmt, filename in outputs.items() if (root / filename).exists()
        )
        if not present:
            continue
        try:
            module = load_sibling(name)
            payload = generated_payload(name, module, root, config)
        except (OSError, UnicodeError, ValueError, TypeError, re.error, ImportError) as exc:
            errors.append(f"{name}: не удалось пересобрать реестр для сверки: {exc}")
            continue
        if payload is None:
            continue
        for fmt, path in present:
            try:
                content = module.EMITTERS[fmt](payload, root)
            except (TypeError, ValueError) as exc:
                errors.append(f"{path.name}: сбой эмиттера {fmt}: {exc}")
                continue
            if path.read_text(encoding="utf-8") != content:
                errors.append(f"{path.name} устарел — пересобрать scripts/{name}.py без --check")


def check_index_links(root, index_text, errors):
    """Ссылки INDEX.md на Markdown-файлы должны разрешаться."""
    targets = set(MD_LINK.findall(index_text)) | set(MD_BACKTICK.findall(index_text))
    for target in sorted(targets):
        if "://" in target or target.startswith("#"):
            continue
        if not (root / target).exists():
            errors.append(f"INDEX.md ссылается на несуществующий файл: {target}")


def validate(root, config):
    errors, warnings = [], []
    texts = {}
    for name in [config["index"]] + config["corpus"]:
        try:
            texts[name] = local_path(root, name).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append(f"Cannot read {name}: {exc}")
    records = registry(texts.get(config["index"], ""), config, errors)
    check_index_links(root, texts.get(config["index"], ""), errors)
    check_generated_registries(root, config, errors)
    pattern = config["id_pattern"]
    mention_re = re.compile(r"(?<![\w-])(?:" + pattern + r")(?![\w-])")
    definition_re = re.compile(r"^\s*[^<\s].+?\((" + pattern + r")\):\s*\S")
    anchor_re = re.compile(r'^\s*<a id="([A-Za-z0-9_-]+)"></a>\s*$')
    definitions = defaultdict(list)
    mentions = set()
    anchors = {}
    for name in config["corpus"]:
        file_anchors = defaultdict(list)
        current_anchor = None
        for number, line in visible_lines(texts.get(name, "")):
            anchor = anchor_re.fullmatch(line)
            if anchor:
                current_anchor = anchor[1]
                file_anchors[current_anchor].append(number)
                continue
            if not line.strip():
                continue
            mentions.update(match[0] for match in mention_re.finditer(line))
            definition = definition_re.match(line)
            if definition:
                definitions[definition[1]].append((name, current_anchor, number))
            current_anchor = None
        anchors[name] = file_anchors
        for anchor, locations in file_anchors.items():
            if len(locations) != 1:
                errors.append(f"{name}: duplicate anchor #{anchor}")
    for ident in sorted(mentions - records.keys()):
        errors.append(f"Mention without registry row: {ident}")
    for ident in sorted(set(config["required_ids"]) - records.keys()):
        errors.append(f"Required ID missing: {ident}")
    for ident, (canonical, status) in sorted(records.items()):
        found = definitions.get(ident, [])
        if status in {"open", "gap"}:
            if canonical != "—" or found:
                errors.append(f"{ident}: {status} requires Canonical=— and no definition")
            if status == "gap" and ident in mentions:
                errors.append(f"{ident}: gap is mentioned in normative corpus")
            continue
        if len(found) != 1:
            errors.append(f"{ident}: expected exactly one definition, found {len(found)}")
        if canonical.count("#") != 1:
            errors.append(f"{ident}: canonical must be file.md#explicit-anchor")
            continue
        name, anchor = canonical.split("#")
        if name not in config["corpus"]:
            errors.append(f"{ident}: canonical file is outside configured corpus: {name}")
            continue
        if len(anchors.get(name, {}).get(anchor, [])) != 1:
            errors.append(f"{ident}: canonical anchor absent or duplicated: {canonical}")
        if len(found) == 1 and found[0][:2] != (name, anchor):
            errors.append(f"{ident}: canonical does not point to its definition")
    for name in config["corpus"]:
        if not any(location[0] == name for values in definitions.values() for location in values):
            warnings.append(f"No formal definitions in corpus file: {name}")
    return errors, warnings, len(records)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config", default="governance.json")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        config = load_config(root, args.config)
        errors, warnings, count = validate(root, config)
    except (OSError, UnicodeError, ValueError, TypeError, re.error) as exc:
        print(f"CONFIG ERROR: {exc}")
        return 2
    for message in errors:
        print("ERROR: " + message)
    for message in warnings:
        print("WARNING: " + message)
    failed = bool(errors or (warnings and (config["strict"] or args.strict)))
    result = "FAIL" if failed else "PASS"
    print(f"RESULT: {result} | records={count}, errors={len(errors)}, warnings={len(warnings)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
