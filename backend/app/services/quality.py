"""Качество выданных прогнозов по неделям демонстрационного окна.

Базовая частота и точность простого правила — не из БД, а из реестра метрик ML:
contracts/quality_reference.json, копия блока quality_screen
ml/reports/SUBMISSION_METRICS.json (совпадение проверяет тест).
"""
import json
from collections.abc import Iterable
from datetime import date, timedelta
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


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def tally(outcomes: Iterable[str | None]) -> dict[str, int]:
    """Автоматические исходы прогнозов → выдано, попало, промахов, неизвестно.

    Одно правило на экран «Качество» и итог журнала. Прогноз без факта (окно не закрыто
    или ML ещё не ответил) — неизвестный, а не промах.
    """
    values = list(outcomes)
    return {"issued": len(values), "hit": values.count("hit"), "miss": values.count("miss"),
            "unknown": sum(value == "unknown" or value is None for value in values)}


def weekly(db: Session, scenario: str) -> QualityOut:
    this_monday = monday(settings_store.demo_today(db))
    weeks = []
    for k in range(WEEKS, 0, -1):
        start = this_monday - timedelta(weeks=k)
        end = start + timedelta(days=7)
        counts = tally(db.scalars(select(models.Outcome.outcome_auto).select_from(models.Forecast)
            .outerjoin(models.Outcome, models.Forecast.id == models.Outcome.forecast_id).where(
                models.Forecast.scenario == scenario, models.Forecast.in_budget.is_(True),
                models.Forecast.asof >= start, models.Forecast.asof < end)))
        hit, miss = counts["hit"], counts["miss"]
        weeks.append(QualityWeek(week_start=start, **counts,
                                 precision=round(hit / (hit + miss), 3) if hit + miss else None))
    ref = reference().get(scenario, {})
    return QualityOut(scenario=scenario, weeks=weeks, base_rate=ref.get("base_rate"),
                      rule_precision=ref.get("rule_precision"),
                      reference_period=ref.get("period"), reference_source=ref.get("source"),
                      reference_note=ref.get("note"),
                      note="Исходы определены автоматически по журналу СМВУ; «неизвестно» не считается промахом.", source="live")
