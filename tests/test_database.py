import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import MaintenanceRepository


def test_concurrent_create_and_file_release(tmp_path: Path) -> None:
    path = tmp_path / "requests.db"
    repository = MaintenanceRepository(path)
    forecast = {
        "channel_id": 10,
        "sensor_name": "Test",
        "risk_score": 0.6,
        "risk_level": "high",
        "recommendation": "Review",
        "assessment_mode": "historical",
        "data_as_of": "2026-08-01",
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: repository.create(forecast), range(16)))
    assert sum(created for _, created in results) == 1
    assert len(repository.list()) == 1
    path.unlink()  # On Windows an unclosed SQLite connection makes this fail.


def test_migration_preserves_existing_drafts(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE maintenance_requests (
            id INTEGER PRIMARY KEY, channel_id INTEGER, sensor_name TEXT, risk_score REAL,
            priority TEXT, recommendation TEXT, status TEXT DEFAULT 'draft', created_at TEXT,
            UNIQUE(channel_id,status))""")
        connection.execute(
            "INSERT INTO maintenance_requests VALUES (1,10,'Old',0.8,'high',"
            "'Review','draft','2026-08-01')"
        )
    connection.close()
    repository = MaintenanceRepository(path)
    assert repository.list()[0]["assessment_mode"] == "legacy"
    assert repository.list()[0]["sensor_name"] == "Old"
