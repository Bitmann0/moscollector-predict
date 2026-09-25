"""Журнал прогнозов и карточка прогноза. Живое, минимум.

ЗАГЛУШКА части — владелец BE-05 (C2).
Заменить: фильтры from, to, decision, outcome, obj и группировку group_by — сейчас они
принимаются и не применяются, работают только scenario и пагинация; dynamics_30d —
сейчас 30 синтетических точек, нужны тревоги и плохие состояния по дням из events;
calendar.holiday — производственный календарь; поиск заявки по forecast_ids — сейчас
перебор всех заявок в Python, нужен запрос по JSON или связующая таблица.
Контракт: list_forecasts и get_card не меняются; тесты tests/test_endpoints_shape.py
и tests/test_daily_run.py должны остаться зелёными.

Живые части: строки из forecasts, объект и канал из address и справочников, последнее
решение, исходы, номер заявки, версии и решения в карточке. Порядок журнала: asof по
убыванию, rank по возрастанию.

Журнал показывает только выданное (in_budget): ML возвращает и строки вне бюджета
(заглушка — около 40 в день при 1–20 в бюджете), они хранятся в forecasts для анализа,
но в ForecastItem нет поля in_budget, и в списке их нельзя было бы отличить от выданных.
Карточка открывается для любой строки.
"""
import hashlib
from datetime import date, timedelta

from sqlalchemy import select
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
from .helpers import Refs, count, from_db, page_of

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота",
               "воскресенье"]
DYNAMICS_DAYS = 30


def decision_out(row: models.Decision) -> DecisionOut:
    return DecisionOut(id=row.id, action=row.action, reason_code=row.reason_code,
                       comment=row.comment, author=row.author,
                       created_at=from_db(row.created_at), source=row.source)


def _work_order_ids(db: Session) -> dict[str, str]:
    """forecast_id → id заявки. Заявок в демо десятки, поэтому читаем все."""
    out: dict[str, str] = {}
    for order_id, forecast_ids in db.execute(
            select(models.WorkOrder.id, models.WorkOrder.forecast_ids)
            .order_by(models.WorkOrder.created_at)):
        for forecast_id in forecast_ids or []:
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
    orders = _work_order_ids(db)
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
    total = count(db, stmt)
    rows = list(db.scalars(stmt.order_by(models.Forecast.asof.desc(), models.Forecast.rank,
                                         models.Forecast.id)
                           .offset((page - 1) * page_size).limit(page_size)))
    return page_of(ForecastItem, _items(db, rows), total, page, page_size)


def _dynamics(row: models.Forecast) -> list[DynamicsPoint]:
    points = []
    for i in range(DYNAMICS_DAYS - 1, -1, -1):
        day = row.asof - timedelta(days=i)
        digest = hashlib.sha256(f"{row.id}|{day.isoformat()}".encode()).digest()
        points.append(DynamicsPoint(day=day, alarms=digest[0] % 6, bad_states=digest[1] % 4))
    return points


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
    weekday = row.asof.weekday()
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
        dynamics_30d=_dynamics(row),
        # weekday по ISO: 1 — понедельник, 7 — воскресенье
        calendar=CalendarInfo(weekday=weekday + 1, weekday_title=WEEKDAYS_RU[weekday],
                              holiday=None),
    )
