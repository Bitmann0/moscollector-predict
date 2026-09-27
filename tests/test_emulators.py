"""Эмуляторы ML2-11: план записей ОДС и шагов help desk на поддельном API без сети
и без ожидания, затем по одному проходу против приложения через TestClient."""
import argparse
import importlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote

import pytest
from app import models
from app.schemas.events import OdsRowIn
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
_api = importlib.import_module("_api")
emulation = importlib.import_module("_emulation")
ods = importlib.import_module("emulate_ods")
helpdesk = importlib.import_module("emulate_helpdesk")

API = "/api/v1"
VOCAB = json.loads((ROOT / "contracts" / "vocabularies.json").read_text(encoding="utf-8"))
TRANSITIONS = VOCAB["work_order_transitions"]
TRANSITION_PERM = VOCAB["work_order_transition_perm"]
INTEGRATION_PERMS = frozenset(p for p, roles in VOCAB["permissions"].items()
                              if "integration" in roles)
ACTIONS = {item["code"] for item in VOCAB["action"]}
DAY = date(2026, 6, 30)


class FakeClient:
    """Клиент в интерфейсе _api.Api.call: запросы уходят в FakeServer от имени who."""

    def __init__(self, server: "FakeServer", who: str) -> None:
        self.server = server
        self.who = who

    def call(self, method, path, body=None, *, timeout=None):
        return self.server.handle(self.who, method, path, body)


class FakeServer:
    """Заявки и приём ОДС с проверками сервиса: 404, 409, 422, 403 — как в
    services/work_orders.transition."""

    def __init__(self, orders=(), *, perms=INTEGRATION_PERMS, forecasts=()) -> None:
        self.orders = {order["id"]: dict(order) for order in orders}
        self.forecasts = list(forecasts)
        self.perms = perms
        self.calls: list[tuple] = []
        self.before_patch = None
        self.forced: dict[str, int] = {}
        self.ods_rows: list[dict] = []
        self.reader = FakeClient(self, "reader")
        self.writer = FakeClient(self, "writer")

    def patches(self, order_id=None) -> list[dict]:
        return [body for who, method, path, body in self.calls
                if method == "PATCH" and (order_id is None or path.endswith(f"/{order_id}"))]

    @staticmethod
    def _page(items, params):
        page, size = int(params.get("page", 1)), int(params.get("page_size", 50))
        return 200, {"items": items[(page - 1) * size:page * size], "total": len(items),
                     "page": page, "page_size": size}

    def handle(self, who, method, path, body):
        self.calls.append((who, method, path, body))
        route, _, query = path.partition("?")
        params = dict(parse_qsl(query))
        if method == "GET":
            assert who == "reader", f"читать должен reader: {method} {path}"
        else:
            assert who == "writer", f"писать должен writer: {method} {path}"
        if method == "GET" and route == "/system/status":
            return 200, {"demo_today": DAY.isoformat()}
        if method == "GET" and route == "/forecasts":
            return self._page(self.forecasts, params)
        if method == "GET" and route == "/work-orders":
            items = [dict(o) for _, o in sorted(self.orders.items())
                     if params.get("status") in (None, o["status"])]
            return self._page(items, params)
        if method == "POST" and route == "/ingest/ods-journal":
            fresh = [row for row in body if row not in self.ods_rows]
            self.ods_rows.extend(fresh)
            return 201, {"accepted": len(fresh), "duplicates": len(body) - len(fresh),
                         "rejected": 0}
        if route.startswith("/work-orders/"):
            order_id = unquote(route.rsplit("/", 1)[1])
            order = self.orders.get(order_id)
            if order is None:
                raise _api.ApiError(method, path, 404, "work_order_not_found")
            if method == "GET":
                return 200, dict(order)
            if method == "PATCH":
                if self.before_patch:
                    self.before_patch(order_id, order)
                if order_id in self.forced:
                    raise _api.ApiError(method, path, self.forced[order_id], "forced")
                if order["status"] != body["expected_status"]:
                    raise _api.ApiError(method, path, 409, json.dumps(
                        {"detail": {"code": "status_conflict",
                                    "current_status": order["status"]}}))
                if body["status"] not in TRANSITIONS[order["status"]]:
                    raise _api.ApiError(method, path, 422, "work_order_transition_not_allowed")
                if TRANSITION_PERM.get(body["status"]) not in self.perms:
                    raise _api.ApiError(method, path, 403, "forbidden")
                order["status"] = body["status"]
                return 200, dict(order)
        raise AssertionError(f"неожиданный запрос {method} {path}")


