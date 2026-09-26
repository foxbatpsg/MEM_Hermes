"""Уплотнение: механическое снятие записей с поиска без удаления истории.

Основание: ТЗ v1.7 §3.5, §3.7.7 (`mode_compaction`), §4.3, §9.1
(`suppressed_records`, `rebuilt_at`), §15 (правила 1–3 и tie-breaker),
§15.1 (`compaction_rule2_enabled`), §16 (счётчик использования), §19.2
(изоляция ошибок), §19.5 и §22 (логирование), §20 (параметры уплотнения),
§21.5 (команда `compact`).

Модуль не меняет журнал, не удаляет и не редактирует записи, не объединяет их
семантически и не вызывает модель. Единственный результат — добавление
`event_id` в таблицу `suppressed_records`; потребители снятых — Поиск
(`search.plan`) и Дайджест (`digest._collect_turns`) — читают её сами.

Данные берутся из производного слоя SQLite: журнал не открывается. Правила 1 и
3 определяются журналом и воспроизводятся после `rebuild-index`; правило 2
зависит от `usage_count` и после пересборки не воспроизводится (§15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cmp_to_key
from typing import Any, Mapping, Sequence

from .log import Logger
from .store import Store

#: Причины снятия записываются в `suppressed_records.reason` (§9.1, §15).
REASON_AGE = "age"
REASON_UNUSED = "unused"
REASON_DUPLICATE = "duplicate"

#: Правила §15. Правило 2 выполняется только при `compaction_rule2_enabled`.
RULE_AGE = 1
RULE_UNUSED = 2
RULE_DUPLICATE = 3

#: Набор правил, воспроизводимых после `rebuild-index` (§15, §16).
REBUILD_RULES: tuple[int, ...] = (RULE_AGE, RULE_DUPLICATE)

#: Порядок присвоения причины при совпадении нескольких правил.
#: Правила проверяются в порядке 1 -> 3 -> 2: причина не влияет на результат
#: (запись снимается при совпадении хотя бы одного правила), но делает запись
#: в служебном слое воспроизводимой по одному правилу.
REASON_PRIORITY: tuple[int, ...] = (RULE_AGE, RULE_DUPLICATE, RULE_UNUSED)

REASON_BY_RULE: dict[int, str] = {
    RULE_AGE: REASON_AGE,
    RULE_UNUSED: REASON_UNUSED,
    RULE_DUPLICATE: REASON_DUPLICATE,
}

#: Операции уплотнения в логе (§22: перечень открыт для новых операций).
OPERATION_SKIPPED = "compaction_skipped"
OPERATION_APPLIED = "compaction_applied"
OPERATION_REBUILT = "compaction_rebuilt"

#: Статусы результата уплотнения.
STATUS_APPLIED = "applied"
STATUS_DISABLED = "disabled"
STATUS_FAILED = "failed"

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Suppression:
    """Одна снятая запись: `event_id` и правило, по которому она снята."""

    event_id: str
    reason: str
    project: str = ""


@dataclass
class CompactionPlan:
    """Результат оценки записей: что и по какому правилу снимается."""

    scanned: int = 0
    suppressions: list[Suppression] = field(default_factory=list)
    rules: tuple[int, ...] = REBUILD_RULES
    rule2_enabled: bool = False
    rebuilt_at: str = ""

    @property
    def event_ids(self) -> set[str]:
        return {item.event_id for item in self.suppressions}

    def by_reason(self) -> dict[str, int]:
        counts = {REASON_AGE: 0, REASON_UNUSED: 0, REASON_DUPLICATE: 0}
        for item in self.suppressions:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


@dataclass
class CompactionResult:
    """Итог одного запуска уплотнения."""

    status: str = STATUS_APPLIED
    scanned: int = 0
    suppressions: list[Suppression] = field(default_factory=list)
    already_suppressed: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def applied(self) -> bool:
        return self.status == STATUS_APPLIED

    def event_ids(self) -> set[str]:
        return {item.event_id for item in self.suppressions}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_moment(stamp: str) -> datetime | None:
    """ISO-8601 со смещением -> aware datetime; неразобранное значение -> None."""

    text = (stamp or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def age_days(timestamp_utc: str, now: datetime | None = None) -> float | None:
    """Возраст записи в днях по `timestamp_utc`; неизвестное время -> None.

    `None` означает, что правила 1 и 2 к записи неприменимы: возраст не
    вычислен, а не равен нулю.
    """

    moment = _parse_moment(timestamp_utc)
    if moment is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return (reference - moment).total_seconds() / SECONDS_PER_DAY


def rule2_applies(
    store: Store,
    config: Mapping[str, Any],
    record: Mapping[str, Any],
    usage: Mapping[str, int],
    now: datetime | None = None,
    rebuilt_at: str | None = None,
) -> bool:
    """Правило 2: неиспользованная запись старше `compaction_unused_age`.

    Условия §15 п. 2: `usage_count = 0`, возраст больше
    `compaction_unused_age`, запись создана после последней пересборки
    (`timestamp_utc > rebuilt_at`) и `compaction_rule2_enabled = true`.
    Правило выключено по умолчанию (§15.1, RB-01).
    """

    if not bool(config["compaction_rule2_enabled"]):
        return False
    marker = store.get_meta("rebuilt_at") if rebuilt_at is None else rebuilt_at
    if not marker:
        # Пересборки не было: условие «создана после пересборки» не выполнено.
        return False
    if usage.get(str(record["event_id"]), 0) != 0:
        return False
    age = age_days(str(record.get("timestamp_utc") or ""), now)
    if age is None or age <= float(config["compaction_unused_age"]):
        return False
    return str(record.get("timestamp_utc") or "") > marker


#: Признак «числовой turn равен или отсутствует» в §15: если у обеих записей
#: `turn_numeric` есть и различается, сравниваются числа; иначе сравнение
#: переходит на `turn` как строку (это же относится к производным ходам,
#: у которых `turn_numeric IS NULL`).
TURN_NUMERIC_MISSING = -1


def _sort_tuple(record: Mapping[str, Any]) -> tuple[str, int, int, str, str]:
    """Промежуточное представление записи для упорядочивания по §15.

    `turn` и `event_id` хранятся раздельно: смешивать их в одну строку нельзя,
    иначе `turn` сравнивался бы с началом `event_id`.
    """

    turn_numeric = record.get("turn_numeric")
    return (
        str(record.get("timestamp_utc") or ""),
        TURN_NUMERIC_MISSING if turn_numeric is None else 1,
        int(turn_numeric) if turn_numeric is not None else 0,
        str(record.get("turn") or ""),
        str(record.get("event_id") or ""),
    )


def _compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Порядок §15: `timestamp_utc`, затем `turn_numeric`, `turn`, `event_id`.

    `turn_numeric` сравнивается, только если он есть у обеих записей: при его
    отсутствии у любой из них норма прямо переводит сравнение на `turn` как
    строку. Возвращаемое значение предназначено для `functools.cmp_to_key`.
    """

    first, second = _sort_tuple(left), _sort_tuple(right)
    if first[0] != second[0]:
        return -1 if first[0] < second[0] else 1
    left_numeric = left.get("turn_numeric")
    right_numeric = right.get("turn_numeric")
    if left_numeric is not None and right_numeric is not None and left_numeric != right_numeric:
        return -1 if int(left_numeric) < int(right_numeric) else 1
    for a, b in zip(first[3:], second[3:]):
        if a != b:
            return -1 if a < b else 1
    return 0


