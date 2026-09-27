"""Тест формы C2: каждый эндпоинт отвечает своей pydantic-схемой.

Владельцы заглушек меняют содержимое ответов, но не их форму: этот тест должен
оставаться зелёным. Список GET берётся из OpenAPI приложения, поэтому новый GET без
данных для его параметров здесь сразу упадёт — допишите их в REQUIRED_QUERY.
"""
import io
import threading
import time

import httpx
import openpyxl
import pytest
import uvicorn
from app import models
from app.main import API_PREFIX, ROUTERS, create_app
from app.routers import stream as stream_router
from app.services.export import COLUMNS
from app.services.helpers import now_utc
from app.services.notifications import broker
from conftest import DEMO_PASSWORD
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

API = "/api/v1"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SKIP_GET = {f"{API}/stream", f"{API}/export/forecasts.xlsx"}
REQUIRED_QUERY = {"scenario": "sensor_link"}

_APP = create_app()
# FastAPI 0.14x оборачивает подключённые роутеры в _IncludedRouter, поэтому маршруты берём
# из самих роутеров (main.ROUTERS) плюс объявленные прямо на приложении (/health).
_ROUTES = {(m, API_PREFIX + r.path): r for module in ROUTERS for r in module.router.routes
           if isinstance(r, APIRoute) for m in r.methods}
_ROUTES |= {(m, r.path): r for r in _APP.routes if isinstance(r, APIRoute) for m in r.methods}
GET_PATHS = sorted(p for p, ops in _APP.openapi()["paths"].items()
                   if "get" in ops and p not in SKIP_GET)


def _validate(method: str, path: str, resp: httpx.Response) -> None:
    model = _ROUTES[(method, path)].response_model
    if model is None:
        return
    if model is str:
        assert isinstance(resp.text, str) and resp.text
        return
    TypeAdapter(model).validate_python(resp.json())


def _url(path: str, ids: dict) -> tuple[str, dict]:
    operation = _APP.openapi()["paths"][path]["get"]
    params = {}
    for p in operation.get("parameters", []):
        if p["in"] == "query" and p.get("required"):
            assert p["name"] in REQUIRED_QUERY, f"нет значения для {path}?{p['name']}"
            params[p["name"]] = REQUIRED_QUERY[p["name"]]
    return path.format(**ids), params


@pytest.fixture
def ids(ran, admin) -> dict:
    forecast = admin.get(f"{API}/forecasts").json()["items"][0]["id"]
    order = admin.get(f"{API}/work-orders").json()["items"][0]["id"]
    return {"forecast_id": forecast, "order_id": order}


@pytest.mark.parametrize("path", GET_PATHS)
def test_every_get_matches_schema(path, admin, ids):
    url, params = _url(path, ids)
    resp = admin.get(url, params=params)
    assert resp.status_code == 200, resp.text
    _validate("GET", path, resp)


def test_get_list_is_not_empty_after_run(admin, ran):
    """Сквозная труба: после run-daily журнал, заявки и дерево не пусты."""
    admin.post(f"{API}/ingest/events", json=[])  # партия, чтобы история загрузок не пуста
    for path in ["/forecasts", "/work-orders", "/ingest/batches"]:
        assert admin.get(f"{API}{path}").json()["total"] > 0, path
    assert admin.get(f"{API}/reference/tree").json()[0]["channels"] == 30


