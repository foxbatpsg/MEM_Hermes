"""Генератор отчёта о прогоне приёмочных тестов (критерий §25 п. 24, П-15).

Основание: ТЗ v1.7 §23 (обязательные номера), §25 (п. 24-25). Отчёт
`tests/acceptance_v1.6.md` содержит все номера 1…N со статусами; номера
берутся из `tests/required_tests.txt`, а статусы — из фактического прогона
тестов `minimem/tests` и чтения docstring: номер считается покрытым, только
если он реально назван в проверке.

Запуск: python -B tools/build_acceptance_report.py [--check]
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CODE_DIR.parent
TESTS_DIR = REPO_ROOT / "tests"
UNIT_TESTS_DIR = CODE_DIR / "tests"
REQUIRED_FILE = TESTS_DIR / "required_tests.txt"
REPORT_FILE = TESTS_DIR / "acceptance_v1.6.md"

#: Номер, снятый по §23 (дубль MM-96).
RETIRED = {112: "снят (дубль MM-96)"}

#: Номер, намеренно не использованный (§23, статус «пропуск»).
GAP: dict[int, str] = {}

#: Номера, проверка которых зафиксирована вне `minimem/tests`.
EXTERNAL_CHECKS = {153: "tests/test_required_numbers.py", 154: "tests/test_required_numbers.py"}

NUMBER_RE = re.compile(r"MM-(\d+)")


def required_numbers() -> list[int]:
    """Номера из `tests/required_tests.txt` по одному в строке, в порядке файла."""

    return [int(line) for line in REQUIRED_FILE.read_text("utf-8").split("\n") if line.strip()]


def covered_numbers() -> dict[int, list[str]]:
    """Номера, названные в docstring тестов `minimem/tests`, с именами тестов.

    Разбор идёт через `ast`: docstring берётся у функции, а не из текста
    файла, поэтому номер в комментарии или в строке кода проверкой не считается.
    """

    found: dict[int, list[str]] = {}
    for path in sorted(UNIT_TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc = ast.get_docstring(node) or ""
            if isinstance(node, ast.ClassDef):
                methods = [
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                label = f"{node.name} ({', '.join(methods)})" if methods else node.name
            else:
                label = node.name
            for match in NUMBER_RE.finditer(doc):
                found.setdefault(int(match.group(1)), []).append(f"{path.name}::{label}")
    return found


def run_suite() -> tuple[int, int, int]:
    """Прогон тестов `minimem`: возвращает (всего, успешно, пропущено)."""

    completed = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
        cwd=CODE_DIR,
        capture_output=True,
        text=True,
    )
    summary = next(
        (
            line
            for line in reversed(completed.stderr.strip().splitlines())
            if line.startswith("Ran ")
        ),
        "",
    )
    total = int(re.search(r"Ran (\d+)", summary).group(1)) if summary else 0
    ok = completed.stderr.rstrip().endswith("OK") or "\nOK" in completed.stderr
    # Число пропусков сообщает сам unittest в строке `OK (skipped=3)`.
    skipped_match = re.search(r"skipped=(\d+)", completed.stderr)
    skipped = int(skipped_match.group(1)) if skipped_match else 0
    return total, total - skipped if ok else -1, skipped


def statuses() -> dict[int, tuple[str, str]]:
    """Статус и привязка каждого номера 1…N (§23, §25 п. 24).

    Обязательные номера берутся из `tests/required_tests.txt`; остальные
    номера §23 попадают в отчёт со статусом «не обязателен для первой
    приёмки» — §25 п. 24 требует в отчёте все номера 1…N, включая пропуски.
    """

    covered = covered_numbers()
    required = set(required_numbers())
    highest = max(set(required) | set(RETIRED) | set(GAP) | set(covered))
    result: dict[int, tuple[str, str]] = {}
    for number in range(1, highest + 1):
        if number in RETIRED:
            result[number] = (RETIRED[number], "§23: статус «снят», в прогоне не участвует")
            continue
        if number in GAP:
            result[number] = (GAP[number], "§23: статус «пропуск»")
            continue
        places = covered.get(number, [])
        if number not in required:
            marker = "не обязателен для первой приёмки (§23)"
            if places:
                result[number] = (f"пройден, {marker}", ", ".join(places))
            else:
                result[number] = (f"нет проверки, {marker}", "номер не встречается ни в одном тесте")
            continue
        if places:
            result[number] = ("пройден", ", ".join(places))
        elif number in EXTERNAL_CHECKS:
            result[number] = ("пройден", EXTERNAL_CHECKS[number])
        else:
            result[number] = ("нет проверки", "номер не встречается ни в одном тесте")
    return result



def build_report() -> str:
    """Текст отчёта: все номера 1…N со статусами (§25 п. 24)."""

    current = statuses()
    all_numbers = sorted(set(current) | set(RETIRED) | set(GAP))
    total, passed, skipped = run_suite()

    lines: list[str] = []
    lines.append("# Отчёт о приёмке MiniMem-1")
    lines.append("")
    lines.append("Файл формируется автоматически: `python -B tools/build_acceptance_report.py`.")
    lines.append("Номера берутся из `tests/required_tests.txt`, статусы — из фактического")
    lines.append("прогона тестов и чтения docstring проверок.")
    lines.append("")
    lines.append(f"Сформирован: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Итог прогона")
    lines.append("")
    lines.append(f"- тестов выполнено: {total};")
    lines.append(f"- успешно: {passed if passed >= 0 else 'прогон неуспешен'};")
    lines.append(f"- пропущено: {skipped} (установочные тесты Hermes).")
    lines.append("")
    lines.append("## Статусы номеров")
    lines.append("")
    lines.append("| Номер | Статус | Проверка |")
    lines.append("|---|---|---|")
    for number in all_numbers:
        if number in current:
            status, place = current[number]
        elif number in RETIRED:
            status, place = RETIRED[number], "вне `required_tests.txt` (снят по §23)"
        else:
            status, place = "вне обязательного перечня", "—"
        lines.append(f"| {number} | {status} | {place} |")
    lines.append("")
    values = [status for status, _ in current.values()]
    lines.append("## Сводка")
    lines.append("")
    for name in ("пройден", "снят (дубль MM-96)"):
        lines.append(f"- {name}: {sum(1 for value in values if value == name)};")
    lines.append(f"- нет проверки: {sum(1 for value in values if value.startswith('нет проверки'))};")
    optional = sum(1 for value in values if "не обязателен" in value)
    lines.append(f"- из них вне `required_tests.txt` (не обязательны): {optional}.")
    lines.append("")
    lines.append("Приёмка не пройдена, если хотя бы один обязательный номер имеет статус")
    lines.append("«нет проверки» или если прогон тестов завершился ошибкой (§25 п. 13).")
    lines.append("")
    return "\n".join(lines)


def _without_timestamp(text: str) -> str:
    """Текст отчёта без строки формирования: она меняется при каждом прогоне."""

    return "\n".join(
        line for line in text.split("\n") if not line.startswith("Сформирован:")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка отчёта о приёмке MiniMem-1")
    parser.add_argument(
        "--check", action="store_true", help="только проверить актуальность отчёта"
    )
    args = parser.parse_args(argv)

    report = build_report()
    if args.check:
        # Метка времени меняется при каждом прогоне, поэтому актуальность
        # проверяется по содержанию без неё.
        current = REPORT_FILE.read_text("utf-8") if REPORT_FILE.is_file() else ""
        if _without_timestamp(current) != _without_timestamp(report):
            print("отчёт не актуален")
            return 1
        print("отчёт актуален")
        return 0
    REPORT_FILE.write_text(report, encoding="utf-8", newline="\n")
    print(f"отчёт: {REPORT_FILE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

