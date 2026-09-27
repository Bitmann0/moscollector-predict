"""Качество выданных прогнозов по неделям демонстрационного окна."""
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
        rows = list(db.execute(select(models.Forecast.id, models.Outcome.outcome_auto)
            .outerjoin(models.Outcome, models.Forecast.id == models.Outcome.forecast_id).where(
                models.Forecast.scenario == scenario, models.Forecast.in_budget.is_(True),
                models.Forecast.asof >= start, models.Forecast.asof < end)))
        issued = len(rows)
        hit = sum(value == "hit" for _, value in rows)
        unknown = sum(value == "unknown" or value is None for _, value in rows)
        miss = sum(value == "miss" for _, value in rows)
        weeks.append(QualityWeek(week_start=start, issued=issued, hit=hit, miss=miss,
                                 unknown=unknown,
                                 precision=round(hit / (hit + miss), 3) if hit + miss else None))
    return QualityOut(scenario=scenario, weeks=weeks, base_rate=None, rule_precision=None,
                      note="Автоматические исходы СМВУ; unknown не считается промахом.", source="live")