def order(order_id, status, priority="planned"):
    return {"id": order_id, "status": status, "priority": priority, "forecast_ids": []}


def desk_for(server, **kwargs):
    return helpdesk.Helpdesk(server.reader, server.writer, perms=server.perms, vocab=VOCAB,
                             **kwargs)


# --- help desk ------------------------------------------------------------------------

def test_next_status_follows_graph_and_integration_perms():
    plan = {status: helpdesk.next_status(status, TRANSITIONS, TRANSITION_PERM,
                                         INTEGRATION_PERMS) for status in TRANSITIONS}
    assert plan == {"draft": None, "confirmed": "in_progress", "in_progress": "completed",
                    "completed": None, "cancelled": None}
    for status, target in plan.items():
        if target:
            assert target in TRANSITIONS[status]
            assert TRANSITION_PERM[target] in INTEGRATION_PERMS


def test_next_status_never_cancels_and_needs_perms():
    every_perm = frozenset(VOCAB["permissions"])
    for status in TRANSITIONS:
        assert helpdesk.next_status(status, TRANSITIONS, TRANSITION_PERM,
                                    every_perm) != "cancelled"
        assert helpdesk.next_status(status, TRANSITIONS, TRANSITION_PERM, frozenset()) is None


def test_once_moves_each_order_one_step_with_expected_status():
    server = FakeServer([order("A", "confirmed", "urgent"), order("B", "in_progress"),
                         order("C", "draft"), order("D", "completed")])
    steps = desk_for(server).step(wait=False)

    assert [(s.order_id, s.from_status, s.to_status, s.result) for s in steps] == [
        ("A", "confirmed", "in_progress", "moved"), ("B", "in_progress", "completed", "moved")]
    assert {k: o["status"] for k, o in server.orders.items()} == {
        "A": "in_progress", "B": "completed", "C": "draft", "D": "completed"}
    for body in server.patches():
        assert set(body) == {"expected_status", "status", "reason"}
        assert body["reason"].startswith("эмуляция help desk")
    assert [b["expected_status"] for b in server.patches()] == ["confirmed", "in_progress"]
    listed = {dict(parse_qsl(path.partition("?")[2])).get("status")
              for _, method, path, _ in server.calls if path.startswith("/work-orders?")}
    assert listed == {"confirmed", "in_progress"}


def test_conflict_rereads_order_and_retries_from_fresh_status():
    server = FakeServer([order("A", "confirmed")])

    def human_takes_it(order_id, row):
        row["status"] = "in_progress"  # технолог успел взять заявку между чтением и PATCH
        server.before_patch = None

    server.before_patch = human_takes_it
    desk = desk_for(server)
    [step] = desk.step(wait=False)

    assert (step.result, step.detail) == ("conflict", "статус уже in_progress")
    assert len(server.patches("A")) == 1
    assert server.calls[-1][:3] == ("reader", "GET", "/work-orders/A")
    assert desk.seen["A"].status == "in_progress"

    [step] = desk.step(wait=False)
    assert (step.from_status, step.to_status, step.result) == ("in_progress", "completed",
                                                               "moved")
    assert server.patches("A")[-1]["expected_status"] == "in_progress"


def test_forbidden_and_illegal_are_skipped_until_status_changes():
    server = FakeServer([order("A", "confirmed"), order("B", "confirmed"),
                         order("C", "confirmed")])
    server.forced = {"A": 403, "B": 422}
    desk = desk_for(server)

    results = {s.order_id: s.result for s in desk.step(wait=False)}
    assert results == {"A": "skipped", "B": "skipped", "C": "moved"}
    assert {s.order_id for s in desk.step(wait=False)} == {"C"}  # A и B не долбим

    server.forced = {}
    server.orders["A"]["status"] = "in_progress"  # статус сменили вручную
    moved = {s.order_id: s.to_status for s in desk.step(wait=False)}
    assert moved == {"A": "completed"}