def test_export_is_xlsx_with_header(admin):
    resp = admin.get(f"{API}/export/forecasts.xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == XLSX
    sheet = openpyxl.load_workbook(io.BytesIO(resp.content)).active
    assert next(sheet.iter_rows(values_only=True)) == tuple(COLUMNS)


def _mutations(ids: dict) -> list[tuple[str, str, str, dict]]:
    """(метод, шаблон пути из OpenAPI, фактический путь, kwargs запроса)."""
    fid, oid = ids["forecast_id"], ids["order_id"]
    csv_body = "ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n" \
               "1,9000001,2026-06-30,10:00:00,t,1\n2,9000002,2026-06-30,10:01:00,f,0\n"
    return [
        ("POST", "/auth/login", "/auth/login",
         {"json": {"login": "admin", "password": DEMO_PASSWORD}}),
        ("POST", "/forecasts/{forecast_id}/decisions", f"/forecasts/{fid}/decisions",
         {"json": {"action": "dispatch_crew", "reason_code": "confirmed_by_readings",
                   "comment": "проверка формы"}}),
        ("POST", "/forecasts/{forecast_id}/outcome", f"/forecasts/{fid}/outcome",
         {"json": {"outcome": "confirmed_event", "event_at": "2026-06-16T10:00:00",
                   "channel": 9000001}}),
        ("POST", "/work-orders", "/work-orders", {"json": {"forecast_ids": [fid]}}),
        ("PATCH", "/work-orders/{order_id}", f"/work-orders/{oid}",
         {"json": {"expected_status": "draft", "status": "confirmed", "reason": "тест"}}),
        ("POST", "/ingest/events", "/ingest/events",
         {"json": [{"ид_события": 1, "ид_канала_данных": 9000001, "дата": "2026-06-30",
                    "время": "10:00:00", "тревожное": "t", "значение_датчика": "1"}]}),
        ("POST", "/ingest/events/upload", "/ingest/events/upload",
         {"files": {"file": ("events.csv", csv_body.encode(), "text/csv")}}),
        ("POST", "/ingest/ods-journal", "/ingest/ods-journal",
         {"json": [{"ts": "2026-06-30T09:00:00+03:00", "obj_id": "9101",
                    "record_type": "обход"}]}),
        ("DELETE", "/ingest/day/{day}", "/ingest/day/2026-06-30", {}),
        ("POST", "/reference/sync", "/reference/sync", {}),
        ("PUT", "/settings", "/settings", {"json": {"replay_speed": 120}}),
        ("POST", "/admin/run-daily", "/admin/run-daily", {"json": {"asof": "2026-06-16"}}),
        ("DELETE", "/admin/issued-log", "/admin/issued-log",
         {"params": {"from": "2026-06-16", "to": "2026-06-16"}}),
        ("POST", "/admin/emulate-decisions", "/admin/emulate-decisions",
         {"json": {"date_from": "2026-06-01", "date_to": "2026-06-29"}}),
        ("POST", "/notifications/{notification_id}/read", "/notifications/1/read", {}),
        ("POST", "/auth/logout", "/auth/logout", {}),
    ]


def test_every_mutation_matches_schema(admin, ids):
    mutations = _mutations(ids)
    declared = {(m.upper(), p.removeprefix(API)) for p, ops in _APP.openapi()["paths"].items()
                for m in ops if m != "get"}
    assert declared == {(m, t) for m, t, _, _ in mutations}, "новый эндпоинт — добавьте сюда"
    for method, template, path, kwargs in mutations:
        resp = admin.request(method, f"{API}{path}", **kwargs)
        if template == "/notifications/{notification_id}/read":
            assert resp.status_code in {204, 404}  # до BE-08 лента пуста
            continue
        assert resp.status_code in {200, 201, 204}, (path, resp.text)
        if resp.status_code != 204:
            _validate(method, f"{API}{template}", resp)


def test_upload_counts_rows_and_batch_is_listed(admin):
    body = ("ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
            "1,9000001,2026-06-30,10:00:00,t,1\n2,9000002,2026-06-30,10:01:00,f,0\n"
            "3,9000003,2026-06-30,10:02:00,f,0\n")
    resp = admin.post(f"{API}/ingest/events/upload",
                      files={"file": ("events.csv", body.encode(), "text/csv")})
    assert resp.status_code == 201
    batch = resp.json()
    assert batch["rows_total"] == 3
    listed = admin.get(f"{API}/ingest/batches").json()["items"]
    assert listed[0]["batch_id"] == batch["batch_id"]


def test_upload_rejects_only_bad_rows_and_deduplicates(admin):
    body = ("ид_события,ид_канала_данных,дата,время,тревожное,значение_датчика\n"
            "11,9000001,2026-06-30,10:00:00,t,1\n"
            "bad,9000002,2026-06-30,10:01:00,f,0\n")
    first = admin.post(f"{API}/ingest/events/upload",
                       files={"file": ("events.csv", body.encode(), "text/csv")}).json()
    assert (first["accepted"], first["rejected"], first["duplicates"]) == (1, 1, 0)
    second = admin.post(f"{API}/ingest/events/upload",
                        files={"file": ("events.csv", body.encode(), "text/csv")}).json()
    assert (second["accepted"], second["rejected"], second["duplicates"]) == (0, 1, 1)


def test_forecast_decision_filters_and_grouping(admin, ran):
    page = admin.get(f"{API}/forecasts").json()
    fid = page["items"][0]["id"]
    assert admin.post(f"{API}/forecasts/{fid}/decisions",
                      json={"action": "defer", "reason_code": "await_data"}).status_code == 201
    assert admin.get(f"{API}/forecasts", params={"decision": "any"}).json()["total"] == 1
    assert admin.get(f"{API}/forecasts", params={"decision": "none"}).json()["total"] == (
        page["total"] - 1)
    grouped = admin.get(f"{API}/forecasts", params={"group_by": "obj"}).json()
    assert grouped["total"] == len({item["object"]["id"] for item in page["items"]})


def test_quality_counts_unmatured_forecasts_as_unknown(admin, ran):
    result = admin.get(f"{API}/quality", params={"scenario": "sensor_link"}).json()
    week = next(item for item in result["weeks"] if item["week_start"] == "2026-06-15")
    assert week["issued"] == 2
    assert week["unknown"] == 2
    assert week["precision"] is None


def test_notifications_read_flag(seeded, admin):
    seeded.add(models.Notification(ts=now_utc(), kind="alert.new", severity="warning",
                                   title="проверка", payload={}, read_by=[]))
    seeded.commit()
    item = admin.get(f"{API}/notifications").json()["items"][0]
    assert item["read"] is False
    assert admin.post(f"{API}/notifications/{item['id']}/read").status_code == 204
    assert admin.get(f"{API}/notifications").json()["items"][0]["read"] is True
    assert admin.post(f"{API}/notifications/999999/read").status_code == 404


def test_work_order_manual_create_and_transition(admin, ids):
    forecast = ids["forecast_id"]
    first = admin.post(f"{API}/work-orders", json={"forecast_ids": [forecast]})
    assert first.status_code == 201
    card = first.json()
    assert card["id"].startswith("WO-") and card["status"] == "draft"
    assert card["forecast_ids"] == [forecast] and card["created_by"] == "admin"
    again = admin.post(f"{API}/work-orders", json={"forecast_ids": [forecast]}).json()
    assert again["id"] == card["id"]
    moved = admin.patch(f"{API}/work-orders/{card['id']}",
                        json={"expected_status": "draft", "status": "confirmed"}).json()
    assert moved["status"] == "confirmed"
    assert [(h["from_status"], h["to_status"]) for h in moved["history"]][-1] == (
        "draft", "confirmed")


def test_geo_schema_is_conditional(admin):
    geo = admin.get(f"{API}/schema.geojson").json()
    assert geo["properties"]["note"] == "условная схема, не географические координаты"
    assert geo["features"]
    assert admin.get(f"{API}/schema.wkt").text.startswith("GEOMETRYCOLLECTION")


@pytest.fixture
def live_server(app, monkeypatch):
    """uvicorn в потоке: TestClient копит тело ответа целиком и бесконечный SSE не отдаст."""
    monkeypatch.setattr(stream_router, "HEARTBEAT_S", 0.2)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                           lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn не стартовал"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def _read_until(lines, wanted: str, limit: int = 50) -> list[str]:
    seen = []
    for line in lines:
        seen.append(line)
        if line == wanted or len(seen) >= limit:
            break
    return seen


def test_stream_first_frame_is_hello(live_server):
    with httpx.Client(base_url=live_server, timeout=10) as http:
        assert http.get(f"{API}/stream").status_code == 401
        token = http.post(f"{API}/auth/login", json={"login": "dispatcher",
                                                     "password": DEMO_PASSWORD}
                          ).cookies["mk_session"]
        headers = {"Cookie": f"mk_session={token}"}
        with http.stream("GET", f"{API}/stream", headers=headers) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            lines = resp.iter_lines()
            seen = _read_until(lines, "event: hello")
            assert seen[-1] == "event: hello", seen
            broker.publish("alert.new", {"id": "test"}, title="проверка")
            seen = _read_until(lines, "event: alert.new")
            assert seen[-1] == "event: alert.new", seen
    deadline = time.monotonic() + 5
    while broker._subs and time.monotonic() < deadline:  # отписка после обрыва соединения
        time.sleep(0.05)
    assert not broker._subs
