import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.database import MaintenanceRepository


@pytest.fixture
def forecast():
    return {"channel_id": 10, "sensor_name": "Тест", "risk_score": 0.7,
            "risk_level": "high", "recommendation": "Проверить"}


def test_concurrent_creation_and_lifecycle(tmp_path, forecast):
    repository = MaintenanceRepository(tmp_path / "requests.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: repository.create(forecast), range(20)))
    assert sum(created for _, created in results) == 1
    request_id = results[0][0]["id"]
    repository.transition(request_id, "draft", "in_progress", "Диспетчер", "Проверка")
    assert repository.create(forecast)[1] is False
    with pytest.raises(ValueError, match="уже изменена"):
        repository.transition(request_id, "draft", "cancelled", "Другой", "Отмена")
    repository.transition(request_id, "in_progress", "completed", "Диспетчер", "Проверено")
    with pytest.raises(ValueError, match="Недопустимый"):
        repository.transition(request_id, "completed", "in_progress", "Диспетчер", "Ещё раз")
    second, created = repository.create(forecast)
    assert created and second["id"] != request_id
    repository.transition(second["id"], "draft", "in_progress", "Диспетчер", "Проверка")
    repository.transition(second["id"], "in_progress", "completed", "Диспетчер", "Готово")
    reopened = MaintenanceRepository(tmp_path / "requests.db")
    assert len(reopened.request_detail(request_id)["history"]) == 2
    assert len(reopened.list()) == 2


def test_upgrade_preserves_original_database(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE maintenance_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    sensor_name TEXT NOT NULL,
                    risk_score REAL NOT NULL,
                    priority TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    UNIQUE(channel_id, status)
                )""")
    connection.execute("INSERT INTO maintenance_requests VALUES "
                       "(5, 10, 'Датчик', 0.7, 'high', 'Проверить', 'draft', '2026-09-15')")
    connection.commit()
    connection.close()
    repository = MaintenanceRepository(path)
    assert repository.request_detail(5)["sensor_name"] == "Датчик"
    repository.transition(5, "draft", "cancelled", "Диспетчер", "Ошибка регистрации")
    assert repository.request_detail(5)["history"][0]["status"] == "cancelled"
