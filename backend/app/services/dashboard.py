"""Сводка для главного экрана.

ЗАГЛУШКА — владелец BE-05 (C2).
Заменить: ряды series_forecasts_per_day и series_coverage_per_day — сейчас синтетика за
14 дней до demo_today, нужны агрегаты по forecasts и forecast_runs; «открытый прогноз» —
сейчас «в бюджете и без решения», нужно правило BE-05 с учётом окна valid_to;
planned_like_alarms_24h — сейчас тревоги с непустой подсказкой, нужна классификация C5.
Контракт: summary(db) -> DashboardSummary не меняется; тест
tests/test_endpoints_shape.py должен остаться зелёным.

Живые части: счётчики прогнозов, заявок и событий из БД, охват из последнего прогона,
статус голов (services/system.head_states).
"""
import hashlib
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, vocab
from ..schemas.dashboard import DashboardSummary, ScenarioKpi, SeriesPoint
from . import settings_store
from .helpers import msk_midnight, to_db
from .system import head_states

SERIES_DAYS = 14


def _synthetic(name: str, day, low: float, high: float) -> float:
    digest = hashlib.sha256(f"{name}|{day.isoformat()}".encode()).digest()
    return round(low + (high - low) * digest[0] / 255, 3)


def _coverage_by_head(db: Session) -> dict[str, float]:
    run = db.scalars(select(models.ForecastRun).where(models.ForecastRun.kind == "daily")
                     .order_by(models.ForecastRun.id.desc()).limit(1)).first()
    coverage = ((run.raw or {}).get("score") or {}).get("coverage") or [] if run else []
    return {c["head"]: c["fraction"] for c in coverage if "head" in c and "fraction" in c}


def summary(db: Session) -> DashboardSummary:
    today = settings_store.demo_today(db)
    coverage = _coverage_by_head(db)
    decided = select(models.Decision.forecast_id)
    scenarios = []
    for s in vocab.load()["scenario"]:
        open_count = db.scalar(select(func.count()).select_from(models.Forecast).where(
            models.Forecast.scenario == s["code"], models.Forecast.in_budget.is_(True),
            models.Forecast.id.not_in(decided))) or 0
        scenarios.append(ScenarioKpi(scenario=s["code"], title=s["title"],
                                     open_forecasts=open_count,
                                     coverage_fraction=coverage.get(s["head"])))
    by_status = dict.fromkeys(vocab.codes("work_order_status"), 0)
    for status, n in db.execute(select(models.WorkOrder.status, func.count())
                                .group_by(models.WorkOrder.status)):
        by_status[status] = n
    day_start, day_end = to_db(msk_midnight(today)), to_db(msk_midnight(today + timedelta(1)))
    alarms = select(func.count()).select_from(models.Event).where(
        models.Event.alarm.is_(True), models.Event.ts >= day_start, models.Event.ts < day_end)
    days = [today - timedelta(days=i) for i in range(SERIES_DAYS - 1, -1, -1)]
    return DashboardSummary(
        demo_today=today,
        scenarios=scenarios,
        work_orders_by_status=by_status,
        alarms_24h=db.scalar(alarms) or 0,
        planned_like_alarms_24h=db.scalar(alarms.where(models.Event.hint.is_not(None))) or 0,
        heads=head_states(db),
        series_forecasts_per_day=[
            SeriesPoint(day=d, value=round(_synthetic("forecasts", d, 5, 25))) for d in days],
        series_coverage_per_day=[
            SeriesPoint(day=d, value=_synthetic("coverage", d, 0.8, 0.97)) for d in days],
        source="stub",
    )