def test_delay_waits_for_clock_and_scales_with_speed():
    now = [0.0]
    server = FakeServer([order("A", "confirmed", "urgent")])
    desk = desk_for(server, speed=60, clock=lambda: now[0], seed=7)
    wait = helpdesk.delay_s(7, "A", "confirmed", "urgent", 60)

    assert desk.step() == []
    now[0] = wait - 0.1
    assert desk.step() == []
    now[0] = wait + 0.1
    assert [s.result for s in desk.step()] == ["moved"]

    real = helpdesk.delay_s(7, "A", "confirmed", "urgent", 1)
    assert real == pytest.approx(wait * 60)
    assert 15 * 60 <= real <= 45 * 60
    assert helpdesk.delay_s(7, "A", "confirmed", "urgent", 1) == real
    assert helpdesk.delay_s(8, "A", "confirmed", "urgent", 1) != real


def test_dry_run_does_not_patch():
    server = FakeServer([order("A", "confirmed")])
    [step] = desk_for(server, dry_run=True).step(wait=False)
    assert step.result == "planned" and server.patches() == []


def test_read_all_walks_pages(monkeypatch):
    monkeypatch.setattr(emulation, "PAGE_SIZE", 2)
    server = FakeServer([order(f"W{i}", "confirmed") for i in range(5)])
    items = emulation.read_all(server.reader, "/work-orders?status=confirmed")
    assert [item["id"] for item in items] == ["W0", "W1", "W2", "W3", "W4"]


def test_reader_logs_in_again_after_401():
    class Expiring:
        def __init__(self):
            self.logins = 0
            self.expired = True

        def call(self, method, path, body=None, *, timeout=None):
            if self.expired:
                self.expired = False
                raise _api.ApiError(method, path, 401, "session_invalid")
            return 200, {"ok": True}

        def login(self, login, password):
            self.logins += 1

    api = Expiring()
    assert emulation.Reader(api, "technician", "pw").call("GET", "/me") == (200, {"ok": True})
    assert api.logins == 1


# --- журнал ОДС -----------------------------------------------------------------------

def forecast(fid, rank, *, valid_from="2026-06-30T00:00:00+03:00",
             valid_to="2026-07-01T00:00:00+03:00", obj="9101", **extra):
    return {"id": fid, "rank": rank, "scenario": "sensor_link",
            "scenario_title": "Отказ датчика: риск потери связи", "valid_from": valid_from,
            "valid_to": valid_to, "object": {"id": obj}, "decision": None,
            "outcome_auto": None, "outcome_manual": None, "work_order_id": None, **extra}


FORECASTS = [forecast(f"F{i:02d}", i, obj=str(9100 + i)) for i in range(1, 13)]
REASON_BY_TITLE = {item["title"]: item for item in VOCAB["reason_code"]}


def plan(forecasts=FORECASTS, orders=(), *, seed=42, count=20, with_orders=True):
    return ods.plan_day(DAY, list(forecasts), list(orders), seed=seed, count=count, vocab=VOCAB,
                        with_orders=with_orders)


def test_ods_rows_fit_ingest_schema_and_are_marked():
    rows = plan()
    assert 0 < len(rows) <= 20
    for row in rows:
        parsed = OdsRowIn.model_validate(row)
        assert parsed.ts.date() == DAY and parsed.ts.utcoffset() == timedelta(hours=3)
        assert row["reason"].startswith("эмуляция ОДС: ")
        assert row["decision"] is None or row["decision"] in ACTIONS
        assert row["record_type"] in {*ods.RECORD_BY_ACTION.values(), "осмотр", "закрытие"}
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)


def test_ods_plan_is_deterministic_by_seed():
    assert plan(seed=42) == plan(seed=42)
    assert plan(seed=42) != plan(seed=43)


