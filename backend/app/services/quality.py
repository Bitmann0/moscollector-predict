"""Качество выданных прогнозов по неделям демонстрационного окна.

Базовая частота и точность простого правила — не из БД, а из реестра метрик ML:
contracts/quality_reference.json, копия блока quality_screen
ml/reports/SUBMISSION_METRICS.json (совпадение проверяет тест).
"""
import json
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from ..schemas.misc import QualityOut, QualityWeek
from . import settings_store

WEEKS = 4


@lru_cache
def reference() -> dict:
    path = get_settings().contracts_dir / "quality_reference.json"
    return json.loads(path.read_text(encoding="utf-8"))["scenario"]


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
    ref = reference().get(scenario, {})
    return QualityOut(scenario=scenario, weeks=weeks, base_rate=ref.get("base_rate"),
                      rule_precision=ref.get("rule_precision"),
                      reference_period=ref.get("period"), reference_source=ref.get("source"),
                      reference_note=ref.get("note"),
                      note="Автоматические исходы СМВУ; unknown не считается промахом.", source="live")
