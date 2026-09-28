"""Общие фикстуры тестов backend.

БД — SQLite во временном файле; если задан TEST_DATABASE_URL (CI, сервис PostgreSQL),
все тесты идут на нём, а таблицы удаляются и создаются заново перед каждым тестом:
нужна отдельная пустая БД, её данные не сохранятся. Секреты генерируются на сессию:
в репозитории паролей нет.

ML подменяется FakeMl через app.dependency_overrides[get_ml_client]: ответы собраны из
моделей зеркала app.schemas.ml, пакет mkl не нужен.
"""
import hashlib
import os
import secrets
import threading
import time as clock
from datetime import date, datetime, time, timedelta

import pytest
import uvicorn
from app import security
from app.config import get_settings
from app.db import Base, get_engine, reset_engine, session_factory
from app.main import create_app
from app.routers import stream as stream_router
from app.schemas.ml import (
    AddressOut,
    AlertOut,
    CoverageOut,
    FactorOut,
    HeadStatus,
    OutcomeQuery,
    OutcomeResult,
    ReadyResponse,
    ScoreRequest,
    ScoreResponse,
    WeeklyPriority,
    WeeklyResponse,
    WorkOrderOut,
)
from app.services.ml_client import MlUnavailable, get_ml_client
from fastapi.testclient import TestClient

DEMO_PASSWORD = secrets.token_urlsafe(12)
SECRET_KEY = secrets.token_urlsafe(32)
API_KEY = secrets.token_urlsafe(16)
TUESDAY = date(2026, 6, 16)
MONDAY = date(2026, 6, 15)
ROLES = ["dispatcher", "technician", "analyst", "manager", "admin"]

# Каналы синтетического справочника: (голова, канал, объект, комплекс, пикет, в бюджете).
ALERT_PLAN = [
    ("A_link", 9000001, "9101", "9100", 0.0, True),
    ("A_link", 9000006, "9102", "9100", 40.0, True),
    ("A_link", 9000011, "9103", "9100", 80.0, False),
    ("D", 9000016, "9201", "9200", 0.0, True),
]
HORIZON = {"A_link": 24, "D": 168}
DIRECTION = {"A_link": ("sensor_failure", "Отказ датчика"),
             "D": ("infrastructure_wear", "Износ оборудования")}


