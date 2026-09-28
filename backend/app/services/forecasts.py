"""Журнал прогнозов, его итог по неделям и карточка с историей, событиями и решениями."""
from collections.abc import Iterable
from datetime import date, timedelta
from operator import attrgetter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.common import Page
from ..schemas.forecasts import (
    CalendarInfo,
    DecisionOut,
    DynamicsPoint,
    FactorItem,
    ForecastCard,
    ForecastItem,
    ForecastSummary,
    ForecastWeek,
    VersionItem,
)
from . import quality
from .helpers import Refs, count, from_db, msk_midnight, page_of, to_db

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота",
               "воскресенье"]
DYNAMICS_DAYS = 30
HOLIDAYS = {(1, day) for day in range(1, 9)} | {(2, 23), (3, 8), (5, 1), (5, 9),
                                                (6, 12), (11, 4)}


def decision_out(row: models.Decision) -> DecisionOut:
    return DecisionOut(id=row.id, action=row.action, reason_code=row.reason_code,
                       comment=row.comment, author=row.author,
                       created_at=from_db(row.created_at), source=row.source)


def work_order_ids(db: Session, forecast_ids: set[str] | None = None) -> dict[str, str]:
    """forecast_id → id заявки. Заявок в демо десятки, поэтому читаем все."""
    out: dict[str, str] = {}
    for order_id, stored_ids in db.execute(
            select(models.WorkOrder.id, models.WorkOrder.forecast_ids)
            .order_by(models.WorkOrder.created_at)):
        for forecast_id in stored_ids or []:
            if forecast_ids is not None and forecast_id not in forecast_ids:
                continue
            out.setdefault(forecast_id, order_id)
    return out


def _items(db: Session, rows: list[models.Forecast]) -> list[ForecastItem]:
    ids = [r.id for r in rows]
    refs = Refs(db, {r.channel_id for r in rows})
    last_decision: dict[str, models.Decision] = {}
    for d in db.scalars(select(models.Decision).where(models.Decision.forecast_id.in_(ids))
                        .order_by(models.Decision.created_at, models.Decision.id)):
        last_decision[d.forecast_id] = d
    outcomes = {o.forecast_id: o for o in db.scalars(
        select(models.Outcome).where(models.Outcome.forecast_id.in_(ids)))}
    orders = work_order_ids(db, set(ids))
    items = []
    for r in rows:
        outcome = outcomes.get(r.id)
        decision = last_decision.get(r.id)
        items.append(ForecastItem(
            id=r.id, kind=r.kind, scenario=r.scenario,
            scenario_title=vocab.scenario(r.scenario)["title"], head=r.head, asof=r.asof,
            valid_from=from_db(r.valid_from), valid_to=from_db(r.valid_to),
            horizon_hours=r.horizon_hours, score_type=r.score_type, risk=r.risk,
            priority_score=r.priority_score, rank=r.rank,
            object=refs.object_ref(r.obj_id, r.address),
            channel=refs.channel_ref(r.channel_id, r.address),
            data_status=r.data_status,
            decision=decision_out(decision) if decision else None,
            outcome_auto=outcome.outcome_auto if outcome else None,
            outcome_manual=outcome.outcome_manual if outcome else None,
            source=r.source, work_order_id=orders.get(r.id), case_key=r.case_key,
        ))
    return items


def _filters(*, scenario: str | None, date_from: date | None, date_to: date | None,
             decision: str | None, outcome: str | None, obj: str | None) -> list:
    """Условия журнала: одни на список и на итог, чтобы итог считал ровно строки списка."""
    clauses = [models.Forecast.in_budget.is_(True)]
    if scenario is not None:
        clauses.append(models.Forecast.scenario == scenario)
    if date_from is not None:
        clauses.append(models.Forecast.asof >= date_from)
    if date_to is not None:
        clauses.append(models.Forecast.asof <= date_to)
    if obj is not None:
        clauses.append(models.Forecast.obj_id == obj)
    if decision == "none":
        clauses.append(~models.Forecast.id.in_(select(models.Decision.forecast_id)))
    elif decision == "any":
        clauses.append(models.Forecast.id.in_(select(models.Decision.forecast_id)))
    elif decision is not None:
        # Фильтр по последнему решению — тому, что строка журнала показывает в колонке.
        # Решения пишутся по порядку, поэтому последнее — с наибольшим id.
        latest = select(func.max(models.Decision.id)).group_by(models.Decision.forecast_id)
        clauses.append(models.Forecast.id.in_(select(models.Decision.forecast_id).where(
            models.Decision.id.in_(latest), models.Decision.action == decision)))
    if outcome is not None:
        clauses.append(models.Forecast.id.in_(select(models.Outcome.forecast_id).where(
            (models.Outcome.outcome_auto == outcome) | (models.Outcome.outcome_manual == outcome))))
    return clauses