def tie_break_key(record: Mapping[str, Any]) -> Any:
    """Ключ сортировки группы дублей по §15 (MM-60)."""

    return cmp_to_key(_compare)(record)


def _duplicate_event_ids(records: Sequence[Mapping[str, Any]]) -> set[str]:
    """Правило 3: лишние записи одинакового `content_hash` внутри проекта.

    Одинаковое содержимое в разных проектах дублем не считается: группы
    строятся по паре (project, content_hash) (§15, тест 43). Остаётся первая
    запись по tie-breaker, снимаются остальные (тест 38).
    """

    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for record in records:
        key = (str(record.get("project") or ""), str(record.get("content_hash") or ""))
        groups.setdefault(key, []).append(record)

    duplicates: set[str] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        for record in sorted(members, key=tie_break_key)[1:]:
            duplicates.add(str(record["event_id"]))
    return duplicates


def plan_compaction(
    store: Store,
    config: Mapping[str, Any],
    rules: tuple[int, ...] = REBUILD_RULES,
    now: datetime | None = None,
) -> CompactionPlan:
    """Оценивает записи индекса и строит план снятия; состояние не меняется.

    Правила 1 и 3 применяются всегда, правило 2 — только при
    `compaction_rule2_enabled = true` (§15, §15.1). План используется и для
    записи (`apply_plan`), и для разбора `--dry-run` команды `compact`.
    """

    result = CompactionPlan(rules=tuple(rules))
    result.rule2_enabled = bool(config["compaction_rule2_enabled"])
    result.rebuilt_at = store.get_meta("rebuilt_at") or ""

    records = store.meta_all()
    result.scanned = len(records)
    usage = store.usage_all()
    max_age = float(config["compaction_age"])
    duplicates = _duplicate_event_ids(records) if RULE_DUPLICATE in result.rules else set()

    for record in records:
        reasons: list[str] = []
        if RULE_AGE in result.rules:
            age = age_days(str(record.get("timestamp_utc") or ""), now)
            if age is not None and age > max_age:
                reasons.append(REASON_AGE)
        if RULE_DUPLICATE in result.rules and str(record["event_id"]) in duplicates:
            reasons.append(REASON_DUPLICATE)
        if RULE_UNUSED in result.rules and rule2_applies(
            store, config, record, usage, now, result.rebuilt_at
        ):
            reasons.append(REASON_UNUSED)
        if not reasons:
            continue
        reason = next(
            REASON_BY_RULE[rule] for rule in REASON_PRIORITY if REASON_BY_RULE[rule] in reasons
        )
        result.suppressions.append(
            Suppression(
                event_id=str(record["event_id"]),
                reason=reason,
                project=str(record.get("project") or ""),
            )
        )
    return result


