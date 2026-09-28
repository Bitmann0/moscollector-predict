"""Фильтруемый журнал прогнозов и карточка с историей, событиями и решениями."""
from datetime import date, timedelta

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
    VersionItem,
)
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


def list_forecasts(db: Session, *, scenario: str | None, date_from: date | None,
                   date_to: date | None, decision: str | None, outcome: str | None,
                   obj: str | None, group_by: str | None, page: int,
                   page_size: int) -> Page[ForecastItem]:
    stmt = select(models.Forecast).where(models.Forecast.in_budget.is_(True))
    if scenario is not None:
        stmt = stmt.where(models.Forecast.scenario == scenario)
    if date_from is not None:
        stmt = stmt.where(models.Forecast.asof >= date_from)
    if date_to is not None:
        stmt = stmt.where(models.Forecast.asof <= date_to)
    if obj is not None:
        stmt = stmt.where(models.Forecast.obj_id == obj)
    if decision == "none":
        stmt = stmt.where(~models.Forecast.id.in_(select(models.Decision.forecast_id)))
    elif decision == "any":
        stmt = stmt.where(models.Forecast.id.in_(select(models.Decision.forecast_id)))
    elif decision is not None:
        # Фильтр по последнему решению — тому, что строка журнала показывает в колонке.
        # Решения пишутся по порядку, поэтому последнее — с наибольшим id.
        latest = select(func.max(models.Decision.id)).group_by(models.Decision.forecast_id)
        stmt = stmt.where(models.Forecast.id.in_(select(models.Decision.forecast_id).where(
            models.Decision.id.in_(latest), models.Decision.action == decision)))
    if outcome is not None:
        stmt = stmt.where(models.Forecast.id.in_(select(models.Outcome.forecast_id).where(
            (models.Outcome.outcome_auto == outcome) | (models.Outcome.outcome_manual == outcome))))
    ordered = stmt.order_by(models.Forecast.asof.desc(), models.Forecast.rank,
                            models.Forecast.id)
    if group_by:
        grouped: list[models.Forecast] = []
        seen: set[str | None] = set()
        for row in db.scalars(ordered):
            key = row.obj_id if group_by == "obj" else row.case_key
            if key not in seen:
                grouped.append(row)
                seen.add(key)
        total = len(grouped)
        rows = grouped[(page - 1) * page_size:page * page_size]
    else:
        total = count(db, stmt)
        rows = list(db.scalars(ordered.offset((page - 1) * page_size).limit(page_size)))
    return page_of(ForecastItem, _items(db, rows), total, page, page_size)


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
