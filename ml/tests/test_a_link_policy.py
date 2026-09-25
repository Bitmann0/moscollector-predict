import datetime as dt

import polars as pl
import pytest

from mkl import contract, serve, service
from scripts.eval_a_link_policy import _summary_unknown


def test_unknown_link_outcome_still_consumes_live_budget():
    frame = pl.DataFrame({
        "ch": [1, 2], "obj": ["A", "B"],
        "day": [dt.date(2026, 6, 1)] * 2,
        "risk": [0.9, 0.8], "y": [None, 1],
    }, schema={"ch": pl.Int64, "obj": pl.String, "day": pl.Date,
               "risk": pl.Float64, "y": pl.Int64})
    result = _summary_unknown(frame, None, {
        "budget_per_day": 1, "horizon_days": 1})
    assert result["alerts"] == 1
    assert result["unknown_alerts"] == 1
    assert result["hits"] == 0
    assert result["precision_lower_bound"] == 0


def test_a_link_serving_requires_its_own_issued_history(monkeypatch):
    day = dt.date(2026, 7, 10)
    ranked = pl.DataFrame({"ch": [1], "obj": ["A"], "day": [day],
                           "risk": [0.8], "alert": [True],
                           "above_thr": [True]})
    cfg = {"A_link": {"direction": "sensor_failure", "title": "link",
                      "entity": ["ch", "day"], "horizon_days": 1,
                      "label": "label_link", "variant": "L9c",
                      "operating_min_precision": 0.5,
                      "unknown_in_budget": True,
                      "cooldown_days": 7, "product_status": "pilot"}}
    art = {"threshold": 0.5, "metadata": {
        "head": "A_link", "label": "label_link", "variant": "L9c",
        "horizon_days": 1, "operating_min_precision": 0.5,
        "unknown_in_budget": True,
        "threshold_end": "2026-07-09"}}
    monkeypatch.setattr(serve, "load_heads", lambda: cfg)
    monkeypatch.setattr(serve, "score_with_internals",
                        lambda head, asof: (ranked, art, ranked))
    monkeypatch.setattr(service, "_address", lambda row: contract.Address(
        obj=row["obj"], channel=row["ch"]))
    with pytest.raises(ValueError, match="complete issued-recommendation"):
        service.alerts_for_head("A_link", day, with_factors=False)
    got = service.alerts_for_head(
        "A_link", day, with_factors=False,
        issued_history=[(1, day-dt.timedelta(days=1))],
        history_complete_from=day-dt.timedelta(days=7))
    assert len(got) == 1 and not got[0].in_budget


def test_combined_pilot_histories_are_scoped_by_head(monkeypatch):
    calls = {}
    monkeypatch.setattr(serve, "load_heads", lambda: {
        "D": {"product_status": "pilot", "cooldown_days": 7},
        "A_link": {"product_status": "pilot", "cooldown_days": 7}})
    def fake(head, asof, **kwargs):
        calls[head] = kwargs["issued_history"]
        return []
    monkeypatch.setattr(service, "alerts_for_head", fake)
    day = dt.date(2026, 7, 10)
    service.daily_alerts(day, with_factors=False,
                         issued_histories={"D": [(1, day)],
                                           "A_link": [(2, day)]},
                         history_complete_from=day-dt.timedelta(days=7))
    assert calls == {"D": [(1, day)], "A_link": [(2, day)]}
