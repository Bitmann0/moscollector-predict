"""D recommendations must follow the same policy in evaluation and serving."""
import datetime as dt

import polars as pl
import pytest

from mkl import contract, serve, service


def test_issued_cooldown_uses_calendar_days_and_does_not_backfill():
    day = dt.date(2026, 7, 10)
    ranked = pl.DataFrame({"ch": [1, 2, 3], "risk": [0.9, 0.8, 0.7],
                           "alert": [True, True, False]})
    out = serve.apply_issued_cooldown(
        ranked, "ch", day,
        [(1, day - dt.timedelta(days=7)),
         (2, day - dt.timedelta(days=8))])
    assert out["alert"].to_list() == [False, True, False]
    assert serve.apply_issued_cooldown(
        ranked, "ch", day, [(1, day)])["alert"].to_list() == [True, True, False]


def test_backtest_cooldown_is_calendar_based_and_thresholded():
    days = [dt.date(2026, 1, 1), dt.date(2026, 1, 10)]
    df = pl.DataFrame({"ch": [1, 2, 1, 2], "obj": ["A", "B", "A", "B"],
                       "day": [days[0], days[0], days[1], days[1]],
                       "risk": [0.9, 0.5, 0.9, 0.5]})
    got = serve.alerts_over_time(df, 1, "ch", cooldown_days=7,
                                 threshold=0.8)
    assert got.filter(pl.col("alert"))["ch"].to_list() == [1, 1]


def test_d_serving_requires_complete_issued_history(monkeypatch):
    day = dt.date(2026, 7, 10)
    cfg = {"D": {"direction": "infrastructure_wear", "title": "D",
                 "horizon_days": 7}}
    ranked = pl.DataFrame({"ch": [1], "obj": ["A"], "day": [day],
                           "risk": [0.9], "alert": [True],
                           "above_thr": [True]})
    monkeypatch.setattr(serve, "load_heads", lambda: cfg)
    monkeypatch.setattr(serve, "score_with_internals",
                        lambda head, asof: (ranked, {}, ranked))
    with pytest.raises(ValueError, match="complete issued-recommendation"):
        service.alerts_for_head("D", day, with_factors=False)
    with pytest.raises(ValueError, match="complete issued-recommendation"):
        service.daily_alerts(day, heads=["D"], with_factors=False)
    with pytest.raises(ValueError, match="complete issued-recommendation"):
        service.alerts_for_head(
            "D", day, with_factors=False, issued_history=[],
            history_complete_from=day - dt.timedelta(days=6))
    monkeypatch.setattr(service, "_address", lambda row: contract.Address(
        obj=row["obj"], channel=row["ch"]))
    got = service.alerts_for_head(
        "D", day, with_factors=False,
        issued_history=[(1, day - dt.timedelta(days=1))],
        history_complete_from=day - dt.timedelta(days=7))
    assert len(got) == 1 and not got[0].in_budget


def test_default_service_excludes_unapproved_heads_and_d_without_history(monkeypatch):
    monkeypatch.setattr(serve, "load_heads", lambda: {
        "D": {"product_status": "pilot"},
        "B": {}, "A_strict": {"product_status": "deferred"}})
    monkeypatch.setattr(serve, "model_path", lambda head: type("P", (), {
        "exists": lambda self: True})())
    assert service.daily_alerts(with_factors=False) == []