def apply_plan(store: Store, plan: CompactionPlan, suppressed_at: str | None = None) -> int:
    """Записывает план в `suppressed_records`; возвращает число новых записей.

    Запись с уже существующим `event_id` не переписывается: причина и время
    снятия остаются от первого применения, поэтому повторный запуск
    уплотнения ничего не меняет (идемпотентность, тест 37).
    """

    existing = store.suppressed_ids()
    stamp = suppressed_at or _now_iso()
    fresh = [item for item in plan.suppressions if item.event_id not in existing]
    with store.connection:
        for item in fresh:
            store.suppress(item.event_id, item.reason, stamp)
    return len(fresh)


def active_rules(config: Mapping[str, Any], rules: tuple[int, ...] | None) -> tuple[int, ...]:
    """Набор правил прогона: 1 и 3 всегда, 2 — только при включённом флаге."""

    if rules is not None:
        return tuple(rules)
    if bool(config["compaction_rule2_enabled"]):
        return (RULE_AGE, RULE_DUPLICATE, RULE_UNUSED)
    return (RULE_AGE, RULE_DUPLICATE)


def run_compaction(
    store: Store,
    config: Mapping[str, Any],
    logger: Logger,
    rules: tuple[int, ...] | None = None,
    now: datetime | None = None,
    session_id: str = "",
    project: str = "",
    operation: str = OPERATION_APPLIED,
    dry_run: bool = False,
) -> CompactionResult:
    """Выполняет уплотнение и возвращает итог (§15).

    `rules = None` — обычный прогон: правила 1 и 3 плюс правило 2, если оно
    включено флагом. После `rebuild-index` вызывается с `REBUILD_RULES`:
    счётчики обнулены, и правило 2 невосстановимо (§15, §16, тест 44).
    `dry_run` не пишет состояние — это режим `minimem compact --dry-run`.
    """

    result = CompactionResult()
    applied_rules = active_rules(config, rules)
    try:
        plan = plan_compaction(store, config, applied_rules, now)
        result.scanned = plan.scanned
        if dry_run:
            result.status = STATUS_APPLIED
            result.suppressions = plan.suppressions
        else:
            fresh = apply_plan(store, plan)
            result.already_suppressed = len(plan.suppressions) - fresh
            result.suppressions = plan.suppressions
    except Exception as exc:  # noqa: BLE001 - изоляция ошибок (§19.2)
        result.status = STATUS_FAILED
        result.errors.append(type(exc).__name__)
        logger.error(
            "compaction",
            operation,
            error_code="compaction_failed",
            session_id=session_id,
            project=project,
            records_scanned=result.scanned,
        )
        return result

    logger.log(
        "compaction",
        operation,
        status="ok",
        session_id=session_id,
        project=project,
        records_scanned=result.scanned,
        records_suppressed=0 if dry_run else len(result.suppressions),
        records_planned=len(result.suppressions),
        records_already_suppressed=result.already_suppressed,
        suppression_by_rule=plan_compaction_counts(result.suppressions),
        rules_applied=list(applied_rules),
    )
    return result


