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


def test_feedback_snapshot_survives_restart(tmp_path: Path, sample_data_dir: Path) -> None:
    main.settings = main.Settings(sample_data_dir, tmp_path / "feedback.db")
    with TestClient(main.app) as client:
        response = client.post("/api/v1/forecasts/10/feedback", json={
            "decision": "monitor", "reason": "Проверить на обходе", "author": "Диспетчер 1",
        })
        assert response.status_code == 201
        snapshot = response.json()["forecast_snapshot"]
        assert snapshot["data_to"].startswith("2026-08-01")
        assert snapshot["predicted_at"].startswith("2026-08-01")
        assert snapshot["model_version"] == "0.1.0"
        assert client.post("/api/v1/forecasts/10/feedback", json={
            "decision": "monitor", "reason": "   ", "author": "Диспетчер",
        }).status_code == 422
        assert client.post("/api/v1/forecasts/999/feedback", json={
            "decision": "monitor", "reason": "Проверить", "author": "Диспетчер",
        }).status_code == 404
    with TestClient(main.app) as client:
        history = client.get("/api/v1/forecasts/10/feedback").json()
        assert history["total"] == 1
        assert history["items"][0]["forecast_snapshot"] == snapshot
