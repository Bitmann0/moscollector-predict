import datetime as dt

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


def test_real_endpoint_preserves_unknown_and_request_order(monkeypatch):
    monkeypatch.setattr(outcomes, "_daily", lambda items: ({}, {}, None))
    monkeypatch.setattr(outcomes, "_episode_starts", lambda items: {})
    monkeypatch.setattr(outcomes, "_weekly_data",
                        lambda items: (set(), set(), set(), None))
    body = [
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