def plan_compaction_counts(suppressions: Sequence[Suppression]) -> dict[str, int]:
    """Счётчики снятых по причинам — для лога и вывода CLI (§19.5)."""

    counts = {REASON_AGE: 0, REASON_UNUSED: 0, REASON_DUPLICATE: 0}
    for item in suppressions:
        counts[item.reason] = counts.get(item.reason, 0) + 1
    return counts


def rebuild_suppression(
    store: Store,
    config: Mapping[str, Any],
    logger: Logger,
    now: datetime | None = None,
) -> CompactionResult:
    """Пересчитывает снятые записи после `rebuild-index` (правила 1 и 3).

    Таблица `suppressed_records` уже очищена `drop_index_layer`, поэтому план
    применяется к пустому состоянию: записи, снятые ранее только по правилу 2,
    возвращаются в поиск, а снятые по правилам 1 и 3 остаются снятыми
    (§15, тест 44).
    """

    return run_compaction(
        store, config, logger, rules=REBUILD_RULES, now=now, operation=OPERATION_REBUILT
    )


def auto_compaction_enabled(config: Mapping[str, Any]) -> bool:
    """`mode_compaction` — единственный gate автоматического уплотнения.

    При `mode_compaction = false` автоматическое уплотнение не выполняется ни
    на `on_session_end`, ни в catch-up, а существующее состояние снятия не
    меняется (§3.5, §3.7.7, тест MM-54). Явная команда `compact` этому флагу
    не подчиняется: это административное действие (§3.5, §21.5).
    """

    return bool(config["mode_compaction"])


def note_disabled(
    config: Mapping[str, Any],
    logger: Logger,
    session_id: str = "",
    project: str = "",
    hook_event: str = "on_session_end",
) -> CompactionResult:
    """Логирует отказ по `mode_compaction = false`, не меняя состояние.

    Отсутствие уплотнения не ошибка: ход Hermes продолжается (§19.2).
    """

    result = CompactionResult(status=STATUS_DISABLED)
    logger.log(
        "compaction",
        OPERATION_SKIPPED,
        status="skipped",
        session_id=session_id,
        project=project,
        hook_event=hook_event,
        compaction_rule2_enabled=bool(config["compaction_rule2_enabled"]),
        error_detail_code="mode_compaction_disabled",
    )
    return result
