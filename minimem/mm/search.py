"""Поиск по реплике пользователя: FTS5, пороги, вставка результата.

Основание: ТЗ v1.7 §3.3, §9 (шкала score и порог), §12 (хук Поиска),
§12.1, §13 и §13.1 (формат вставки и санитизация), §16 (счётчик
использования), §17 (`session_returns`), §18 (жизненный цикл хуков),
§19.5 и §22 (логирование), §20 (параметры поиска), §21.2 (`minimem search`).

Модуль читает производный слой SQLite: журнал не читается, модель не
вызывается. Счётчик использования увеличивается только у фактически
вставленных записей (П-11). План и вставка разделены: `plan()` даёт
разбор выдачи с причинами отсева, `perform_search()` превращает его во
вставку, а `minimem search --explain` печатает план без вставки и без
влияния на индекс и счётчики.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from . import insert, query
from .log import Logger
from .store import Store

#: Режимы запроса: все основы через AND либо релаксация до OR (§12 п.3).
MODE_ALL = "all"
MODE_ANY = "any"

#: Причины отсева записи (§21.2).
REJECT_BELOW_THRESHOLD = "below_threshold"
REJECT_ALREADY_RETURNED = "already_returned"
REJECT_SUPPRESSED = "suppressed"
REJECT_SAME_SESSION = "same_session"
REJECT_OTHER_PROJECT = "other_project"
REJECT_NO_BODY = "no_body"
REJECT_NO_DATE = "no_date"
REJECT_OVER_LIMIT = "over_limit"

#: Статусы результата поиска на ходе.
STATUS_INSERTED = "inserted"
STATUS_NO_TERMS = "no_terms"
STATUS_NO_HITS = "no_hits"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"


@dataclass
class Candidate:
    """Запись выдачи с её оценкой и судьбой на этом ходе."""

    event_id: str
    score: float
    project: str = ""
    session_id: str = ""
    turn: str = ""
    timestamp: str = ""
    truncated: str = "none"
    user_text: str = ""
    assistant_text: str = ""
    selected: bool = False
    reject_reason: str = ""

    @property
    def has_date(self) -> bool:
        """Запись без даты вставляется быть не может (§13, §13.1 п.4)."""

        return bool(self.timestamp)


@dataclass
class SearchPlan:
    """Нормативный разбор выдачи поиска (§12, §12.1)."""

    stems: list[str] = field(default_factory=list)
    query_mode: str = MODE_ALL
    match_query: str = ""
    hits: int = 0
    threshold: float = 0.0
    threshold_applied: str = "none"
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def selected(self) -> list[Candidate]:
        return [item for item in self.candidates if item.selected]

    def top_scores(self, limit: int = 5) -> list[float]:
        """Не более пяти лучших score по убыванию (§19.5)."""

        return [round(item.score, 4) for item in self.candidates[:limit]]


@dataclass
class SearchResult:
    """Итог поиска на одном ходе; поле `text` уходит в `{"context": ...}`."""

    status: str = STATUS_NO_HITS
    session_id: str = ""
    project: str = ""
    text: str = ""
    chars: int = 0
    truncated: bool = False
    event_ids: list[str] = field(default_factory=list)
    reason: str = ""
    plan: SearchPlan | None = None

    @property
    def inserted(self) -> bool:
        return self.status == STATUS_INSERTED and bool(self.text)



def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _threshold_text(relative: float, absolute: float, effective: float) -> str:
    """`threshold_applied` строкой: какие границы сработали (§19.5)."""

    rule = "max" if effective == max(relative, absolute) else "min"
    return f"relative>={relative:.4f} absolute>={absolute:.4f} rule={rule}"


def _fill_candidate(store: Store, event_id: str, score: float) -> Candidate:
    """Читает метаданные и текст записи выдачи из служебного слоя (§9.1)."""

    meta = store.meta_get(event_id) or {}
    body = store.body_get(event_id) or {}
    return Candidate(
        event_id=event_id,
        score=score,
        project=str(meta.get("project") or ""),
        session_id=str(meta.get("session_id") or ""),
        turn=str(meta.get("turn") or ""),
        timestamp=str(meta.get("timestamp") or ""),
        truncated=str(meta.get("truncated") or "none"),
        user_text=str(body.get("user_utterance") or ""),
        assistant_text=str(body.get("assistant_answer") or ""),
    )


def _moment(timestamp: str) -> str:
    """`ГГГГ-ММ-ДД ЧЧ:ММ` из времени записи; неразобранное значение — ""."""

    text = timestamp.strip()
    if len(text) < 16:
        return ""
    return f"{text[:10]} {text[11:16]}"


#: Пометка обрезки текста записи (§13.1 п.3).
TRUNCATION_MARK = " [обрезано]"


def _fit(text: str, budget: int) -> tuple[str, bool]:
    """Обрезает текст по границе строк с пометкой обрезки (§13.1 п.3).

    Пометка входит в бюджет: иначе текст вышел бы за лимит на её длину.
    """

    if budget <= 0:
        return "", bool(text)
    if len(text) <= budget:
        return text, False
    if budget <= len(TRUNCATION_MARK):
        return TRUNCATION_MARK.strip()[:budget], True
    head = text[: budget - len(TRUNCATION_MARK)]
    newline = head.rfind("\n")
    if newline > 0:
        head = head[:newline].rstrip()
    if not head:
        head = text[: budget - len(TRUNCATION_MARK)].rstrip()
    return head + TRUNCATION_MARK, True


def render_record(candidate: Candidate, budget: int = 0) -> str:
    """Текст одной записи для вставки (§13).

    Обязательны дата, проект и источник MiniMem; запись без даты не
    возвращается. `budget` — предел на запись: 0 означает «не обрезать».
    При обрезке текста заголовок получает пометку «обрезано».
    """

    moment = _moment(candidate.timestamp)
    if not moment:
        return ""
    user_text = candidate.user_text
    assistant_text = candidate.assistant_text
    if budget > 0:
        fixed = len(f"[{moment}]\n**Вы:** \n**Агент:** ")
        available = budget - fixed
        if available <= 0:
            return ""
        user_text, cut_user = _fit(user_text, available // 2)
        assistant_text, cut_assistant = _fit(assistant_text, available - len(user_text))
        cut = cut_user or cut_assistant
    else:
        cut = candidate.truncated in ("user", "assistant", "both")

    marks = [moment, f"проект {candidate.project}", f"сессия {candidate.session_id}"]
    if candidate.turn:
        marks.append(f"ход {candidate.turn}")
    marks.append("источник: MiniMem")
    if cut:
        marks.append("обрезано")
    return (
        f"[{' · '.join(marks)}]\n"
        f"**Вы:** {user_text}\n"
        f"**Агент:** {assistant_text}"
    )


def plan(
    store: Store,
    config: Mapping[str, Any],
    project: str,
    session_id: str,
    text: str,
    restrict_project: bool = True,
) -> SearchPlan:
    """Строит запрос, выполняет FTS5 и разбирает выдачу (§12 п.1–9).

    `restrict_project = False` не ограничивает выдачу FTS5 проектом: чужие
    записи попадают в результат с пометкой `other_project`. Этот режим нужен
    только `minimem search --explain` (§21.2); хук работает с `True`.
    """

    result = SearchPlan()
    result.stems = query.extract_stems(text or "", config)
    if not result.stems:
        return result

    result.query_mode = MODE_ALL
    result.match_query = query.build_match_query(result.stems, MODE_ALL)
    hits = store.fts_search(result.match_query, project if restrict_project else None)
    if not hits:
        # Релаксация до OR: состав основ не меняется (§12.1 п.7).
        result.query_mode = MODE_ANY
        result.match_query = query.build_match_query(result.stems, MODE_ANY)
        hits = store.fts_search(result.match_query, project if restrict_project else None)
    result.hits = len(hits)

    suppressed = store.suppressed_ids()
    returned = store.session_returned_ids(project, session_id) if session_id else set()

    candidates: list[Candidate] = []
    for event_id, score in hits:
        candidate = _fill_candidate(store, event_id, score)
        if candidate.project != project:
            candidate.reject_reason = REJECT_OTHER_PROJECT
        elif not candidate.user_text and not candidate.assistant_text:
            candidate.reject_reason = REJECT_NO_BODY
        elif not candidate.has_date:
            candidate.reject_reason = REJECT_NO_DATE
        elif event_id in suppressed:
            candidate.reject_reason = REJECT_SUPPRESSED
        elif session_id and candidate.session_id == session_id:
            candidate.reject_reason = REJECT_SAME_SESSION
        elif event_id in returned:
            candidate.reject_reason = REJECT_ALREADY_RETURNED
        candidates.append(candidate)
    result.candidates = candidates

    scores = [item.score for item in candidates]
    top = max(scores) if scores else 0.0
    relative = top * float(config["search_score_ratio"])
    absolute = float(config["search_score_threshold"])
    result.threshold = max(relative, absolute)
    result.threshold_applied = _threshold_text(relative, absolute, result.threshold)

    limit = int(config["max_return_records"])
    chosen = 0
    for candidate in candidates:
        if candidate.reject_reason:
            continue
        if candidate.score < result.threshold:
            candidate.reject_reason = REJECT_BELOW_THRESHOLD
            continue
        if chosen >= limit:
            candidate.reject_reason = REJECT_OVER_LIMIT
            continue
        candidate.selected = True
        chosen += 1
    return result


#: Служебная часть блока вставки: вводная строка, пустая строка, делимитеры.
#: Резервируется до сборки тела, иначе последняя запись попала бы под
#: обрезку вместе со своим заголовком, а §13 требует заголовок с датой.
_BLOCK_OVERHEAD = (
    len(insert.INTRO_LINE) + 2 + len(insert.START_DELIMITER) + 1 + len(insert.END_DELIMITER)
)


def assemble_body(
    config: Mapping[str, Any], selected: Sequence[Candidate]
) -> tuple[str, list[Candidate], bool]:
    """Тело вставки из отобранных записей в пределах `max_return_chars`.

    Бюджет делится поровну между отобранными записями (§13.1 п.3): длинная
    запись обрезается сама и не вытесняет остальные из вставки. Заголовок
    записи с датой при обрезке сохраняется, как требует §13.

    Возвращает пару `(тело, записи)` и признак обрезки текста.
    """

    limit = int(config["max_return_chars"]) - _BLOCK_OVERHEAD
    if limit <= 0:
        return "", [], False
    share = max(limit // max(len(selected), 1), 1)
    kept: list[Candidate] = []
    blocks: list[str] = []
    total = 0
    truncated = False
    for candidate in selected:
        block = render_record(candidate, share)
        if not block:
            continue
        if total + len(block) + 2 > limit:
            break
        kept.append(candidate)
        blocks.append(block)
        total += len(block) + 2
        if "обрезано" in block and candidate.truncated not in (
            "user",
            "assistant",
            "both",
        ):
            truncated = True
    return "\n\n".join(blocks), kept, truncated


def log_search(
    logger: Logger,
    operation: str,
    status: str,
    session_id: str,
    project: str,
    search_plan: SearchPlan,
    returned: Sequence[str] = (),
    deadline_remaining_ms: int | None = None,
    error_detail_code: str | None = None,
    truncation_fields: str | None = None,
) -> None:
    """Лог поиска с обязательными полями контракта измеримости (§19.5, §22)."""

    fields: dict[str, Any] = {
        "session_id": session_id,
        "project": project,
        "hook_event": "pre_llm_call",
        "search_terms": len(search_plan.stems),
        "search_query_mode": search_plan.query_mode,
        "search_hits": search_plan.hits,
        "search_returned": len(returned),
        "returned_event_ids": list(returned),
        "top5_scores": search_plan.top_scores(),
        "threshold_applied": search_plan.threshold_applied,
    }
    if deadline_remaining_ms is not None:
        fields["deadline_remaining_ms"] = deadline_remaining_ms
    if error_detail_code:
        fields["error_detail_code"] = error_detail_code
    if truncation_fields:
        fields["truncation_fields"] = truncation_fields
    logger.log("search", operation, status=status, **fields)


def perform_search(
    store: Store,
    config: Mapping[str, Any],
    logger: Logger,
    project: str,
    session_id: str,
    text: str,
    deadline_remaining_ms: int | None = None,
) -> SearchResult:
    """Выполняет поиск на ходу и возвращает блок к вставке (§12).

    Порядок §12: нормализация -> запрос -> фильтры -> порог -> лимиты ->
    счётчик и отметка о возврате -> вставка. Отсутствие результата ошибкой
    не является: ход Hermes продолжается без вставки (§19.2).
    """

    result = SearchResult(session_id=session_id, project=project)
    search_plan = result.plan = plan(store, config, project, session_id, text)

    if not search_plan.stems:
        result.status = STATUS_NO_TERMS
        result.reason = "no_search_terms"
        log_search(
            logger,
            "search_skipped",
            "skipped",
            session_id,
            project,
            search_plan,
            deadline_remaining_ms=deadline_remaining_ms,
            error_detail_code="no_search_terms",
        )
        return result

    body, kept, records_cut = assemble_body(config, search_plan.selected)
    if not body:
        result.status = STATUS_NO_HITS if not search_plan.hits else STATUS_EMPTY
        result.reason = "nothing_to_return"
        log_search(
            logger,
            "search_completed",
            "skipped",
            session_id,
            project,
            search_plan,
            deadline_remaining_ms=deadline_remaining_ms,
            error_detail_code="no_passable_records",
        )
        return result

    block, truncated = insert.build_memory_block(body, int(config["max_return_chars"]))
    if not block:
        # Бюджета не хватает даже на делимитеры: пустой блок не вставляется.
        result.status = STATUS_EMPTY
        result.reason = "no_insert_budget"
        log_search(
            logger,
            "search_completed",
            "skipped",
            session_id,
            project,
            search_plan,
            deadline_remaining_ms=deadline_remaining_ms,
            error_detail_code="no_insert_budget",
        )
        return result

    used_at = _now_iso()
    with store.connection:
        for candidate in kept:
            store.bump_usage(candidate.event_id, used_at)
            store.session_return_put(project, session_id, candidate.event_id, used_at)

    result.status = STATUS_INSERTED
    result.text = block
    result.chars = len(block)
    result.truncated = truncated or records_cut
    result.event_ids = [candidate.event_id for candidate in kept]
    log_search(
        logger,
        "search_injected",
        "ok",
        session_id,
        project,
        search_plan,
        returned=result.event_ids,
        deadline_remaining_ms=deadline_remaining_ms,
        truncation_fields="return_chars" if truncated else None,
    )
    return result