def test_ods_decision_reason_fits_action_and_existing_decision_is_kept():
    decided = forecast("F00", 0, decision={"action": "reject", "reason_code": "false_alarm"})
    rows = plan([decided, *FORECASTS], count=40)
    first = next(r for r in rows if "прогноз F00" in r["reason"])
    assert (first["record_type"], first["decision"]) == ("отказ", "reject")
    assert "Ложное срабатывание" in first["reason"]
    for row in rows:
        if row["decision"]:
            title = row["reason"].removeprefix("эмуляция ОДС: ").split(";")[0]
            assert row["decision"] in REASON_BY_TITLE[title]["actions"]


def test_ods_chain_is_ordered_and_skips_inactive_and_progressed_forecasts():
    crew = forecast("FX", 1, decision={"action": "dispatch_crew",
                                       "reason_code": "confirmed_by_readings"})
    old = forecast("FOLD", 2, valid_from="2026-06-28T00:00:00+03:00",
                   valid_to="2026-06-29T00:00:00+03:00")
    confirmed = forecast("FWO", 3, work_order_id="WO-1")
    drafted = forecast("FDRAFT", 4, work_order_id="WO-2")
    orders = [{**order("WO-1", "confirmed"), "forecast_ids": ["FWO"]},
              {**order("WO-2", "draft"), "forecast_ids": ["FDRAFT"]}]
    rows = plan([crew, old, confirmed, drafted], orders, count=10)
    kinds = [r["record_type"] for r in rows if "прогноз FX" in r["reason"]]
    assert kinds and kinds == ["выезд", "осмотр", "закрытие"][:len(kinds)]  # полночь режет хвост
    assert not any("FOLD" in r["reason"] or "FWO" in r["reason"] for r in rows)
    assert any("прогноз FDRAFT" in r["reason"] for r in rows)  # черновик создал расчёт


def test_ods_count_caps_forecast_rows_only():
    card = {**order("WO-1", "completed"), "object": {"id": "9201"}, "forecast_ids": ["F01"],
            "history": [
                {"to_status": "draft", "at": "2026-09-28T07:00:00+00:00"},
                {"to_status": "confirmed", "at": "2026-09-28T09:00:00+03:00"},
                {"to_status": "in_progress", "at": "2026-09-28T09:15:30.123+03:00"},
                {"to_status": "completed", "at": "2026-09-28T09:40:00+00:00"}]}
    rows = plan(orders=[card], count=0)
    assert [(r["ts"], r["obj_id"], r["record_type"], r["decision"]) for r in rows] == [
        ("2026-06-30T09:15:30+03:00", "9201", "выезд", "dispatch_crew"),
        ("2026-06-30T12:40:00+03:00", "9201", "закрытие", None)]
    assert all("WO-1" in r["reason"] for r in rows)
    assert len([r for r in plan(orders=[card], count=5) if "WO-1" not in r["reason"]]) <= 5
    archive = plan(orders=[card], count=40, with_orders=False)  # день не «сегодня» стенда
    assert archive and not any("WO-1" in r["reason"] or "прогноз F01" in r["reason"]
                               for r in archive)


def test_ods_due_uses_moscow_time_of_day():
    rows = [ods.record(datetime(2026, 6, 30, hour, 0, tzinfo=ods.MSK), "1", "отказ",
                       "reject", "x") for hour in (8, 12, 18)]
    at_0930_msk = datetime(2026, 9, 28, 6, 30, tzinfo=UTC)
    assert [r["ts"][11:13] for r in ods.due(rows, DAY, at_0930_msk)] == ["08"]
    assert len(ods.due(rows, DAY, datetime(2026, 9, 28, 12, 0, tzinfo=ods.MSK))) == 2


def test_ods_run_once_reads_as_user_and_posts_with_key(capsys):
    server = FakeServer(forecasts=FORECASTS)
    args = argparse.Namespace(day=DAY, seed=42, count=20, dry_run=False)
    assert ods.run_once(server.reader, server.writer, args, VOCAB) == 0
    posts = [c for c in server.calls if c[1] == "POST"]
    assert posts and all(c[:3] == ("writer", "POST", "/ingest/ods-journal") for c in posts)
    assert server.ods_rows == plan()
    ods.run_once(server.reader, server.writer, args, VOCAB)
    assert "принято 0, дублей" in capsys.readouterr().out


