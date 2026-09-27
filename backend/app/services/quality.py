"""Качество прогноза по неделям окна демо.

ЗАГЛУШКА — владелец BE-05 (C2).
Заменить: агрегат по forecasts и outcomes за окно демо (выдано, попало, мимо,
неизвестно, precision по неделям); base_rate и rule_precision — из реестра ML1-08.
Сейчас — 4 синтетические недели перед неделей demo_today, числа детерминированы
сценарием и датой.
Контракт: weekly(db, scenario) -> QualityOut не меняется; тест
tests/test_endpoints_shape.py должен остаться зелёным.
"""
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas.misc import QualityOut, QualityWeek
from . import settings_store

WEEKS = 4


def weekly(db: Session, scenario: str) -> QualityOut:
    today = settings_store.demo_today(db)
    this_monday = today - timedelta(days=today.weekday())
    weeks = []
    for k in range(WEEKS, 0, -1):
        start = this_monday - timedelta(weeks=k)
        end = start + timedelta(days=7)
        rows = list(db.execute(select(models.Outcome.outcome_auto).join(
            models.Forecast, models.Forecast.id == models.Outcome.forecast_id).where(
                models.Forecast.scenario == scenario, models.Forecast.in_budget.is_(True),
                models.Forecast.asof >= start, models.Forecast.asof < end)))
        issued = db.scalar(select(models.Forecast).where(models.Forecast.scenario == scenario,
                           models.Forecast.in_budget.is_(True), models.Forecast.asof >= start,
                           models.Forecast.asof < end).count()) if False else len(rows)
        hit = sum(value == "hit" for value, in rows)
        unknown = sum(value == "unknown" or value is None for value, in rows)
        miss = sum(value == "miss" for value, in rows)
        weeks.append(QualityWeek(week_start=start, issued=issued, hit=hit, miss=miss,
                                 unknown=unknown,
                                 precision=round(hit / (hit + miss), 3) if hit + miss else None))
    return QualityOut(scenario=scenario, weeks=weeks, base_rate=None, rule_precision=None,
                      note="Автоматические исходы СМВУ; unknown не считается промахом.", source="live")
