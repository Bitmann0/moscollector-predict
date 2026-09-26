"""Real C1 orchestration stays honest when one pilot head cannot score."""
import datetime as dt
from types import SimpleNamespace

import polars as pl
from fastapi.testclient import TestClient

from mkl import config, contract, product_api, service
from mkl.product_api import create_app
from mkl.product_contract import ScoreResponse


DAY = dt.date(2026, 6, 30)


def _alert() -> contract.Alert:
    address = contract.Address(obj="100", obj_parent="10", channel=1,
                               obj_name="Насосная", obj_parent_name="Комплекс",
                               sensor_type="Состояние насоса")
    return contract.Alert(
        alert_id="alert-1", case_key="case-1", schema_version="1.0",
        head="A_link", direction="sensor_failure",
        direction_title="Отказ датчика", title="Потеря связи с каналом",
        asof=DAY, valid_from=dt.datetime(2026, 7, 1),
        valid_to=dt.datetime(2026, 7, 2), horizon_hours=24,
        risk=0.8, rank=1, in_budget=True, above_threshold=True,
        address=address, factors=[{"feature": "silence_z",
                                  "label": "необычно долгое молчание",
                                  "contribution": 0.4}])


def test_real_score_isolates_failed_head_and_keeps_real_coverage(monkeypatch, tmp_path):
    interim = tmp_path / "interim"
    interim.mkdir()
    pl.DataFrame({"ch": [1, 2], "stype": ["Состояние насоса", "Газовый датчик"]
                  }).write_parquet(interim / "channels.parquet")
    monkeypatch.setattr(config, "PATHS", SimpleNamespace(interim=interim))
    monkeypatch.setattr(product_api, "_last_feature_day", lambda: DAY)
    monkeypatch.setattr(product_api, "_has_feature_day", lambda day: True)
    monkeypatch.setattr(product_api, "_pilot_artifact", lambda head, day: {
        "metadata": {"threshold_end": "2026-06-23"},
        "saved_at": "version-1", "threshold": 0.5})

    def score_head(head, *args, **kwargs):
        if head == "D":
            raise ValueError("D: pilot target changed since training")
        assert kwargs["issued_history"] == [(1, dt.date(2026, 6, 29))]
        assert kwargs["history_complete_from"] == dt.date(2026, 6, 23)
        return [_alert()]

    monkeypatch.setattr(service, "alerts_for_head", score_head)
    client = TestClient(create_app("real"))
    resp = client.post("/api/v1/score", json={
        "asof": DAY.isoformat(),
        "issued_histories": {"A_link": [{"channel": 1, "sent_day": "2026-06-29"}],
                             "D": []},
        "history_complete_from": "2026-06-23"})
    assert resp.status_code == 200, resp.text
    result = ScoreResponse.model_validate(resp.json())
    assert result.source == "live"
    assert result.heads["A_link"].result_status == "ok"
    assert result.heads["D"].result_status == "stale"
    assert [(c.head, c.entities_total, c.entities_scored) for c in result.coverage] == [
        ("A_link", 2, 1), ("D", 1, 0)]
    assert len(result.alerts) == 1
    assert len(result.work_orders) == 1
    assert result.work_orders[0].obj_name == "Насосная"
    assert result.work_orders[0].obj_parent_name == "Комплекс"


def test_ready_does_not_claim_day_without_features(monkeypatch, tmp_path):
    interim = tmp_path / "interim"
    interim.mkdir()
    (interim / "channels.parquet").touch()
    monkeypatch.setattr(config, "PATHS", SimpleNamespace(interim=interim))
    monkeypatch.setattr(product_api, "_last_feature_day", lambda: DAY)
    monkeypatch.setattr(product_api, "_has_feature_day", lambda day: False)
    resp = TestClient(create_app("real")).get("/ready", params={"asof": DAY})
    assert resp.status_code == 200
    assert resp.json()["status"] == "missing_data"
    assert resp.json()["source"] == "live"


def test_infeasible_threshold_is_reported(monkeypatch, tmp_path):
    """train_latest.py stores nextafter(1.0) when no threshold meets the gate."""
    import numpy as np

    interim = tmp_path / "interim"
    interim.mkdir()
    pl.DataFrame({"ch": [1], "stype": ["Газовый датчик"]}).write_parquet(
        interim / "channels.parquet")
    monkeypatch.setattr(config, "PATHS", SimpleNamespace(interim=interim))
    monkeypatch.setattr(product_api, "_last_feature_day", lambda: DAY)
    monkeypatch.setattr(product_api, "_has_feature_day", lambda day: True)
    monkeypatch.setattr(product_api, "_pilot_artifact", lambda head, day: {
        "metadata": {"threshold_end": "2026-06-23"}, "saved_at": "version-1",
        "threshold": float(np.nextafter(1.0, np.inf))})
    monkeypatch.setattr(service, "alerts_for_head", lambda head, *a, **kw: [])
    resp = TestClient(create_app("real")).post("/api/v1/score", json={
        "asof": DAY.isoformat(), "heads": ["A_link"],
        "issued_histories": {"A_link": []}, "history_complete_from": "2026-06-23"})
    assert resp.status_code == 200, resp.text
    head = resp.json()["heads"]["A_link"]
    assert head["threshold_feasible"] is False
    assert head["result_status"] == "empty_valid"


def test_ready_marks_future_day(monkeypatch, tmp_path):
    interim = tmp_path / "interim"
    interim.mkdir()
    (interim / "channels.parquet").touch()
    monkeypatch.setattr(config, "PATHS", SimpleNamespace(interim=interim))
    monkeypatch.setattr(product_api, "_last_feature_day", lambda: DAY)
    resp = TestClient(create_app("real")).get(
        "/ready", params={"asof": (DAY + dt.timedelta(days=5)).isoformat()})
    assert resp.json()["status"] == "future_source"
    assert resp.json()["data_last_day"] == DAY.isoformat()
