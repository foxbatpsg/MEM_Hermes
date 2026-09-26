"""Отчёт `minimem stats` по структурированному логу.

Основание: ТЗ v1.7 §21.3 (П-10 — контракт измеримости), §19.5 (поля лога и
вопрос «почему поиск ничего не нашёл»), §22 (состав полей), §25 (критерий
готовности 28).

Отчёт только читает лог. Обязательное требование §19.5: «нет попаданий» и
«ошибка/таймаут» — разные числа, а не одно. Решения по RB-01 и RB-05
принимаются только по этому отчёту.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

#: Операции, которые считаются запуском поиска (§19.5, §21.3).
#: `search_injected` — успешная вставка, `search_completed` — поиск состоялся,
#: но вставлять нечего, `search_skipped` — поиск не запускался (первый ход,
#: ход Возврата, нулевые термины).
SEARCH_OPERATIONS = frozenset(
    {"search_injected", "search_completed", "search_skipped"}
)

#: Операции захвата, по которым суммируются счётчики ходов (§19.5).
#: `capture_record_written` — успешная запись в журнал: без неё в отчёте
#: `turns_captured` всегда был бы нулём.
CAPTURE_OPERATIONS = frozenset(
    {
        "capture_record_written",
        "capture_disabled",
        "capture_invalid_event",
        "capture_duplicate_ignored",
        "capture_event_conflict",
        "capture_event_revision_added",
        "capture_turn_derived",
        "capture_idempotency_guard_unavailable",
        "record_truncated_to_limit",
        "record_body_withheld_redaction_failed",
        "insert_stripped",
        "redaction_applied",
        "sender_changed",
    }
)

#: Операции индексатора (§19.5).
INDEX_OPERATIONS = frozenset({"incremental_index", "rebuild_index"})

#: Операции catch-up (§18.1, §19.5).
CATCH_UP_OPERATIONS = frozenset(
    {"catch_up", "catch_up_timeout", "catch_up_state_unavailable"}
)

#: Операции дайджеста (§14, §19.5).
DIGEST_OPERATIONS = frozenset(
    {
        "digest_created",
        "rebuild_digest",
        "digest_created_field_fallback",
        "digest_interrupted_fallback",
        "digest_empty_session",
    }
)

#: Операции уплотнения (§15, §19.5).
COMPACTION_OPERATIONS = frozenset({"compaction_applied", "compaction_rebuilt"})

#: Признаки таймаута в записи лога (§22).
TIMEOUT_MARKERS = ("timeout", "time_out")


def as_int(record: Mapping[str, Any], key: str) -> int:
    """Числовое поле записи лога; отсутствующее и неверное даёт 0."""

    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def as_scores(value: Any) -> list[float]:
    """Поле `top5_scores` приводится к списку чисел."""

    if not isinstance(value, (list, tuple)):
        return []
    return [
        float(item)
        for item in value
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]


def is_timeout(record: Mapping[str, Any]) -> bool:
    """Таймаут ли это: операция, код ошибки или исчерпанный остаток дедлайна."""

    operation = str(record.get("operation") or "").casefold()
    if any(marker in operation for marker in TIMEOUT_MARKERS):
        return True
    for key in ("error_code", "error_detail_code"):
        value = record.get(key)
        if isinstance(value, str) and any(
            marker in value.casefold() for marker in TIMEOUT_MARKERS
        ):
            return True
    remaining = record.get("deadline_remaining_ms")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        return remaining <= 0
    return False


def parse_moment(value: Any) -> datetime | None:
    """Разбирает `timestamp` записи лога (ISO-8601 со смещением)."""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_date_arg(value: str) -> datetime:
    """`--since` / `--until` принимают дату или метку времени (§21.3)."""

    text = str(value).strip()
    try:
        return datetime.combine(date.fromisoformat(text), time.min, tzinfo=timezone.utc)
    except ValueError:
        pass
    moment = parse_moment(text)
    if moment is None:
        raise ValueError(f"не удалось разобрать дату: {value}")
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def read_log_records(log_file: Path) -> list[dict[str, Any]]:
    """Читает записи лога; нечитаемые строки пропускаются (§19.2)."""

    if not log_file.is_file():
        return []
    records: list[dict[str, Any]] = []
    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                records.append(raw)
    return records


def percentile(values: Sequence[float], share: float) -> float:
    """Перцентиль по ближайшему рангу: для отчёта этого достаточно."""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(share * (len(ordered) - 1)))))
    return float(ordered[index])


@dataclass
class StatsReport:
    """Агрегаты нормативного перечня §21.3."""

    period_since: str = ""
    period_until: str = ""
    project: str = ""
    records_total: int = 0
    # Поиск.
    search_runs: int = 0
    search_empty: int = 0
    search_errors: int = 0
    search_timeouts: int = 0
    search_no_hits: int = 0
    search_terms_total: int = 0
    search_hits_total: int = 0
    search_returned_total: int = 0
    search_scores: list[float] = field(default_factory=list)
    search_score_ratio: float = 0.0
    search_score_threshold: float = 0.0
    # Захват.
    turns_seen: int = 0
    turns_captured: int = 0
    turns_skipped: int = 0
    turns_derived: int = 0
    truncations: int = 0
    redactions: int = 0
    duplicates: int = 0
    withheld: int = 0
    # Индексатор.
    indexer_runs: int = 0
    records_indexed: int = 0
    cursor_resets: int = 0
    records_damaged: int = 0
    # Catch-up.
    catch_up_runs: int = 0
    catch_up_timeouts: int = 0
    # Дайджест.
    digest_created: int = 0
    digest_rebuilt: int = 0
    digest_fallback: int = 0
    digest_interrupted: int = 0
    # Уплотнение.
    suppressed_age: int = 0
    suppressed_unused: int = 0
    suppressed_duplicate: int = 0
    # Задержки хуков.
    latency_p50: int = 0
    latency_p95: int = 0
    latency_max: int = 0
    timeouts: int = 0
    # Конфигурация.
    config_hash: str = ""
    minimem_version: str = ""

    @staticmethod
    def ratio(total: int, count: int) -> float:
        return round(total / count, 4) if count else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Словарь метрик для `--json` (§21.3)."""

        return {
            "period": {"since": self.period_since, "until": self.period_until},
            "project": self.project,
            "log_records": self.records_total,
            "search": {
                "runs": self.search_runs,
                "empty_results": self.search_empty,
                "no_hits": self.search_no_hits,
                "errors": self.search_errors,
                "timeouts": self.search_timeouts,
                "terms_total": self.search_terms_total,
                "hits_total": self.search_hits_total,
                "returned_total": self.search_returned_total,
                "avg_terms": self.ratio(self.search_terms_total, self.search_runs),
                "avg_hits": self.ratio(self.search_hits_total, self.search_runs),
                "empty_share": self.ratio(self.search_empty, self.search_runs),
                "scores": self.search_scores,
                "score_ratio": self.search_score_ratio,
                "score_threshold": self.search_score_threshold,
            },
            "capture": {
                "turns_seen": self.turns_seen,
                "turns_captured": self.turns_captured,
                "turns_skipped": self.turns_skipped,
                "turns_derived": self.turns_derived,
                "truncations": self.truncations,
                "redactions": self.redactions,
                "duplicates": self.duplicates,
                "body_withheld": self.withheld,
            },
            "indexer": {
                "runs": self.indexer_runs,
                "records_indexed": self.records_indexed,
                "cursor_resets": self.cursor_resets,
                "records_damaged": self.records_damaged,
            },
            "catch_up": {"runs": self.catch_up_runs, "timeouts": self.catch_up_timeouts},
            "digest": {
                "created": self.digest_created,
                "rebuilt": self.digest_rebuilt,
                "field_fallback": self.digest_fallback,
                "interrupted": self.digest_interrupted,
            },
            "compaction": {
                "rule1_age": self.suppressed_age,
                "rule2_unused": self.suppressed_unused,
                "rule3_duplicate": self.suppressed_duplicate,
            },
            "hooks": {
                "p50_ms": self.latency_p50,
                "p95_ms": self.latency_p95,
                "max_ms": self.latency_max,
                "timeouts": self.timeouts,
            },
            "config": {
                "config_hash": self.config_hash,
                "minimem_version": self.minimem_version,
            },
        }

    def render(self) -> str:
        """Текстовый отчёт: те же числа, что и в `--json` (§21.3)."""

        return "\n".join(
            [
                f"период: {self.period_since} .. {self.period_until}",
                f"проект: {self.project or 'все'}",
                f"записей лога: {self.records_total}",
                "поиск: "
                f"запусков {self.search_runs}, пустых выдач {self.search_empty}, "
                f"нет попаданий {self.search_no_hits}, ошибок {self.search_errors}, "
                f"таймаутов {self.search_timeouts}",
                f"  терминов всего {self.search_terms_total} "
                f"(в среднем {self.ratio(self.search_terms_total, self.search_runs)}), "
                f"попаданий {self.search_hits_total} "
                f"(в среднем {self.ratio(self.search_hits_total, self.search_runs)}), "
                f"возвращено {self.search_returned_total}",
                f"  распределение score: {self.search_scores or 'нет данных'}",
                f"  калибровка: search_score_ratio={self.search_score_ratio}, "
                f"search_score_threshold={self.search_score_threshold}",
                f"захват: turns_seen {self.turns_seen}, "
                f"turns_captured {self.turns_captured}, "
                f"turns_skipped {self.turns_skipped}, "
                f"turns_derived {self.turns_derived}, обрезок {self.truncations}, "
                f"redaction {self.redactions}, дублей {self.duplicates}, "
                f"body withheld {self.withheld}",
                f"индексатор: проходов {self.indexer_runs}, проиндексировано "
                f"{self.records_indexed}, сбросов курсора {self.cursor_resets}, "
                f"повреждённых записей {self.records_damaged}",
                f"catch-up: попыток {self.catch_up_runs}, "
                f"таймаутов {self.catch_up_timeouts}",
                f"дайджест: создан {self.digest_created}, пересобран {self.digest_rebuilt}, "
                f"fallback по полю {self.digest_fallback}, прерванных сессий "
                f"{self.digest_interrupted}",
                f"уплотнение: правило 1 (возраст) {self.suppressed_age}, "
                f"правило 2 (неиспользованные) {self.suppressed_unused}, "
                f"правило 3 (дубли) {self.suppressed_duplicate}",
                f"задержки хуков: p50 {self.latency_p50} мс, p95 {self.latency_p95} мс, "
                f"max {self.latency_max} мс, таймаутов {self.timeouts}",
                f"конфигурация: config_hash {self.config_hash or '—'}, "
                f"minimem_version {self.minimem_version or '—'}",
            ]
        )


