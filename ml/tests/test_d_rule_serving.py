"""The D evaluation must follow the same Monday calendar as C1."""
import datetime as dt
import math

import polars as pl

from mkl import rule_head
from mkl.config import EQUIPMENT_STYPES
from scripts.eval_d_rule_serving import replay


def test_replay_uses_monday_refresh_and_carries_issued_history(monkeypatch):
    first, last = dt.date(2026, 6, 23), dt.date(2026, 7, 6)
    days = [first + dt.timedelta(days=i) for i in range((last-first).days+1)]
    features = pl.DataFrame({
        "ch": [1] * len(days), "obj": ["one"] * len(days),
        "day": days, "stype": [sorted(EQUIPMENT_STYPES)[0]] * len(days),
        "n_bad_w7": [3] * len(days),
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
