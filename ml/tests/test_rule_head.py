"""Голова-правило D: порог по окну перед днём расчёта, молчание без осуществимого порога."""
import datetime as dt
import math

import polars as pl

from mkl import rule_head
from mkl.config import EQUIPMENT_STYPES

DAY = dt.date(2026, 6, 30)
CFG = {"serving_rule": "n_bad_w7", "budget_per_day": 3, "budget_per_object": True,
       "operating_min_precision": 0.7, "operating_min_alerts": 30, "cooldown_days": 7,
       "horizon_days": 7, "embargo_days": 37, "label": "label_wear"}
STYPE = sorted(EQUIPMENT_STYPES)[0]


def _window(positive: bool) -> tuple[pl.DataFrame, pl.DataFrame]:
    """30 суток окна порога: 20 каналов с неисправностями и 20 чистых."""
    days = [dt.date(2026, 5, 23) + dt.timedelta(days=i) for i in range(30)]
    feats, outcomes = [], []
    for ch in range(40):
        bad = ch < 20
        for i, day in enumerate(days):
            # Лидеры меняются день ото дня: иначе пауза гасит повторы тех же
            # трёх каналов, а добора аудит не делает.
            feats.append({"ch": ch, "obj": str(ch % 10), "day": day, "stype": STYPE,
                          "n_bad_w7": float(3 + (ch * 7 + i) % 20) if bad else 0.0})
            outcomes.append({"ch": ch, "day": day, "y": int(bad and positive),
                             "available_on": day + dt.timedelta(days=7)})
    return pl.DataFrame(feats), pl.DataFrame(outcomes)


def test_rule_threshold_is_chosen_on_known_outcomes():
    feats, outcomes = _window(positive=True)
    pick = rule_head.select("D", CFG, DAY, feats, outcomes)
    assert pick["feasible"] is True
    assert pick["threshold"] >= 3.0
    assert pick["alerts"] >= 30 and pick["precision_lower_bound"] == 1.0


def test_rule_without_feasible_threshold_is_silent():
    feats, outcomes = _window(positive=False)
    pick = rule_head.select("D", CFG, DAY, feats, outcomes)
    assert pick["feasible"] is False
    assert rule_head.feasible({"rule": "n_bad_w7", "threshold": math.inf}) is False


def test_outcomes_after_previous_day_are_not_used():
    """Исход, который станет известен только в день расчёта, в выбор не попадает."""
    feats, outcomes = _window(positive=True)
    late = outcomes.with_columns(pl.lit(DAY).alias("available_on"))
    pick = rule_head.select("D", CFG, DAY, feats, late)
    assert pick["feasible"] is False  # все исходы неизвестны к 29.06


def test_feasibility_differs_for_rule_and_model():
    assert rule_head.feasible({"rule": "n_bad_w7", "threshold": 5.0}) is True
    assert rule_head.feasible({"threshold": 5.0}) is False  # модель: вероятность > 1
    assert rule_head.feasible({"threshold": 0.4}) is True
    assert rule_head.feasible({"threshold": None}) is False


def test_threshold_is_refreshed_weekly_on_monday():
    assert rule_head.refresh_day(dt.date(2026, 6, 29)) == dt.date(2026, 6, 29)
    assert rule_head.refresh_day(dt.date(2026, 7, 5)) == dt.date(2026, 6, 29)
    assert rule_head.refresh_day(dt.date(2026, 6, 28)) == dt.date(2026, 6, 22)