def filter_records(
    records: list[Mapping[str, Any]],
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
) -> list[Mapping[str, Any]]:
    """Отбирает записи лога по периоду и проекту (§21.3)."""

    selected: list[Mapping[str, Any]] = []
    for record in records:
        if project:
            value = record.get("project")
            if value and str(value).casefold() != project.casefold():
                continue
        if since is not None or until is not None:
            moment = parse_moment(record.get("timestamp"))
            if moment is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                if since is not None and moment < since:
                    continue
                if until is not None and moment > until:
                    continue
        selected.append(record)
    return selected


def collect(
    records: list[Mapping[str, Any]],
    config: Mapping[str, Any] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
) -> StatsReport:
    """Считает нормативный перечень метрик §21.3 по записям лога.

    «Нет попаданий» и «ошибка/таймаут» считаются раздельно: это ответ на
    вопрос §19.5 «почему поиск ничего не нашёл» (П-10, тест MM-142).
    """

    report = StatsReport(
        period_since=since.isoformat() if since else "",
        period_until=until.isoformat() if until else "",
        project=project or "",
    )
    if config is not None:
        report.search_score_ratio = float(config.get("search_score_ratio") or 0.0)
        report.search_score_threshold = float(config.get("search_score_threshold") or 0.0)
        report.minimem_version = str(config.get("minimem_version") or "")
        report.config_hash = str(config.get("__config_hash__") or "")

    latencies: list[float] = []
    for record in filter_records(list(records), since, until, project):
        report.records_total += 1
        operation = str(record.get("operation") or "")
        duration = record.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            latencies.append(float(duration))
        if is_timeout(record):
            report.timeouts += 1

        if operation in SEARCH_OPERATIONS:
            report.search_runs += 1
            report.search_terms_total += as_int(record, "search_terms")
            report.search_hits_total += as_int(record, "search_hits")
            returned = as_int(record, "search_returned")
            report.search_returned_total += returned
            report.search_scores.extend(as_scores(record.get("top5_scores")))
            if is_timeout(record):
                # Таймаут — тоже отказ: §19.5 различает «нет попаданий»
                # и «ошибка/таймаут», а не три отдельных состояния.
                report.search_timeouts += 1
                report.search_errors += 1
            elif str(record.get("status") or "") == "error":
                report.search_errors += 1
            elif returned == 0:
                report.search_empty += 1
                if as_int(record, "search_hits") == 0:
                    report.search_no_hits += 1

        if operation in CAPTURE_OPERATIONS:
            report.turns_seen += as_int(record, "turns_seen")
            report.turns_captured += as_int(record, "turns_captured")
            report.turns_skipped += as_int(record, "turns_skipped")
            report.turns_derived += as_int(record, "turns_derived")
            if operation == "record_truncated_to_limit":
                report.truncations += 1
            if operation == "record_body_withheld_redaction_failed":
                report.withheld += 1
            if operation == "redaction_applied" or as_int(record, "redaction_count"):
                report.redactions += max(1, as_int(record, "redaction_count"))
            if operation in (
                "capture_duplicate_ignored",
                "capture_event_conflict",
                "capture_event_revision_added",
            ):
                report.duplicates += 1

        if operation in INDEX_OPERATIONS:
            report.indexer_runs += 1
            report.records_indexed += as_int(record, "records_indexed")
        elif operation == "cursor_reset":
            report.cursor_resets += 1
        elif operation == "journal_record_damaged":
            report.records_damaged += 1

        if operation in CATCH_UP_OPERATIONS:
            report.catch_up_runs += 1
            if operation == "catch_up_timeout" or is_timeout(record):
                report.catch_up_timeouts += 1

        if operation in DIGEST_OPERATIONS:
            if operation == "digest_created":
                report.digest_created += 1
            elif operation == "rebuild_digest":
                report.digest_rebuilt += 1
            elif operation == "digest_created_field_fallback":
                report.digest_fallback += 1
            elif operation == "digest_interrupted_fallback":
                report.digest_interrupted += 1

        if operation in COMPACTION_OPERATIONS:
            by_rule = record.get("suppression_by_rule")
            if isinstance(by_rule, Mapping):
                report.suppressed_age += as_int(by_rule, "age")
                report.suppressed_unused += as_int(by_rule, "unused")
                report.suppressed_duplicate += as_int(by_rule, "duplicate")
            else:
                report.suppressed_age += as_int(record, "records_suppressed")

    report.latency_p50 = int(percentile(latencies, 0.50))
    report.latency_p95 = int(percentile(latencies, 0.95))
    report.latency_max = int(max(latencies)) if latencies else 0
    report.search_scores = [round(value, 4) for value in report.search_scores]
    return report


def collect_from_log(
    log_file: Path,
    config: Mapping[str, Any] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
) -> StatsReport:
    """Читает лог и считает отчёт за период (по умолчанию — текущие сутки)."""

    if since is None and until is None:
        until = datetime.now(timezone.utc)
        since = until.replace(hour=0, minute=0, second=0, microsecond=0)
    records = read_log_records(log_file)
    return collect(records, config=config, since=since, until=until, project=project)


