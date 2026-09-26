"""Сводка по реальному журналу: разбор, количество, повреждения, метаданные.

Вспомогательная проверка после ручного прогона Hermes. Код не входит
в поставку MiniMem и не является частью этапов плана.

Запуск: python -B tools/report_journal.py [memory_root]
"""

from __future__ import annotations

import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from mm import journal, paths  # noqa: E402

DEFAULT_ROOT = Path(r"C:\Projects_loc\LLM_Wiki\memory")


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    files = paths.existing_journal_files(root)
    if not files:
        print(f"дневных файлов журнала нет: {root}")
        return 0
    for path in files:
        records, damaged = journal.read_records(path)
        print(f"{path.relative_to(root)}: записей {len(records)}, повреждённых {len(damaged)}")
        for record in records:
            print(
                f"  offset={record.offset:<6} session={record.session_id:<24} "
                f"trunc={record.metadata.get('truncated', ''):<9} "
                f"body={record.body_status:<8} "
                f"chars={len(record.user_utterance)}/{len(record.assistant_answer)} "
                f"redaction={record.metadata.get('redaction_set', '')} "
                f"platform={record.metadata.get('platform', '')}"
            )
        for item in damaged:
            print(f"  ПОВРЕЖДЕНА offset={item.offset} причина={item.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
