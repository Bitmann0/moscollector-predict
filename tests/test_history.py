from pathlib import Path

import duckdb
from app import main
from app.features import build_features
from fastapi.testclient import TestClient


def test_history_api_preserves_gaps_and_null_baselines(tmp_path: Path, monkeypatch):
    warehouse = tmp_path / "source.duckdb"
    features = tmp_path / "daily.parquet"
    with duckdb.connect(str(warehouse)) as con:
        con.execute("CREATE TABLE ingest_sources(sha256 VARCHAR,time_zone VARCHAR)")
        con.execute("INSERT INTO ingest_sources VALUES ('test','Europe/Moscow')")
        con.execute(
            "CREATE TABLE daily_channels(channel_id INT, local_date DATE, event_count INT, alarm_count INT)"
        )
        con.execute(
            "INSERT INTO daily_channels VALUES (10,'2020-01-01',3,0),(10,'2020-01-03',9,1),(20,'2020-01-03',2,0)"
        )
    build_features(warehouse, features)
    monkeypatch.setattr(
        main,
        "settings",
        main.Settings(tmp_path / "missing", tmp_path / "app.db", history_features_path=features),
    )
    with TestClient(main.app) as client:
        summary = client.get("/api/v1/history/summary").json()
        assert summary["channels"] == 2 and summary["observed_channel_days"] == 3
        assert summary["prediction_available"] is False
        first = client.get("/api/v1/history/days?day=2020-01-01").json()["items"][0]
        assert first["median_events_previous_30d"] is None
        assert first["activity_ratio_to_past"] is None
        assert first["feature_available_at"].startswith("2020-01-01T21:00:00")
        filtered = client.get("/api/v1/history/days?day=2020-01-03&channel=10").json()
        assert filtered["total"] == 1 and filtered["items"][0]["event_count"] == 9
        assert (
            client.get("/api/v1/history/days?day=2020-01-03&limit=1&offset=1").json()["total"] == 2
        )
        series = client.get("/api/v1/history/channels/10?end=2020-01-03&days=3").json()
        assert series["observed_days"] == 2 and series["unobserved_days"] == 1
        assert [item["local_date"] for item in series["items"]] == ["2020-01-01", "2020-01-03"]
        assert client.get("/api/v1/history/days?day=2020-01-02").json()["total"] == 0
        assert client.get("/api/v1/history/days?day=bad").status_code == 422
        assert client.get("/api/v1/history/channels/10?end=2020-01-03&days=1000").status_code == 422


def test_disconnected_history_returns_actionable_status(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "settings", main.Settings(tmp_path, tmp_path / "app.db"))
    with TestClient(main.app) as client:
        result = client.get("/api/v1/history/summary")
        assert result.status_code == 503
        assert "история" in result.json()["detail"]
