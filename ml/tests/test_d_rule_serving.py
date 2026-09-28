"""The D evaluation must follow the same Monday calendar as C1."""
import datetime as dt
import math

import polars as pl
from mkl import rule_head
from mkl.config import EQUIPMENT_STYPES
from scripts.eval_d_rule_serving import actionability, replay


def test_replay_uses_monday_refresh_and_carries_issued_history(monkeypatch):
    first, last = dt.date(2026, 6, 23), dt.date(2026, 7, 6)
    days = [first + dt.timedelta(days=i) for i in range((last-first).days+1)]
    features = pl.DataFrame({
        "ch": [1] * len(days), "obj": ["one"] * len(days),
        "day": days, "stype": [min(EQUIPMENT_STYPES)] * len(days),
        "n_bad_w7": [3] * len(days),
        "n_bad": [0] * len(days), "n_alarms": [0] * len(days),
    })
    outcomes = features.select("ch", "day").with_columns(
        pl.lit(1).alias("y"), pl.lit(dt.date(2026, 7, 20)).alias("available_on"))
    events = pl.DataFrame(schema={
        "ch": pl.Int64, "event_day": pl.Date, "last_signal_day": pl.Date,
        "available_on": pl.Date, "onset_observed": pl.Boolean,
        "episode_id": pl.String,
    })
    cfg = {"serving_rule": "n_bad_w7", "budget_per_day": 1,
           "budget_per_object": True, "cooldown_days": 7, "horizon_days": 7}

    def artifact(head, config, day):
        monday = rule_head.refresh_day(day)
        threshold = math.inf if monday == dt.date(2026, 6, 29) else 1.0
        return {"threshold": threshold,
                "rule": "n_bad_w7", "metadata": {"threshold_end": str(monday)},
                "selection": {"feasible": math.isfinite(threshold)}}

    monkeypatch.setattr(rule_head, "artifact", artifact)
    result = replay(features, outcomes, events, cfg, first, last,
                    dt.date(2026, 7, 20), fold_days=7)
    assert [row["refresh_day"] for row in result["refreshes"]] == [
        "2026-06-22", "2026-06-29", "2026-07-06"]
    assert result["summary"]["alerts"] == 2
    assert [fold["summary"]["alerts"] for fold in result["folds"]] == [1, 1]


def test_actionability_does_not_count_a_continuing_bad_signal_as_new_onset():
    d = dt.date(2026, 5, 13)
    issued = pl.DataFrame({
        "ch": [1, 2, 3], "day": [d] * 3, "alert": [True] * 3,
        "n_bad": [2, 0, None], "n_alarms": [0, 0, None],
        "y": [1, 1, None],
    })
    events = pl.DataFrame({
        "ch": [1, 2], "event_day": [d - dt.timedelta(days=1),
                                    d + dt.timedelta(days=2)],
        "episode_id": ["ongoing", "new"], "onset_observed": [True, True],
    })
    result = actionability(issued, events, d, d, 7)
    assert result["current_bad"] == 1
    assert result["current_clean"] == 1
    assert result["current_unknown"] == 1
    assert result["first_alert_for_observed_new_episode"] == 1
    assert result["new_episode_precision_lower_bound"] == 1 / 3
    assert result["current_bad_hits"] == 1
    assert result["current_clean_new_onsets"] == 1


def test_clean_only_gate_does_not_cool_down_a_suppressed_bad_day(monkeypatch):
    first = dt.date(2026, 6, 22)
    days = [first, first + dt.timedelta(days=1)]
    features = pl.DataFrame({
        "ch": [1, 1], "obj": ["one", "one"], "day": days,
        "stype": [min(EQUIPMENT_STYPES)] * 2,
        "n_bad_w7": [3, 3], "n_bad": [1, 0], "n_alarms": [0, 0],
    })
    outcomes = features.select("ch", "day").with_columns(
        pl.lit(1).alias("y"), pl.lit(dt.date(2026, 7, 1)).alias("available_on"))
    events = pl.DataFrame(schema={
        "ch": pl.Int64, "event_day": pl.Date, "last_signal_day": pl.Date,
        "available_on": pl.Date, "onset_observed": pl.Boolean,
        "episode_id": pl.String,
    })
    monkeypatch.setattr(rule_head, "artifact", lambda *a: {
        "threshold": 1.0, "rule": "n_bad_w7",
        "metadata": {"threshold_end": "2026-06-14"},
        "selection": {"feasible": True}})
    cfg = {"serving_rule": "n_bad_w7", "budget_per_day": 1,
           "budget_per_object": True, "cooldown_days": 7, "horizon_days": 7}
    base = replay(features, outcomes, events, cfg, days[0], days[1],
                  dt.date(2026, 7, 1))
    clean = replay(features, outcomes, events, cfg, days[0], days[1],
                   dt.date(2026, 7, 1), clean_only=True)
    assert base["actionability"]["current_bad"] == 1
    assert clean["actionability"]["current_clean"] == 1
    assert clean["actionability"]["current_bad"] == 0
