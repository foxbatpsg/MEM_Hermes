"""Приёмка: обязательные номера и полнота отчёта (П-15).

Основание: ТЗ v1.7 §23 (файл `required_tests.txt`, обязательные номера),
§25 критерии 24 и 25. Тест падает, если в отчёте `tests/acceptance_v1.6.md`
нет ни одного обязательного номера (MM-153) или если отчёт не содержит всех
номеров 1…N, включая снятые и пропуски (MM-154).

Номера читаются из `tests/required_tests.txt`, а не хранятся в тесте: иначе
проверка потеряла бы смысл и перестала бы ловить расхождение перечня.

Запуск: python -B -m unittest discover -s tests -p "test_*.py" (из корня)
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REQUIRED_FILE = TESTS_DIR / "required_tests.txt"
REPORT_FILE = TESTS_DIR / "acceptance_v1.6.md"

#: Строки таблицы статусов: `| 12 | пройден | ... |`.
ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|(.*)$", re.MULTILINE)


def required_numbers() -> list[int]:
    return [int(line) for line in REQUIRED_FILE.read_text("utf-8").split("\n") if line.strip()]


def report_numbers() -> dict[int, str]:
    """Номера, присутствующие в отчёте, вместе со статусом."""

    text = REPORT_FILE.read_text("utf-8")
    found: dict[int, str] = {}
    for match in ROW_RE.finditer(text):
        found[int(match.group(1))] = match.group(2).strip()
    return found


class AcceptanceReportTests(unittest.TestCase):
    """Отчёт о прогоне (MM-153, MM-154)."""

    def setUp(self) -> None:
        self.required = required_numbers()
        self.reported = report_numbers()

    def test_153_report_lists_required_numbers(self) -> None:
        """MM-153. В отчёте есть каждый обязательный номер из required_tests.txt."""

        self.assertTrue(self.required, "файл обязательных номеров пуст")
        absent = [number for number in self.required if number not in self.reported]
        self.assertEqual(
            absent,
            [],
            f"в отчёте нет обязательных номеров: {absent} (§23, §25 п. 24)",
        )

    def test_154_report_contains_all_numbers_from_one_to_n(self) -> None:
        """MM-154. Отчёт содержит номера 1…N, включая снятый MM-112."""

        self.assertTrue(self.reported, "в отчёте нет ни одной строки со статусом")
        self.assertEqual(min(self.reported), 1)
        highest = max(self.reported)
        missing = [number for number in range(1, highest + 1) if number not in self.reported]
        self.assertEqual(missing, [], f"в отчёте пропущены номера: {missing}")
        self.assertIn(112, self.reported, "снятый MM-112 обязан присутствовать в отчёте")
        self.assertIn("снят", self.reported[112])

    def test_154_no_required_number_is_left_without_check(self) -> None:
        """MM-154. Ни один обязательный номер не остался без проверки."""

        unchecked = [
            number
            for number in self.required
            if self.reported.get(number, "").startswith("нет проверки")
        ]
        self.assertEqual(unchecked, [], f"номера без проверки: {unchecked} (§25 п. 13)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