JOURNAL_ORDER = (models.Forecast.asof.desc(), models.Forecast.rank, models.Forecast.id)


def _first_per_group(rows: Iterable, group_by: str | None) -> list:
    """Группировка журнала: первая строка группы в порядке JOURNAL_ORDER — последний прогноз."""
    if not group_by:
        return list(rows)
    key = attrgetter("obj_id" if group_by == "obj" else "case_key")
    out, seen = [], set()
    for row in rows:
        if key(row) not in seen:
            out.append(row)
            seen.add(key(row))
    return out


def list_forecasts(db: Session, *, scenario: str | None, date_from: date | None,
                   date_to: date | None, decision: str | None, outcome: str | None,
                   obj: str | None, group_by: str | None, page: int,
                   page_size: int) -> Page[ForecastItem]:
    stmt = select(models.Forecast).where(*_filters(
        scenario=scenario, date_from=date_from, date_to=date_to, decision=decision,
        outcome=outcome, obj=obj))
    ordered = stmt.order_by(*JOURNAL_ORDER)
    if group_by:
        grouped = _first_per_group(db.scalars(ordered), group_by)
        total = len(grouped)
        rows = grouped[(page - 1) * page_size:page * page_size]
    else:
        total = count(db, stmt)
        rows = list(db.scalars(ordered.offset((page - 1) * page_size).limit(page_size)))
    return page_of(ForecastItem, _items(db, rows), total, page, page_size)


def summary(db: Session, *, scenario: str | None, date_from: date | None,
            date_to: date | None, decision: str | None, outcome: str | None,
            obj: str | None, group_by: str | None) -> ForecastSummary:
    """Итог журнала по неделям: строки те же, что у list_forecasts, но все, а не страница.

    Неделя — понедельник–воскресенье по asof. asof — дата расчёта по МСК (C1), поэтому
    пояса пересчитывать не нужно. Попадания, промахи и неизвестные считает quality.tally,
    как на экране «Качество». Недели без строк не возвращаются: журнал их не покажет.
    """
    stmt = (select(models.Forecast.asof, models.Forecast.obj_id, models.Forecast.case_key,
                   models.Outcome.outcome_auto,
                   models.Forecast.id.in_(select(models.Decision.forecast_id)).label("decided"))
            .outerjoin(models.Outcome, models.Outcome.forecast_id == models.Forecast.id)
            .where(*_filters(scenario=scenario, date_from=date_from, date_to=date_to,
                             decision=decision, outcome=outcome, obj=obj))
            .order_by(*JOURNAL_ORDER))
    weeks: dict[date, list] = {}
    for row in _first_per_group(db.execute(stmt), group_by):
        weeks.setdefault(quality.monday(row.asof), []).append(row)
    return ForecastSummary(weeks=[
        ForecastWeek(week_start=start, **quality.tally(r.outcome_auto for r in rows),
                     decided=sum(bool(r.decided) for r in rows))
        for start, rows in sorted(weeks.items())])


