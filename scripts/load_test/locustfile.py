"""Нагрузочный профиль — владелец ML1-13 (ТЗ §11: не меньше 20 одновременных
пользователей; C2).

Двадцать пользователей по ролям, веса классов 8 : 5 : 4 : 3 — диспетчер, технолог,
аналитик, руководитель (оценка команды, как делится смена). Каждый входит через
POST /api/v1/auth/login и дальше ходит с cookie mk_session, как браузер. Доли запросов
у ролей свои: диспетчер сидит в журнале прогнозов и карточках, технолог — в заявках
и событиях, аналитик — в журналах, руководитель — на дашборде.

Поток /api/v1/stream держит доля пользователей LOAD_SSE_SHARE, по умолчанию 1: фронт
открывает одно соединение EventSource на вкладку сразу после входа. Соединение живёт
в отдельном greenlet всё время прогона; в статистике это строка «/api/v1/stream (SSE)»
со временем до заголовков ответа и строка «SSE hello» со временем до первого кадра.

Сценарий только читает. LOAD_MUTATIONS=1 добавляет диспетчеру решение по прогнозу
без решения (remote_check, preventive, комментарий «нагрузочный тест locust»). На стенде
это засоряет демо-журнал, поэтому по умолчанию выключено.

locust не входит в зависимости проекта и в venv backend, pytest этот файл не собирает.
Прогон на 20 пользователей, 10 минут:

    pip install locust
    mkdir -p data/load_test
    DEMO_PASSWORD=... locust -f scripts/load_test/locustfile.py --host http://127.0.0.1:8000 \
        --users 20 --spawn-rate 2 --run-time 10m --headless --csv data/load_test/run

PowerShell: New-Item -ItemType Directory -Force data/load_test; $env:DEMO_PASSWORD="...";
затем та же команда locust одной строкой. Без DEMO_PASSWORD в окружении пароль берётся
из .env в корне, как в других скриптах.

Отчёт: data/load_test/run_stats.csv — строка на запрос, колонки «50%» и «95%» дают p50
и p95 в мс, «Failure Count» — число ошибок; run_failures.csv — тексты ошибок. Ту же
таблицу locust печатает в конце прогона и выходит с кодом 1, если была хоть одна ошибка.
"""
import os
import random
import sys
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote

try:
    import gevent
    from locust import HttpUser, between
    from locust.exception import StopUser
    from requests.exceptions import RequestException
except ImportError:  # pragma: no cover - подсказка вместо трассировки
    raise SystemExit("locust не установлен: pip install locust") from None

# _api.py лежит в scripts/, на уровень выше: оттуда пароль из .env, как у других скриптов.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _api import env_value

API = "/api/v1"
PASSWORD = env_value("DEMO_PASSWORD")
if not PASSWORD:
    raise SystemExit("DEMO_PASSWORD не задан ни в окружении, ни в .env")
SSE_SHARE = float(os.environ.get("LOAD_SSE_SHARE", "1"))
MUTATIONS = os.environ.get("LOAD_MUTATIONS") == "1"
SSE_RETRY_S = 5  # как retry: 5000 в ответе /stream
DECISION = {"action": "remote_check", "reason_code": "preventive",
            "comment": "нагрузочный тест locust"}
# Фильтры журнала, которые даёт экран «Прогнозы»; group_by=obj сервис собирает в памяти.
JOURNAL_QUERIES = ["page=1&page_size=50", "decision=none&page=1&page_size=50",
                   "scenario=sensor_link&page=1&page_size=50",
                   "group_by=obj&page=1&page_size=50"]
ORDER_QUERIES = ["page=1&page_size=50", "status=draft&page=1&page_size=50",
                 "status=in_progress&page=1&page_size=50"]
EVENT_QUERIES = ["page=1&page_size=100", "event_class=alarm&page=1&page_size=100"]


def _ids(resp) -> list[str]:
    return [item["id"] for item in resp.json().get("items", [])] if resp.ok else []


def journal(user: "DemoUser") -> None:
    resp = user.client.get(f"{API}/forecasts?{random.choice(JOURNAL_QUERIES)}",
                           name=f"{API}/forecasts")
    user.forecast_ids = _ids(resp) or user.forecast_ids


def forecast_card(user: "DemoUser") -> None:
    if not user.forecast_ids:
        return journal(user)
    forecast_id = quote(random.choice(user.forecast_ids), safe="")
    user.client.get(f"{API}/forecasts/{forecast_id}", name=f"{API}/forecasts/{{id}}")


