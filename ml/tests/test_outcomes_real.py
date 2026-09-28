import dataclasses
import datetime as dt

import polars as pl
from fastapi.testclient import TestClient

from mkl import outcomes
from mkl.product_api import create_app
from mkl.product_contract import OutcomeQuery


D = dt.date


def test_link_waits_for_next_report_before_deciding_silence():
    reports = [D(2026, 5, day) for day in range(1, 11)]
    assert outcomes.link_outcome(reports, D(2026, 5, 10), D(2026, 5, 11)) == "unknown"
    assert outcomes.link_outcome(reports + [D(2026, 5, 11)],
                                 D(2026, 5, 10), D(2026, 5, 11)) == "miss"
    assert outcomes.link_outcome(reports + [D(2026, 5, 13)],
                                 D(2026, 5, 10), D(2026, 5, 13)) == "hit"
    # A channel which normally reports every three days should not produce
    # an L9c hit after an ordinary three-day pause.
    sparse = [D(2026, 5, day) for day in (1, 4, 7, 10, 13, 16, 19, 22)]
    assert outcomes.link_outcome(sparse + [D(2026, 5, 25)],
                                 D(2026, 5, 22), D(2026, 5, 25)) == "miss"


def test_wear_negative_requires_each_future_channel_day():
    day = D(2026, 6, 1)
    observed = {day + dt.timedelta(days=i): (0, 0) for i in range(1, 8)}
    assert outcomes.wear_outcome(observed, set(), day, D(2026, 6, 8)) == "miss"
    del observed[day + dt.timedelta(days=4)]
    assert outcomes.wear_outcome(observed, set(), day, D(2026, 6, 8)) == "unknown"
    observed[day + dt.timedelta(days=3)] = (1, 0)
    assert outcomes.wear_outcome(observed, set(), day, D(2026, 6, 8)) == "hit"
    assert outcomes.wear_outcome({}, {day + dt.timedelta(days=2)},
                                 day, D(2026, 6, 2)) == "hit"


def test_weekly_positive_wins_but_missing_future_day_is_unknown():
    day = D(2026, 6, 1)
    target = [("obj", day + dt.timedelta(days=i)) for i in range(2, 9)]
    assert outcomes.weekly_outcome("obj", day, set(), set(),
                                   set(target), D(2026, 6, 9)) == "miss"
    assert outcomes.weekly_outcome("obj", day, set(), set(),
                                   set(target[:-1]), D(2026, 6, 9)) == "unknown"
    assert outcomes.weekly_outcome("obj", day, {target[0]}, set(),
                                   set(), D(2026, 6, 3)) == "hit"


def test_next_day_outcome_needs_data_for_the_target_day():
    day = D(2026, 6, 10)
    assert outcomes.next_day_outcome({D(2026, 6, 11): True}, day, D(2026, 6, 30)) == "hit"
    assert outcomes.next_day_outcome({D(2026, 6, 11): False}, day, D(2026, 6, 30)) == "miss"
    # Нет строк сущности за целевые сутки — событие не наблюдалось.
    assert outcomes.next_day_outcome({D(2026, 6, 10): True}, day, D(2026, 6, 30)) == "unknown"
    assert outcomes.next_day_outcome({D(2026, 6, 11): True}, day, D(2026, 6, 10)) == "unknown"
    # Исключённый период миграции СМВУ.
    assert outcomes.next_day_outcome({D(2021, 5, 2): True}, D(2021, 5, 1),
                                     D(2026, 6, 30)) == "unknown"


def test_fire_and_flood_outcomes_from_panel(monkeypatch, tmp_path):
    rows = pl.DataFrame({
        "ch": [1, 2, 3, 4, 5],
        "obj": ["A", "A", "A", "P", "P"],
        "picket": [12.0, 18.0, 55.0, None, None],
        "day": [D(2026, 6, 11)] * 3 + [D(2026, 6, 11), D(2026, 6, 12)],
        "n_fire": [0, 2, 0, 0, 0],
        "n_flood": [0, 0, 0, 1, 0],
    })
    rows.write_parquet(tmp_path / "daily_channel.parquet")
    monkeypatch.setattr(outcomes, "PATHS", dataclasses.replace(outcomes.PATHS, interim=tmp_path))
    items = [
        OutcomeQuery(id="B-hit", kind="alert", head="B", obj="A", segment=1,
                     asof=D(2026, 6, 10)),
        OutcomeQuery(id="B-miss", kind="alert", head="B", obj="A", segment=5,
                     asof=D(2026, 6, 10)),
        OutcomeQuery(id="B-nodata", kind="alert", head="B", obj="A", segment=9,
                     asof=D(2026, 6, 10)),
        OutcomeQuery(id="B-noseg", kind="alert", head="B", obj="A", asof=D(2026, 6, 10)),
        OutcomeQuery(id="E-hit", kind="alert", head="E", obj="P", asof=D(2026, 6, 10)),
        OutcomeQuery(id="E-miss", kind="alert", head="E", obj="P", asof=D(2026, 6, 11)),
        OutcomeQuery(id="E-future", kind="alert", head="E", obj="P", asof=D(2026, 6, 12)),
    ]
    got = {r.id: r.outcome for r in outcomes.resolve(items)}
    assert got == {"B-hit": "hit", "B-miss": "miss", "B-nodata": "unknown",
                   "B-noseg": "unknown", "E-hit": "hit", "E-miss": "miss",
                   "E-future": "unknown"}


def test_real_endpoint_preserves_unknown_and_request_order(monkeypatch):
    monkeypatch.setattr(outcomes, "_daily", lambda items: ({}, {}, None))
    monkeypatch.setattr(outcomes, "_episode_starts", lambda items: {})
    monkeypatch.setattr(outcomes, "_weekly_data",
                        lambda items: (set(), set(), set(), None))
    monkeypatch.setattr(outcomes, "_object_days", lambda items: ({}, None))
    body = [
        OutcomeQuery(id="B-1", kind="alert", head="B", obj="obj", segment=1,
                     asof=D(2026, 6, 30)),
        OutcomeQuery(id="E-1", kind="alert", head="E", obj="obj", asof=D(2026, 6, 30)),
        OutcomeQuery(id="D-1", kind="alert", head="D", channel=1,
                     asof=D(2026, 6, 30)),
        OutcomeQuery(id="A-1", kind="alert", head="A_link", channel=2,
                     asof=D(2026, 6, 30)),
        OutcomeQuery(id="G-1", kind="weekly_recommendation",
                     head="guard_weekly", obj="obj", asof=D(2026, 6, 29)),
    ]
    response = TestClient(create_app("real")).post(
        "/api/v1/outcomes", json=[item.model_dump(mode="json") for item in body])
    assert response.status_code == 200, response.text
    assert response.json() == [{"id": item.id, "outcome": "unknown"} for item in body]
