"""Фикстуры самодостаточны: тесты не зависят от документов проекта."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


refs = load("build_refs_registry")
query = load("query_refs")
validator = load("validate_index")

CONFIG = {
    "index": "INDEX.md", "corpus": ["Docs/spec.md"],
    "id_pattern": "(?:R|Q|G)-[0-9]{3}", "required_ids": ["R-001"],
    "registry_heading": "## Реестр", "strict": True,
}
INDEX = """# Index

## Реестр

| ID | Title | Canonical | Status |
|---|---|---|---|
| R-001 | Хранение истории | Docs/spec.md#r-001 | active |
"""
CORPUS = """# ТЗ

## 1. Назначение

<a id="r-001"></a>
Хранение истории (R-001): Захват дописывает завершённые ходы в журнал.

Ссылка на R-001 из текста раздела.

```text
Fake (R-999): пример в блоке кода.
```
"""


class RefsRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "Docs").mkdir()
        self.write("governance.json", json.dumps(CONFIG))
        self.write("INDEX.md", INDEX)
        self.write("Docs/spec.md", CORPUS)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def run_tool(self, module, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.main(["--root", str(self.root)] + argv)
        return code, output.getvalue()

    def build(self):
        return self.run_tool(refs, [])

    def test_build_and_check(self):
        code, output = self.build()
        self.assertEqual(code, 0, output)
        self.assertTrue((self.root / "REFS-REGISTRY.md").exists())
        self.assertTrue((self.root / "REFS-REGISTRY.json").exists())
        self.assertIn("SOURCE-HASH", output)
        self.assertEqual(self.run_tool(refs, ["--check"])[0], 0)
        self.write("Docs/spec.md", CORPUS + "\nХвост без норм.\n")
        self.assertEqual(self.run_tool(refs, ["--check"])[0], 1)

    def test_registry_content(self):
        self.build()
        data = json.loads((self.root / "REFS-REGISTRY.json").read_text(encoding="utf-8"))
        self.assertEqual(data["norms"]["R-001"]["definitions"][0]["line"], 6)
        self.assertEqual(data["counts"]["норм"], 1)
        self.assertEqual(data["counts"]["упоминаний"], 1)
        self.assertNotIn("R-999", data["norms"])
        self.assertEqual(data["sections"][0]["number"], "1")

    def test_queries(self):
        self.build()
        self.assertEqual(self.run_tool(query, ["norm", "R-001"])[0], 0)
        code, output = self.run_tool(query, ["section", "1"])
        self.assertEqual(code, 0, output)
        self.assertIn("§1", output)
        self.assertEqual(self.run_tool(query, ["where", "журнал"])[0], 0)
        self.assertEqual(self.run_tool(query, ["stats"])[0], 0)
        self.assertEqual(self.run_tool(query, ["norm", "R-999"])[0], 1)
        self.assertEqual(self.run_tool(query, ["section", "77"])[0], 1)

    def test_missing_registry(self):
        self.assertEqual(self.run_tool(query, ["stats"])[0], 1)

    def run_validator(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validator.main(["--root", str(self.root)])
        return code, output.getvalue()

    def test_validator_requires_fresh_registry(self):
        self.build()
        self.assertEqual(self.run_validator()[0], 0)
        self.write("Docs/spec.md", CORPUS + "\nХвост без норм.\n")
        code, output = self.run_validator()
        self.assertEqual(code, 1, output)
        self.assertIn("устарел", output)
        self.build()
        self.assertEqual(self.run_validator()[0], 0)


if __name__ == "__main__":
    unittest.main()
