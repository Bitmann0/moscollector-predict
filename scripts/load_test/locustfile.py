"""ЗАГЛУШКА — владелец ML1-13 (ТЗ §11: не меньше 20 одновременных пользователей).
Заменить: профиль нагрузки по ролям и долям запросов (журнал, карточка, дашборд, заявки,
события, SSE-подписка), прогон 10 мин на стенде, отчёт о задержках p50/p95 и ошибках
для раздела «Надёжность и производительность».
Контракт: сценарий входит под демо-пользователем и читает только API C2; запуск —
командой ниже; тест scripts/smoke_compose.py должен остаться зелёным.

locust не входит в зависимости проекта, ставится отдельно:

    pip install locust
    DEMO_PASSWORD=... locust -f scripts/load_test/locustfile.py --host http://localhost:8000 \
        --users 20 --spawn-rate 2 --run-time 10m --headless
"""
import os
import random

try:
    from locust import HttpUser, between, task
except ImportError:  # pragma: no cover - подсказка вместо трассировки
    raise SystemExit("locust не установлен: pip install locust") from None

LOGINS = ["dispatcher", "technician", "analyst", "manager"]


class DemoUser(HttpUser):
    wait_time = between(1, 5)

    def on_start(self) -> None:
        login = os.environ.get("DEMO_LOGIN") or random.choice(LOGINS)
        self.client.post("/api/v1/auth/login",
                         json={"login": login, "password": os.environ.get("DEMO_PASSWORD", "")})
        self.forecast_ids: list[str] = []

    @task(5)
    def forecasts(self) -> None:
        resp = self.client.get("/api/v1/forecasts?page_size=50")
        if resp.ok:
            self.forecast_ids = [item["id"] for item in resp.json().get("items", [])]

    @task(3)
    def card(self) -> None:
        if self.forecast_ids:
            self.client.get(f"/api/v1/forecasts/{random.choice(self.forecast_ids)}",
                            name="/api/v1/forecasts/{id}")

    @task(2)
    def dashboard(self) -> None:
        self.client.get("/api/v1/dashboard/summary")

    @task(1)
    def status(self) -> None:
        self.client.get("/api/v1/system/status")