def _dynamics(db: Session, row: models.Forecast) -> list[DynamicsPoint]:
    """События канала за 30 суток по МСК: все 30 суток, у каждых — число событий.

    Сутки без единого события возвращаются с events=0, а не выбрасываются и не считаются
    исправными: состояние канала в эти сутки неизвестно. Для потери связи молчание канала
    и есть симптом, так что alarms=0 в такие сутки не значит «тревог не было». В БД
    попадают только события, принятые через /ingest/events и /ingest/events/upload (туда
    же пишет replay.py), поэтому events=0 значит «нет в журнале сервиса», а не «датчик
    молчал». У прогноза без канала (недельная рекомендация по объекту) все events=0.

    События хранятся в UTC, поэтому сутки режем по полуночи МСК и раскладываем в Python:
    date() в SQL дал бы сутки UTC, и событие в 01:30 МСК ушло бы в предыдущий день.
    """
    start = row.asof - timedelta(days=DYNAMICS_DAYS - 1)
    # [тревоги, плохие состояния, все события]
    counts = {start + timedelta(days=i): [0, 0, 0] for i in range(DYNAMICS_DAYS)}
    if row.channel_id is not None:
        events = db.execute(select(models.Event.ts, models.Event.alarm, models.Event.event_class)
                            .where(models.Event.channel_id == row.channel_id,
                                   models.Event.ts >= to_db(msk_midnight(start)),
                                   models.Event.ts < to_db(msk_midnight(
                                       row.asof + timedelta(days=1)))))
        for ts, alarm, event_class in events:
            day = counts.get(from_db(ts).date())
            if day is not None:
                day[0] += int(bool(alarm))
                day[1] += int(event_class == "fault")
                day[2] += 1
    return [DynamicsPoint(day=day, alarms=alarms, bad_states=bad, events=total)
            for day, (alarms, bad, total) in counts.items()]


def _coverage_note(db: Session, row: models.Forecast) -> str | None:
    run = db.get(models.ForecastRun, row.last_run_id)
    coverage = ((run.raw or {}).get("score") or {}).get("coverage") or [] if run else []
    item = next((c for c in coverage if c.get("head") == row.head), None)
    if item is None:
        return None
    note = (f"Оценено {item['entities_scored']} из {item['entities_total']} "
            f"({item['fraction']:.0%})")
    return f"{note}: {item['reason']}" if item.get("reason") else note


def get_card(db: Session, forecast_id: str) -> ForecastCard | None:
    row = db.get(models.Forecast, forecast_id)
    if row is None:
        return None
    item = _items(db, [row])[0]
    extra = row.extra or {}
    versions = db.scalars(select(models.ForecastVersion)
                          .where(models.ForecastVersion.forecast_id == forecast_id)
                          .order_by(models.ForecastVersion.id))
    decisions = db.scalars(select(models.Decision)
                           .where(models.Decision.forecast_id == forecast_id)
                           .order_by(models.Decision.created_at.desc(),
                                     models.Decision.id.desc()))
    # Календарь — дня, на который прогноз (начало окна по МСК), а не дня расчёта:
    # в карточке он стоит под окном прогноза.
    day = from_db(row.valid_from).date() if row.valid_from else row.asof
    weekday = day.weekday()
    return ForecastCard(
        **item.model_dump(),
        factors=[FactorItem.model_validate(f) for f in row.factors or []],
        maintenance_note=(
            "В графике есть ППР/ТО на период прогноза; связь с объектом "
            "предположительная, проверьте график"
            if isinstance(extra.get("maintenance_context"), dict)
            and extra["maintenance_context"].get("status") == "schedule_overlap_unconfirmed"
            and extra["maintenance_context"].get("matches") else None),
        evidence=extra.get("evidence"),
        recent_alarm_days_7=extra.get("recent_alarm_days_7"),
        recent_alarm_days_30=extra.get("recent_alarm_days_30"),
        coverage_note=_coverage_note(db, row),
        versions=[VersionItem(run_id=v.run_id, risk=v.risk, rank=v.rank,
                              recorded_at=from_db(v.recorded_at)) for v in versions],
        decisions=[decision_out(d) for d in decisions],
        dynamics_30d=_dynamics(db, row),
        # weekday по ISO: 1 — понедельник, 7 — воскресенье
        calendar=CalendarInfo(weekday=weekday + 1, weekday_title=WEEKDAYS_RU[weekday],
                              holiday=("выходной" if weekday >= 5 else
                                       "праздничный день" if (day.month, day.day)
                                       in HOLIDAYS else None)),
    )