def dashboard(user: "DemoUser") -> None:
    """Экран «Центр управления»: сводка и очередь без решения."""
    user.client.get(f"{API}/dashboard/summary")
    resp = user.client.get(f"{API}/forecasts?decision=none&page=1&page_size=100",
                           name=f"{API}/forecasts (очередь дашборда)")
    user.forecast_ids = _ids(resp) or user.forecast_ids


def work_orders(user: "DemoUser") -> None:
    resp = user.client.get(f"{API}/work-orders?{random.choice(ORDER_QUERIES)}",
                           name=f"{API}/work-orders")
    user.order_ids = _ids(resp) or user.order_ids


def work_order_card(user: "DemoUser") -> None:
    if not user.order_ids:
        return work_orders(user)
    order_id = quote(random.choice(user.order_ids), safe="")
    user.client.get(f"{API}/work-orders/{order_id}", name=f"{API}/work-orders/{{id}}")


def events_journal(user: "DemoUser") -> None:
    user.client.get(f"{API}/events?{random.choice(EVENT_QUERIES)}", name=f"{API}/events")


def system_status(user: "DemoUser") -> None:
    user.client.get(f"{API}/system/status")


def decide(user: "DemoUser") -> None:
    """Решение по прогнозу без решения; только при LOAD_MUTATIONS=1."""
    resp = user.client.get(f"{API}/forecasts?decision=none&page=1&page_size=50",
                           name=f"{API}/forecasts (без решения)")
    ids = _ids(resp)
    if ids:
        forecast_id = quote(random.choice(ids), safe="")
        user.client.post(f"{API}/forecasts/{forecast_id}/decisions", json=DECISION,
                         name=f"{API}/forecasts/{{id}}/decisions")


class DemoUser(HttpUser):
    abstract = True
    host = "http://127.0.0.1:8000"
    wait_time = between(1, 5)
    demo_login = ""

    def on_start(self) -> None:
        self.forecast_ids: list[str] = []
        self.order_ids: list[str] = []
        self.sse = None
        resp = self.client.post(f"{API}/auth/login",
                                json={"login": self.demo_login, "password": PASSWORD})
        if resp.status_code != 200:
            raise StopUser()
        if random.random() < SSE_SHARE:
            self.sse = gevent.spawn(self.hold_stream)

    def on_stop(self) -> None:
        if self.sse is not None:
            self.sse.kill(block=False)

    def hold_stream(self) -> None:
        """Держит /stream открытым, как EventSource: после обрыва — новое соединение."""
        while True:
            started = time.perf_counter()
            try:
                # locust 2.46 пускает запрос в with-блок только с catch_response=True;
                # без него поток падал LocustError и SSE-пользователей в прогоне не было.
                with self.client.get(f"{API}/stream", stream=True, timeout=(10, 60),
                                     headers={"Accept": "text/event-stream"},
                                     name=f"{API}/stream (SSE)", catch_response=True) as resp:
                    if resp.ok:
                        for line in resp.iter_lines():
                            if line == b"event: hello":
                                self._sse("hello", started)
            except RequestException as exc:
                self._sse("обрыв", started, exc)
            gevent.sleep(SSE_RETRY_S)

    def _sse(self, name: str, started: float, exc: Exception | None = None) -> None:
        self.environment.events.request.fire(
            request_type="SSE", name=name, response_time=(time.perf_counter() - started) * 1000,
            response_length=0, exception=exc, context={})


class Dispatcher(DemoUser):
    weight = 8
    demo_login = "dispatcher"
    tasks: ClassVar[dict] = {
        journal: 6, forecast_card: 5, dashboard: 2, work_orders: 2, work_order_card: 1,
        events_journal: 2, system_status: 1, **({decide: 1} if MUTATIONS else {})}


class Technician(DemoUser):
    weight = 5
    demo_login = "technician"
    tasks: ClassVar[dict] = {
        work_orders: 5, work_order_card: 4, events_journal: 3, journal: 1, forecast_card: 1,
        dashboard: 1, system_status: 1}


class Analyst(DemoUser):
    weight = 4
    demo_login = "analyst"
    tasks: ClassVar[dict] = {
        journal: 4, forecast_card: 2, events_journal: 4, dashboard: 2, work_orders: 1,
        system_status: 1}


class Manager(DemoUser):
    weight = 3
    demo_login = "manager"
    tasks: ClassVar[dict] = {
        dashboard: 5, work_orders: 3, journal: 2, forecast_card: 1, work_order_card: 1,
        events_journal: 1, system_status: 1}