def test_ods_loop_posts_due_rows_once():
    server = FakeServer(forecasts=FORECASTS)
    args = argparse.Namespace(day=DAY, seed=42, count=20, dry_run=False, interval=60)
    passes = iter([None, KeyboardInterrupt])

    def sleep(_seconds):
        if next(passes) is KeyboardInterrupt:
            raise KeyboardInterrupt

    noon = datetime(2026, 9, 28, 12, 0, tzinfo=ods.MSK)
    assert ods.run_loop(server.reader, server.writer, args, VOCAB, now=lambda: noon,
                        sleep=sleep) == 0
    assert server.ods_rows == [r for r in plan() if r["ts"] <= "2026-06-30T12:00:00+03:00"]
    assert len([c for c in server.calls if c[1] == "POST"]) == 1  # второй проход: слать нечего


# --- против приложения ----------------------------------------------------------------

class ClientApi:
    """TestClient в интерфейсе _api.Api.call: путь от /api/v1, ApiError на 4xx и 5xx."""

    def __init__(self, client) -> None:
        self.client = client

    def call(self, method, path, body=None, *, timeout=None):
        resp = self.client.request(method, API + path, json=body)
        if resp.status_code >= 400:
            raise _api.ApiError(method, path, resp.status_code, resp.text)
        return resp.status_code, (resp.json() if resp.content else None)


def test_ods_emulator_against_app(ran, login, integration, db):
    reader, writer = ClientApi(login("dispatcher")), ClientApi(integration)
    day = date(2026, 6, 16)  # прогнозы прогона 15.06 действуют с 16.06
    forecasts, orders, is_today = ods.collect(reader, day)
    assert not is_today  # DEMO_TODAY тестов — 30.06
    rows = ods.plan_day(day, forecasts, orders, seed=42, count=20, vocab=VOCAB,
                        with_orders=is_today)

    assert rows
    assert ods.post(writer, rows) == {"accepted": len(rows), "duplicates": 0, "rejected": 0}
    assert ods.post(writer, rows) == {"accepted": 0, "duplicates": len(rows), "rejected": 0}
    assert db.scalar(select(func.count()).select_from(models.OdsRecord)) == len(rows)


def test_helpdesk_emulator_against_app(ran, login, integration):
    dispatcher = login("dispatcher")
    draft = dispatcher.get(f"{API}/work-orders?status=draft").json()["items"][0]
    resp = dispatcher.patch(f"{API}/work-orders/{draft['id']}",
                            json={"expected_status": "draft", "status": "confirmed"})
    assert resp.status_code == 200, resp.text
    writer = ClientApi(integration)
    _, me = writer.call("GET", "/me")
    desk = helpdesk.Helpdesk(ClientApi(login("technician")), writer,
                             perms=me["permissions"], vocab=VOCAB)

    assert [(s.to_status, s.result) for s in desk.step(wait=False)] == [("in_progress", "moved")]
    assert [(s.to_status, s.result) for s in desk.step(wait=False)] == [("completed", "moved")]
    assert desk.step(wait=False) == []  # остались черновики: их подтверждает человек
    history = dispatcher.get(f"{API}/work-orders/{draft['id']}").json()["history"]
    assert [(h["to_status"], h["author"]) for h in history[-2:]] == [
        ("in_progress", "integration"), ("completed", "integration")]
    assert all(h["reason"].startswith("эмуляция help desk") for h in history[-2:])

    forecasts, orders, is_today = ods.collect(ClientApi(dispatcher), DAY)
    rows = ods.plan_day(DAY, forecasts, orders, seed=42, count=0, vocab=VOCAB,
                        with_orders=is_today)
    assert is_today
    assert [(r["record_type"], r["decision"]) for r in rows] == [("выезд", "dispatch_crew"),
                                                                ("закрытие", None)]
    assert ods.post(writer, rows)["accepted"] == 2
