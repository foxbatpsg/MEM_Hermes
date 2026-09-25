"""Self-contained fixtures: tests do not depend on the demo or future project content."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("validate_index.py")
SPEC = importlib.util.spec_from_file_location("validator", SCRIPT)
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)

CONFIG = {
    "index": "INDEX.md", "corpus": ["docs/rules.md"],
    "id_pattern": "(?:A|Q|G)-[0-9]{2}", "required_ids": ["A-01", "G-01"],
    "registry_heading": "## Реестр", "strict": True,
}
INDEX = """# Index
## Реестр
| ID | Title | Canonical | Status |
|---|---|---|---|
| A-01 | Rule | docs/rules.md#a-01 | active |
| G-01 | Gate | docs/rules.md#g-01 | active |
| Q-01 | Question | — | open |
"""
CORPUS = '''<a id="a-01"></a>
Rule (A-01): Required.
<a id="g-01"></a>
Gate (G-01): Test required.
Question Q-01 remains open.
'''


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "docs").mkdir()
        self.write("INDEX.md", INDEX)
        self.write("docs/rules.md", CORPUS)
        self.config = dict(CONFIG)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def run_validation(self):
        self.write("governance.json", json.dumps(self.config))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = validator.main(["--root", str(self.root)])
        return code, output.getvalue()

    def test_valid(self):
        self.assertEqual(self.run_validation()[0], 0)

    def test_invalid_corpus_variants(self):
        variants = [
            CORPUS + "Unknown A-99\n",
            CORPUS + 'Rule again (A-01): duplicate.\n',
            CORPUS.replace('Rule (A-01): Required.', 'Rule A-01 is required.'),
            CORPUS.replace('id="a-01"', 'id="wrong"'),
            CORPUS + '<a id="a-01"></a>\n',
            CORPUS + 'Question (Q-01): unapproved definition.\n',
            CORPUS.replace('Rule (A-01): Required.', 'Rule (A-01): '),
        ]
        for text in variants:
            with self.subTest(text=text):
                self.write("docs/rules.md", text)
                self.assertEqual(self.run_validation()[0], 1)

    def test_invalid_registry_variants(self):
        variants = [
            INDEX.replace('| A-01 | Rule | docs/rules.md#a-01 | active |\n', '') + '\nMention A-01\n',
            INDEX + '| A-01 | Duplicate | docs/rules.md#a-01 | active |\n',
            INDEX.replace('docs/rules.md#a-01', 'docs/rules.md#g-01'),
            INDEX.replace('docs/rules.md#a-01', 'missing.md#a-01'),
            INDEX.replace('| active |', '| unknown |', 1),
            INDEX.replace('## Реестр', '## Other'),
            INDEX + '| A-99 | Phantom | docs/rules.md#a-99 | active |\n',
            INDEX.replace('| Title |', '| Name |'),
        ]
        for text in variants:
            with self.subTest(text=text):
                self.write("INDEX.md", text)
                self.assertEqual(self.run_validation()[0], 1)

    def test_missing_files(self):
        for name in ["INDEX.md", "docs/rules.md"]:
            path = self.root / name
            original = path.read_text(encoding="utf-8")
            path.unlink()
            self.assertEqual(self.run_validation()[0], 1)
            self.write(name, original)

    def test_gap_and_retired(self):
        self.write("INDEX.md", INDEX.replace('| active |', '| retired |', 1) +
                   '| Q-02 | Intentional gap | — | gap |\n')
        self.assertEqual(self.run_validation()[0], 0)
        self.write("docs/rules.md", CORPUS + 'Q-02 is used.\n')
        self.assertEqual(self.run_validation()[0], 1)

    def test_custom_ids(self):
        self.config['id_pattern'] = '(?:REQ|DEC|GATE)-[0-9]{3}'
        self.config['required_ids'] = ['REQ-001', 'GATE-001']
        for name, text in [('INDEX.md', INDEX), ('docs/rules.md', CORPUS)]:
            self.write(name, text.replace('A-01', 'REQ-001').replace('G-01', 'GATE-001').replace('Q-01', 'DEC-001'))
        self.assertEqual(self.run_validation()[0], 0)

    def test_examples_ignored(self):
        self.write('docs/rules.md', CORPUS + '\n```text\nFake (A-99): example\n```\n<!-- A-98 -->\n')
        self.assertEqual(self.run_validation()[0], 0)

    def test_strict_warnings(self):
        self.write('docs/extra.md', '# No formal definitions\n')
        self.config['corpus'] = ['docs/rules.md', 'docs/extra.md']
        self.assertEqual(self.run_validation()[0], 1)
        self.config['strict'] = False
        self.assertEqual(self.run_validation()[0], 0)

    def test_empty_start_and_growth(self):
        self.config['corpus'] = []
        self.config['required_ids'] = []
        empty_index = '\n'.join(INDEX.splitlines()[:4]) + '\n'
        self.write('INDEX.md', empty_index)
        self.assertEqual(self.run_validation()[0], 0)
        self.write('docs/wishes.md', 'Any free-form wishes, even R-999, are not normative.\n')
        self.assertEqual(self.run_validation()[0], 0)
        self.write('INDEX.md', INDEX)
        self.assertEqual(self.run_validation()[0], 1)
        self.config['corpus'] = ['docs/rules.md']
        self.assertEqual(self.run_validation()[0], 0)
        self.config['corpus'] = []
        self.write('INDEX.md', empty_index)
        self.config['required_ids'] = ['G-01']
        self.assertEqual(self.run_validation()[0], 1)

    def test_bad_config(self):
        for key, value in [('corpus', None), ('corpus', ['../outside.md']),
                           ('id_pattern', '('), ('strict', 'yes'),
                           ('required_ids', ['BAD']), ('index', 'docs/rules.md')]:
            with self.subTest(key=key, value=value):
                self.config = dict(CONFIG)
                self.config[key] = value
                self.assertEqual(self.run_validation()[0], 2)

    def test_cli_pass_and_fail(self):
        self.write('governance.json', json.dumps(self.config))
        command = [sys.executable, '-B', str(SCRIPT), '--root', str(self.root), '--strict']
        passed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn('RESULT: PASS', passed.stdout)
        self.write('docs/rules.md', CORPUS + 'Unregistered A-99\n')
        failed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
        self.assertIn('RESULT: FAIL', failed.stdout)


    def test_index_markdown_links(self):
        self.write("INDEX.md", INDEX + "\nСсылка на `docs/missing.md` и [правила](docs/rules.md).\n")
        self.assertEqual(self.run_validation()[0], 1)
        self.write("INDEX.md", INDEX + "\nСсылка на `docs/rules.md` и [правила](docs/rules.md).\n")
        self.assertEqual(self.run_validation()[0], 0)


if __name__ == '__main__':
    unittest.main()