def _hash(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def alert_id(head: str, channel: int, asof: date) -> str:
    return _hash(head, asof.isoformat(), f"channel={channel}")


def build_score(request: ScoreRequest) -> ScoreResponse:
    asof = request.asof
    start = datetime.combine(asof + timedelta(days=1), time(0))  # без таймзоны: МСК (C1)
    alerts = []
    for rank, (head, channel, obj, parent, picket, in_budget) in enumerate(ALERT_PLAN, 1):
        direction, title = DIRECTION[head]
        alerts.append(AlertOut(
            alert_id=alert_id(head, channel, asof), case_key=_hash(head, f"channel={channel}"),
            head=head, direction=direction, direction_title=title, title=title, asof=asof,
            valid_from=start, valid_to=start + timedelta(hours=HORIZON[head]),
            horizon_hours=HORIZON[head], risk=round(0.9 - rank / 10, 2), rank=rank,
            in_budget=in_budget,
            address=AddressOut(obj=obj, obj_parent=parent, channel=channel, picket=picket,
                               obj_name=f"Объект-заглушка {obj}", obj_parent_name="Комплекс",
                               obj_kind_ru="охранная зона", sensor_name=f"Канал {channel}",
                               sensor_type="Газовый датчик", picket_label=f"ПК {picket:g}"),
            factors=[FactorOut(feature="gap_days", label="Дни без связи", contribution=0.4)],
        ))
    d_alert = next(a for a in alerts if a.head == "D")
    a_alert = alerts[0]
    orders = [
        WorkOrderOut(order_id=_hash("wo", d_alert.alert_id), created_for=asof,
                     due_by=d_alert.valid_to, priority="плановая", work_type="Диагностика",
                     direction="infrastructure_wear", direction_title="Износ", obj="9201",
                     pickets=[0.0], alert_ids=[d_alert.alert_id], channels=[9000016]),
        WorkOrderOut(order_id=_hash("wo", a_alert.alert_id), created_for=asof,
                     due_by=a_alert.valid_to, priority="срочная", work_type="Проверка связи",
                     direction="sensor_failure", direction_title="Отказ", obj="9101",
                     alert_ids=[a_alert.alert_id], channels=[9000001]),
    ]
    return ScoreResponse(
        asof=asof, source="stub",
        heads={h: HeadStatus(result_status="ok", model_version="stub", model_lag_days=7,
                             threshold_feasible=True) for h in request.heads},
        alerts=alerts,
        coverage=[CoverageOut(head=h, direction=DIRECTION[h][0], entities_total=30,
                              entities_scored=27, fraction=0.9) for h in request.heads],
        work_orders=orders,
    )


def build_weekly(asof: date) -> WeeklyResponse:
    priorities = [
        WeeklyPriority(obj=obj, rank=rank, recommendation_id=_hash("guard", obj, asof),
                       case_key=_hash("guard", obj), priority_score=score,
                       recent_alarm_days_7=days7, recent_alarm_days_30=days30,
                       evidence=f"{days7} дня с охранной тревогой за 7 дней (заглушка)",
                       obj_name=f"Объект-заглушка {obj}", address_known=True)
        for rank, (obj, score, days7, days30) in enumerate(
            [("9201", 0.8, 4, 11), ("9101", 0.5, 2, 6)], 1)
    ]
    return WeeklyResponse(asof=asof, valid_from=asof + timedelta(days=2),
                          valid_to=asof + timedelta(days=9), next_run=asof + timedelta(days=7),
                          result_status="ok", priorities=priorities, source="stub")


class FakeMl:
    """Подмена MlClient: те же методы, ответы по C1, журнал вызовов в calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.fail_score = False
        self.fail_weekly = False
        self.fail_ready = False

    def score(self, request: ScoreRequest) -> ScoreResponse:
        self.calls.append(("score", request))
        if self.fail_score:
            raise MlUnavailable("ConnectError: ML недоступен (тест)")
        return build_score(request)

    def weekly(self, asof: date) -> WeeklyResponse:
        self.calls.append(("weekly", asof))
        if self.fail_weekly:
            raise MlUnavailable("HTTP 500: сбой недельной очереди (тест)")
        return build_weekly(asof)

    def ready(self, asof: date | None = None) -> ReadyResponse:
        self.calls.append(("ready", asof))
        if self.fail_ready:
            raise MlUnavailable("ConnectError: ML недоступен (тест)")
        return ReadyResponse(status="ready", asof=asof, data_last_day=date(2026, 6, 30),
                             source="stub")

    def outcomes(self, items: list[OutcomeQuery]) -> list[OutcomeResult]:
        self.calls.append(("outcomes", items))
        return [OutcomeResult(id=item.id, outcome="unknown") for item in items]


@pytest.fixture(autouse=True)
def reset_login_throttle():
    """Счётчик неудачных входов общий на процесс: тесты не должны делить его."""
    from app.routers.auth import throttle

    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture(autouse=True)
def fresh_parameters():
    """Кеш параметров (app/services/parameters.py) общий на процесс, а БД у теста своя."""
    from app.services import parameters

    parameters.invalidate()
    yield
    parameters.invalidate()


@pytest.fixture(autouse=True)
def fast_password_hash(monkeypatch):
    """200 000 итераций PBKDF2 — это ~0,2 с на хеш; seed в каждом тесте хеширует пять
    паролей. Число итераций хранится в самом хеше, поэтому проверка пароля не меняется."""
    monkeypatch.setattr(security, "_ITERATIONS", 1_000)


@pytest.fixture
def db_url(tmp_path) -> str:
    return os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def env(db_url, monkeypatch, tmp_path):
    """Переменные окружения сервиса и чистая схема БД."""
    for key, value in {"DATABASE_URL": db_url, "SECRET_KEY": SECRET_KEY,
                       "DEMO_PASSWORD": DEMO_PASSWORD, "INTEGRATION_API_KEY": API_KEY,
                       "DEMO_TODAY": "2026-06-30", "DEMO_SETTINGS_LOCKED": "0",
                       "SEED_DEMO": "1", "ML_URL": "http://ml.invalid:8001",
                       # Пустой каталог: настоящий справочник с машины разработчика
                       # не должен подменять синтетику в тестах.
                       "RAW_DATA_DIR": str(tmp_path / "raw")}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_engine()
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def db(env):
    with session_factory()() as session:
        yield session


@pytest.fixture
def seeded(db):
    from app.seed import seed

    seed(db)
    return db


@pytest.fixture
def fake_ml() -> FakeMl:
    return FakeMl()


@pytest.fixture
def app(seeded, fake_ml):
    application = create_app()
    application.dependency_overrides[get_ml_client] = lambda: fake_ml
    return application


@pytest.fixture
def login(app):
    """login("dispatcher") → TestClient с cookie сессии этой роли."""
    clients = []

    def make(role: str) -> TestClient:
        client = TestClient(app)
        resp = client.post("/api/v1/auth/login", json={"login": role, "password": DEMO_PASSWORD})
        assert resp.status_code == 200, resp.text
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


@pytest.fixture
def admin(login) -> TestClient:
    return login("admin")


@pytest.fixture
def integration(app):
    with TestClient(app, headers={"X-API-Key": API_KEY}) as client:
        yield client


@pytest.fixture
def ran(admin) -> dict:
    """Прогон понедельника 2026-06-15 (с недельной очередью) через API."""
    resp = admin.post("/api/v1/admin/run-daily", json={"asof": MONDAY.isoformat()})
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def live_server(app, monkeypatch):
    """uvicorn в потоке: TestClient копит тело ответа целиком и бесконечный SSE не отдаст.
    Нужен и скриптам scripts/_api.py: они ходят через urllib по настоящему HTTP."""
    monkeypatch.setattr(stream_router, "HEARTBEAT_S", 0.2)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = clock.monotonic() + 10
    while not server.started:
        assert clock.monotonic() < deadline, "uvicorn не стартовал"
        clock.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
