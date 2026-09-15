from pathlib import Path

from app import main
from fastapi.testclient import TestClient


def test_health_and_forecasts(tmp_path: Path, sample_data_dir: Path) -> None:
    main.settings = main.Settings(
        data_dir=sample_data_dir, database_path=tmp_path / "test.db", forecast_hours=24
    )
    with TestClient(main.app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["ready"] is True
        forecasts = client.get("/api/v1/forecasts?limit=2")
        assert forecasts.status_code == 200
        assert len(forecasts.json()["items"]) == 2


def test_create_request_is_idempotent(tmp_path: Path, sample_data_dir: Path) -> None:
    main.settings = main.Settings(
        data_dir=sample_data_dir, database_path=tmp_path / "test.db", forecast_hours=24
    )
    with TestClient(main.app) as client:
        channel_id = client.get("/api/v1/forecasts?limit=1").json()["items"][0]["channel_id"]
        first = client.post("/api/v1/maintenance-requests", json={"channel_id": channel_id})
        second = client.post("/api/v1/maintenance-requests", json={"channel_id": channel_id})
        assert first.status_code == 201
        assert first.json()["created"] is True
        assert second.json()["created"] is False
