"""Фикстуры самодостаточны: тесты не зависят от документов и карточек проекта."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("c_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cards = load("build_cards_registry")
query = load("query_registry")
validator = load("validate_index")

GOVERNANCE = {
    "index": "INDEX.md", "corpus": ["Docs/spec.md"],
    "id_pattern": "(?:R|Q|G)-[0-9]{3}", "required_ids": [],
    "registry_heading": "## Реестр", "strict": True,
}
CARDS = {
    "files": ["Docs/cards/Stage-1.md"],
    "id_pattern": "T-[0-9]{3}",
    "title_pattern": "^\\*\\*Название:\\*\\*\\s*(.+)$",
    "task_pattern": "E[0-9]{2}-T[0-9]{2}",
    "markers": ["BLOCKED_BY"],
}
INDEX = """# Index

## Реестр

| ID | Title | Canonical | Status |
|---|---|---|---|
| R-001 | Хранение истории | Docs/spec.md#r-001 | active |
"""
CORPUS = """# ТЗ

<a id="r-001"></a>
Хранение истории (R-001): Захват дописывает завершённые ходы в журнал.
"""
CARD_FILE = """# Карточки

## T-001

**Название:** Реализовать журнал.
Нормы: R-001.
Задача: E01-T05.
BLOCKED_BY: R-002

## T-002

**Название:** Реализовать индекс.
"""


class CardsRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Docs" / "cards").mkdir(parents=True)
        self.write("governance.json", json.dumps(GOVERNANCE))
        self.write("cards.json", json.dumps(CARDS))
        self.write("INDEX.md", INDEX)
        self.write("Docs/spec.md", CORPUS)
        self.write("Docs/cards/Stage-1.md", CARD_FILE)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def run_tool(self, module, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.main(["--root", str(self.root)] + argv)
        return code, output.getvalue()

    def build(self):
        return self.run_tool(cards, [])

    def test_build_and_check(self):
        code, output = self.build()
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "CARDS-REGISTRY.md").exists())
        self.assertEqual(self.run_tool(cards, ["--check"])[0], 0)
        self.write("Docs/cards/Stage-1.md", CARD_FILE + "\nХвост.\n")
        self.assertEqual(self.run_tool(cards, ["--check"])[0], 1)

    def test_records(self):
        self.build()
        data = json.loads((self.root / "CARDS-REGISTRY.json").read_text(encoding="utf-8"))
        self.assertEqual([card["id"] for card in data["cards"]], ["T-001", "T-002"])
        first = data["cards"][0]
        self.assertEqual(first["title"], "Реализовать журнал.")
        self.assertEqual(first["line"], 3)
        self.assertEqual(first["norms"], ["R-001", "R-002"])
        self.assertEqual(first["tasks"], ["E01-T05"])
        self.assertEqual(first["markers"], ["BLOCKED_BY"])
        self.assertEqual(data["norm_to_cards"]["R-001"], ["T-001"])
        self.assertEqual(data["task_to_cards"]["E01-T05"], ["T-001"])

    def test_queries(self):
        self.build()
        self.assertEqual(self.run_tool(query, ["card", "T-001"])[0], 0)
        self.assertEqual(self.run_tool(query, ["norm", "R-002"])[0], 0)
        self.assertEqual(self.run_tool(query, ["task", "E01-T05"])[0], 0)
        self.assertEqual(self.run_tool(query, ["find", "индекс"])[0], 0)
        self.assertEqual(self.run_tool(query, ["stats"])[0], 0)
        self.assertEqual(self.run_tool(query, ["card", "T-999"])[0], 1)
        self.assertEqual(self.run_tool(query, ["norm", "G-001"])[0], 1)

    def test_missing_cards_layer(self):
        (self.root / "cards.json").unlink()
        self.assertEqual(self.build()[0], 1)
        self.assertEqual(self.run_tool(query, ["stats"])[0], 1)

    def test_bad_cards_config(self):
        for value in [{}, {"files": ["Docs/spec.txt"]}, {"files": ["Docs/spec.md"], "id_pattern": "("}]:
            with self.subTest(value=value):
                self.write("cards.json", json.dumps(value))
                self.assertEqual(self.build()[0], 2)

    def run_validator(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validator.main(["--root", str(self.root)])
        return code, output.getvalue()

    def test_validator_requires_fresh_registry(self):
        self.build()
        self.assertEqual(self.run_validator()[0], 0)
        self.write("Docs/cards/Stage-1.md", CARD_FILE + "\nХвост.\n")
        code, output = self.run_validator()
        self.assertEqual(code, 1, output)
        self.assertIn("CARDS-REGISTRY", output)
        self.build()
        self.assertEqual(self.run_validator()[0], 0)


if __name__ == "__main__":
    unittest.main()
