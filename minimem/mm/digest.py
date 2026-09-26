"""Дайджест: детерминированная выжимка последних ходов сессии, без модели.

Основание: ТЗ v1.7 §3.4, §13.1 (санитизация), §14 (формат и правила),
§11.1 и §9.1 (`sessions.digest_file`), §15 (общее правило снятых записей),
§19.5 и §22 (логирование), §21.5 (`rebuild-digest`, `verify`).

Дайджест читает производный слой SQLite, а не журнал: список сессий и
состав ходов берутся из `memory_meta` (MM-58), текст — из `memory_fts`.
Источником истины остаётся журнал: файл дайджеста всегда можно пересобрать
командой `minimem rebuild-digest`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from . import canonical, insert, paths
from .log import Logger
from .store import Store

#: Заголовок и подпись способа построения (§14).
TITLE = "# Дайджест сессии"
METHOD_LINE = "- Способ: детерминированная выжимка, без модели"

#: Пояснение перед ходами: выжимка не является пересказом (§14).
PREAMBLE = (
    "> Ниже — последние ходы сессии в сокращении. Это выжимка, а не пересказ\n"
    "> и не итог: выводы в ней не подведены. Полная история — в журнале."
)

#: Значения поля «Завершение» (§14, §26.2, П-17).
COMPLETION_FULL = "полное"
COMPLETION_INTERRUPTED = "прервано"
COMPLETION_FAILED = "сбой"
COMPLETION_VALUES = (COMPLETION_FULL, COMPLETION_INTERRUPTED, COMPLETION_FAILED)

#: Значения `sessions.digest_status` (§9.1).
STATUS_NONE = "none"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: Поля заголовка, без которых файл считается повреждённым (§14, §11.1).
REQUIRED_FIELDS = ("Сессия", "Последний ход", "Создан")

#: Пометка обрезки внутри хода (§14).
TRUNCATION_MARK = " [обрезано]"

#: Длина безопасной части имени файла (П-12, п.3).
SAFE_NAME_MAX = 64

#: Длина суффикса-хеша (П-12, п.4).
HASH_SUFFIX_LENGTH = 8

#: Символы, разрешённые в безопасной части имени (П-12, п.1).
_SAFE_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")



def safe_component(session_id: str) -> str:
    """Безопасная часть имени файла дайджеста (П-12, п.1-3).

    Символы вне `[A-Za-z0-9._-]` заменяются на `_`, регистр приводится к
    нижнему, результат обрезается до 64 символов. Хвост из точек и пробелов
    убирается, иначе Windows отверг бы имя.
    """

    cleaned = "".join(ch if ch.lower() in _SAFE_ALLOWED else "_" for ch in session_id)
    # Хвост из точек и пробелов убирается (Windows их не принимает), но
    # подчёркивания — законная часть безопасного имени, их не трогаем.
    cleaned = cleaned.lower().rstrip(". ")[:SAFE_NAME_MAX]
    return cleaned or "session"


def session_hash(session_id: str) -> str:
    """Первые 8 hex-символов sha256 исходного session_id (П-12, п.4)."""

    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:HASH_SUFFIX_LENGTH]


def digest_filename(session_id: str) -> str:
    """Имя файла дайджеста: `<safe>--<hash8>.md` (П-12, п.5)."""

    return f"{safe_component(session_id)}--{session_hash(session_id)}.md"


def digest_path(memory_root: Path, project: str, session_id: str) -> Path:
    """Путь файла дайджеста: `memory_root/<project>/memory/digest/<file>` (§14)."""

    return paths.digest_dir(memory_root, project) / digest_filename(session_id)


def to_utc(stamp: str) -> str:
    """ISO-8601 с offset -> нормализованный UTC; нечитаемое значение -> ""."""

    if not stamp:
        return ""
    try:
        return canonical.timestamp_utc(datetime.fromisoformat(stamp))
    except ValueError:
        return ""


def now_iso() -> str:
    """Текущее время в ISO-8601 с offset (поле «Создан», §14)."""

    return paths.now().isoformat(timespec="seconds")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "да"}
    return False


def completion_from_event(extra: Mapping[str, Any] | None) -> str:
    """Поле «Завершение» по событию on_session_end (§26.2, П-17).

    Отсутствие `completed` / `failed` / `interrupted` не мешает созданию
    дайджеста: такое состояние считается полным (MM-159).
    """

    if not isinstance(extra, Mapping):
        return COMPLETION_FULL
    if _truthy(extra.get("interrupted")):
        return COMPLETION_INTERRUPTED
    if _truthy(extra.get("failed")):
        return COMPLETION_FAILED
    return COMPLETION_FULL


@dataclass
class DigestHeader:
    """Разобранный заголовок файла дайджеста (§14, §11.1)."""

    fields: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    def get(self, name: str, default: str = "") -> str:
        return self.fields.get(name, default)

    @property
    def session_id(self) -> str:
        return self.get("Сессия")

    @property
    def last_turn(self) -> str:
        return self.get("Последний ход")

    @property
    def created(self) -> str:
        return self.get("Создан")

    @property
    def completion(self) -> str:
        return self.get("Завершение", COMPLETION_FULL)

    @property
    def last_turn_utc(self) -> str:
        """«Последний ход», нормализованный в UTC; пусто, если не разобран."""

        return to_utc(self.last_turn)

    @property
    def created_utc(self) -> str:
        return to_utc(self.created)

    @property
    def valid(self) -> bool:
        """Файл пригоден для выбора последнего дайджеста (§11.1)."""

        return all(self.fields.get(name) for name in REQUIRED_FIELDS)


def read_header(path: Path) -> DigestHeader | None:
    """Читает заголовок файла дайджеста; `None`, если файл повреждён (§3.4)."""

    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.split("\n")
    if not lines or lines[0].strip() != TITLE:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if not line.startswith("- "):
            if fields:
                break
            continue
        key, _, value = line[2:].partition(":")
        fields[key.strip()] = value.strip()
    header = DigestHeader(fields=fields, path=Path(path))
    return header if header.valid else None


def existing_digest_files(memory_root: Path, project: str) -> list[Path]:
    """Файлы дайджестов проекта, по возрастанию имени."""

    directory = paths.digest_dir(memory_root, project)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.md"), key=lambda item: item.name)


def _turn_time(timestamp: str) -> str:
    """Часть HH:MM заголовка хода из времени записи (§14)."""

    return timestamp[11:16] if len(timestamp) >= 16 else ""


def _clean_text(text: str) -> tuple[str, bool]:
    """Санитизация текста хода и признак того, что она что-то изменила (§13.1)."""

    sanitized = insert.sanitize_for_insert(text or "")
    return sanitized, sanitized != (text or "")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """Обрезает текст по лимиту вместе с пометкой (§14).

    Пометка обрезки входит в лимит, иначе текст мог бы выйти за него на
    длину самой пометки.
    """

    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    if limit <= len(TRUNCATION_MARK):
        return TRUNCATION_MARK.strip()[:limit], True
    return text[: limit - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK, True


def _render_turn(
    turn: str,
    timestamp: str,
    user_text: str,
    assistant_text: str,
    budget: int,
) -> str | None:
    """Блок одного хода в пределах бюджета; `None`, если ход не помещается.

    Заголовок хода обязателен, тексты делят остаток поровну. Если на ход
    остаётся меньше заголовка с метками «**Вы:**» и «**Агент:**», ход
    отбрасывается: блок без текста в выжимке не читается.
    """

    heading = f"### Ход {turn}"
    moment = _turn_time(timestamp)
    if moment:
        heading = f"{heading} · {moment}"
    fixed = len(heading) + len("**Вы:** ") + len("**Агент:** ") + 3
    if budget < fixed:
        return None
    available = budget - fixed
    user_part, _ = _truncate(user_text, available // 2)
    assistant_part, _ = _truncate(assistant_text, available - len(user_part))
    return "\n".join([heading, "", f"**Вы:** {user_part}", f"**Агент:** {assistant_part}"])


@dataclass
class _SessionView:
    """Готовая к записи выжимка одной сессии."""

    session_id: str
    project: str
    turns_total: int
    last_turn: str
    last_day: str
    blocks: list[str]
    sanitized_turns: int


def _render_header(view: _SessionView, completion: str, created: str) -> list[str]:
    """Строки заголовка файла дайджеста в нормативном порядке (§14)."""

    return [
        TITLE,
        f"- Сессия: {view.session_id}",
        f"- Дата: {view.last_day}",
        f"- Проект: {view.project}",
        f"- Ходов в сессии: {view.turns_total}",
        f"- Последний ход: {view.last_turn}",
        f"- Создан: {created}",
        f"- Завершение: {completion}",
        METHOD_LINE,
    ]


def _collect_turns(
    store: Store,
    config: Mapping[str, Any],
    project: str,
    session_id: str,
    completion: str,
    created: str,
) -> _SessionView:
    """Собирает текст дайджеста из индекса: последние `digest_turns` ходов.

    Записи, снятые с поиска, исключаются по общему правилу (§14, §15).
    Повреждённые записи в `memory_meta` не попадают: индексатор их не
    сохраняет (§4.5.3). Ходы берутся с конца — последние важнее, и при
    нехватке места отбрасываются более ранние.
    """

    suppressed = store.suppressed_ids()
    limit = int(config["digest_turns"])
    max_chars = int(config["max_digest_chars"])

    rows = [
        row
        for row in store.meta_by_session(project, session_id)
        if row["event_id"] not in suppressed
    ]

    last_turn = str(rows[-1]["timestamp"] or "") if rows else ""
    view = _SessionView(
        session_id=session_id,
        project=project,
        turns_total=len(rows),
        last_turn=last_turn,
        last_day=last_turn[:10] if len(last_turn) >= 10 else "",
        blocks=[],
        sanitized_turns=0,
    )

    header_len = len("\n".join(_render_header(view, completion, created)))
    # 4 разделителя вокруг заголовка и пояснения, 1 завершающий перевод
    # строки и по 1 между блоками ходов — все они входят в лимит.
    budget = max_chars - header_len - len(PREAMBLE) - 5
    if budget <= 0 or limit <= 0:
        return view

    used = 0
    for row in reversed(rows[-limit:]):
        body = store.body_get(row["event_id"]) or {}
        user_text, user_changed = _clean_text(str(body.get("user_utterance") or ""))
        answer_text, answer_changed = _clean_text(str(body.get("assistant_answer") or ""))
        if user_changed or answer_changed:
            view.sanitized_turns += 1
        separator = 1 if view.blocks else 0
        block = _render_turn(
            str(row["turn"] or canonical.DERIVED_TURN),
            str(row["timestamp"] or ""),
            user_text,
            answer_text,
            budget - used - separator,
        )
        if block is None:
            continue
        view.blocks.append(block)
        used += len(block) + separator

    view.blocks.reverse()
    return view


@dataclass
class DigestResult:
    """Итог построения одного дайджеста."""

    status: str = "ok"
    session_id: str = ""
    project: str = ""
    file: str = ""
    path: Path | None = None
    chars: int = 0
    turns_total: int = 0
    turns_included: int = 0
    completion: str = COMPLETION_FULL
    created: str = ""
    last_turn: str = ""
    error: str = ""

    @property
    def created_ok(self) -> bool:
        """Файл дайджеста записан (пустая сессия файла не создаёт)."""

        return self.status == "ok" and self.path is not None


def _write_text(path: Path, text: str) -> bool:
    """Атомарная запись текстового файла: временный файл, затем замена."""

    if not paths.ensure_directory(path.parent):
        return False
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
    return True


def _mark_status(
    store: Store,
    session_id: str,
    project: str,
    digest_file: str | None,
    status: str,
    logger: Logger,
) -> None:
    """Пишет `digest_file` и `digest_status` в `sessions` (§9.1, П-12)."""

    try:
        with store.connection:
            store.session_upsert(
                session_id,
                project,
                digest_file=digest_file,
                digest_status=status,
                last_seen_at=now_iso(),
            )
    except Exception as exc:  # noqa: BLE001 - служебный слой не роняет Hermes
        logger.error(
            "digest",
            "sessions_update_failed",
            type(exc).__name__,
            session_id=session_id,
            project=project,
        )


def _check_collision(store: Store, session_id: str, key: str) -> bool:
    """Отображён ли файл дайджеста на другую сессию (§14, П-12).

    При совпадении имён файл не перезаписывается: событие
    `digest_name_collision` пишет вызывающий код.
    """

    return any(row["session_id"] != session_id for row in store.session_by_digest_file(key))


def build_digest(
    store: Store,
    config: Mapping[str, Any],
    memory_root: Path,
    logger: Logger,
    session_id: str,
    project: str,
    completion: str = COMPLETION_FULL,
    created: str = "",
    hook_event: str = "on_session_end",
) -> DigestResult:
    """Создаёт или перезаписывает файл дайджеста сессии (§14).

    Порядок: выбор ходов из индекса -> санитизация -> сборка в пределах
    `max_digest_chars` -> проверка коллизии имён -> атомарная запись ->
    отметка в `sessions`. Ошибка наружу не поднимается: результат отражается
    в `DigestResult` и в логе, Hermes продолжает работу (§19.2).
    """

    if completion not in COMPLETION_VALUES:
        completion = COMPLETION_FULL

    result = DigestResult(session_id=session_id, project=project, completion=completion)
    if not session_id.strip():
        result.status = "failed"
        result.error = "empty_session_id"
        logger.error(
            "digest", "digest_failed", "empty_session_id", session_id=session_id, project=project
        )
        return result

    if not created:
        created = now_iso()
    view = _collect_turns(store, config, project, session_id, completion, created)

    if view.turns_total == 0:
        # Пустая сессия: файл не создаётся, событие пишется в лог (§14).
        result.status = "empty"
        result.error = "no_records"
        result.created = created
        logger.log(
            "digest",
            "digest_empty_session",
            status="skipped",
            session_id=session_id,
            project=project,
            hook_event=hook_event,
        )
        _mark_status(store, session_id, project, None, STATUS_NONE, logger)
        return result

    path = digest_path(memory_root, project, session_id)
    key = paths.relative_to_root(path, memory_root)
    result.file = key
    result.path = path
    result.created = created
    result.last_turn = view.last_turn

    if _check_collision(store, session_id, key):
        result.status = "failed"
        result.error = "digest_name_collision"
        logger.log(
            "digest",
            "digest_name_collision",
            status="error",
            session_id=session_id,
            project=project,
            error_code="digest_name_collision",
            error_detail_code=key,
        )
        _mark_status(store, session_id, project, key, STATUS_FAILED, logger)
        return result

    header = _render_header(view, completion, created)
    text = "\n".join([*header, "", PREAMBLE, "", *view.blocks]).rstrip("\n") + "\n"
    if view.sanitized_turns:
        logger.log(
            "digest",
            "insert_sanitized",
            session_id=session_id,
            project=project,
            records_scanned=view.sanitized_turns,
        )

    if not _write_text(path, text):
        result.status = "failed"
        result.error = "write_failed"
        logger.error(
            "digest", "digest_failed", "write_failed", session_id=session_id, project=project
        )
        _mark_status(store, session_id, project, key, STATUS_FAILED, logger)
        return result

    result.chars = len(text)
    result.turns_total = view.turns_total
    result.turns_included = len(view.blocks)
    _mark_status(store, session_id, project, key, STATUS_DONE, logger)
    logger.log(
        "digest",
        "digest_created",
        session_id=session_id,
        project=project,
        hook_event=hook_event,
        digest_chars=result.chars,
        records_scanned=view.turns_total,
    )
    return result


@dataclass
class DigestReport:
    """Итог проверки дайджестов для `verify` (§21.5)."""

    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    damaged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    files: int = 0
    max_chars: int = 0


def verify_digests(
    store: Store,
    memory_root: Path,
    logger: Logger,
    config: Mapping[str, Any] | None = None,
    project: str | None = None,
) -> DigestReport:
    """Проверяет файлы дайджестов: повреждения, размер, коллизии (§21.5).

    Повреждённый дайджест — проблема отчёта, а не причина прервать проверку:
    Hermes продолжает работу (§3.4, §14).
    """

    report = DigestReport()
    limit = int(config["max_digest_chars"]) if config else 0
    # Коллизия имён (П-12) и рассинхронизация отображения видны в
    # `sessions`: один digest_file у нескольких session_id и наоборот.
    by_name: dict[str, set[str]] = {}
    by_session: dict[str, set[str]] = {}

    # Файлы каталога перебираются один раз на проект: иначе сессии одного
    # проекта дали бы повторные записи об одном и том же файле.
    for project_name in sorted({row["project"] for row in store.meta_sessions(project)}):
        for path in existing_digest_files(memory_root, project_name):
            key = paths.relative_to_root(path, memory_root)
            header = read_header(path)
            if header is None:
                report.damaged.append(f"{key}: заголовок не читается")
                continue
            report.files += 1
            # Лимит §6 задан в символах, а не в байтах: сравнивается длина
            # текста, иначе кириллица даёт ложное превышение.
            try:
                chars = len(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                report.damaged.append(f"{key}: {type(exc).__name__}")
                continue
            report.max_chars = max(report.max_chars, chars)
            if limit and chars > limit:
                report.problems.append(
                    f"{key}: {chars} символов больше max_digest_chars {limit}"
                )
            owner = f"{project_name}/{header.session_id}"
            by_session.setdefault(owner, set()).add(key)

    for row in store.sessions_all(project):
        digest_file = row.get("digest_file")
        if not digest_file:
            continue
        by_name.setdefault(digest_file, set()).add(f"{row['project']}/{row['session_id']}")
        if not (memory_root / digest_file).is_file():
            report.missing.append(f"{row['project']}/{row['session_id']}: {digest_file}")

    for key, owners in sorted(by_name.items()):
        if len(owners) > 1:
            report.collisions.append(f"{key}: один файл у сессий {', '.join(sorted(owners))}")
    for owner, keys in sorted(by_session.items()):
        if len(keys) > 1:
            report.problems.append(
                f"сессия {owner}: файлы дайджестов {', '.join(sorted(keys))}"
            )

    for name, items in (
        ("повреждённые дайджесты", report.damaged),
        ("совпадения имён дайджестов", report.collisions),
        ("дайджесты, отсутствующие при digest_status=done", report.missing),
    ):
        if items:
            report.problems.append(f"{name}: {len(items)}")
        else:
            report.notes.append(f"{name}: нет")
    report.notes.append(
        f"дайджестов: {report.files}, максимальный размер: {report.max_chars} символов"
    )

    logger.log(
        "digest",
        "verify_digest",
        status="error" if report.problems else "ok",
        error_code="verify_problems" if report.problems else None,
    )
    return report


def last_session(store: Store, project: str | None = None) -> dict[str, str] | None:
    """Сессия с максимальным «Последним ход» — по правилу §11.1."""

    best: dict[str, str] | None = None
    best_key: tuple[str, str] | None = None
    for row in store.meta_sessions(project):
        rows = store.meta_by_session(row["project"], row["session_id"])
        if not rows:
            continue
        key = (to_utc(str(rows[-1]["timestamp"] or "")), row["session_id"])
        if best_key is None or key > best_key:
            best_key = key
            best = row
    return best


def rebuild_digests(
    store: Store,
    config: Mapping[str, Any],
    memory_root: Path,
    logger: Logger,
    session_id: str | None = None,
    all_sessions: bool = False,
    project: str | None = None,
    completion: str = COMPLETION_FULL,
) -> list[DigestResult]:
    """Пересобирает файлы дайджестов из журнала (§21.5).

    Без параметров пересобирается дайджест последней сессии, с `session_id` —
    указанной, с `all_sessions` — всех сессий индекса. Поле «Последний ход»
    при пересборке не меняется: оно определяется последним ходом сессии, а не
    временем пересборки (§11.1, П-07).
    """

    pairs = store.meta_sessions(project)
    if session_id:
        pairs = [row for row in pairs if row["session_id"] == session_id]
    elif not all_sessions:
        last = last_session(store, project)
        pairs = [last] if last is not None else []

    results: list[DigestResult] = []
    for row in pairs:
        result = build_digest(
            store,
            config,
            memory_root,
            logger,
            row["session_id"],
            row["project"],
            completion=completion,
            hook_event="rebuild_digest",
        )
        results.append(result)
        logger.log(
            "digest",
            "rebuild_digest",
            status="ok" if result.status == "ok" else result.status,
            session_id=row["session_id"],
            project=row["project"],
            digest_chars=result.chars,
            error_detail_code=result.error or None,
        )
    return results
