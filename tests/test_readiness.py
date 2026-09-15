from datetime import UTC, datetime
from pathlib import Path

from app import main
from fastapi.testclient import TestClient


def test_missing_data_liveness_and_readiness(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "settings", main.Settings(tmp_path / "missing", tmp_path / "db"))
    with TestClient(main.app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code == 503
        assert client.post("/api/v1/maintenance-requests/auto").status_code == 409


def test_live_readiness_expires_without_restart(
    sample_data_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    class Clock(datetime):
        value = datetime(2026, 8, 1, 10, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(
        main,
        "settings",
        main.Settings(
            sample_data_dir,
            tmp_path / "db",
            mode="live",
            max_data_age_hours=6,
        ),
    )
    with TestClient(main.app) as client:
        assert client.get("/api/v1/ready").status_code == 200
        Clock.value = datetime(2026, 8, 1, 16, tzinfo=UTC)
        assert client.get("/api/v1/ready").status_code == 503
        assert (
            client.post("/api/v1/maintenance-requests", json={"channel_id": 10}).status_code == 409
        )


def test_historical_drafts_are_marked_and_source_is_preserved(
    sample_data_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main, "settings", main.Settings(sample_data_dir, tmp_path / "db"))
    with TestClient(main.app) as client:
        assert client.get("/api/v1/ready").status_code == 200
        item = client.post("/api/v1/maintenance-requests", json={"channel_id": 10}).json()["item"]
        assert item["assessment_mode"] == "historical"
        assert item["data_as_of"] == "2026-08-01T09:00:00+00:00"
        assert (
            client.get("/api/v1/forecasts?limit=1&offset=1").json()["items"][0]["channel_id"] == 20
        )
