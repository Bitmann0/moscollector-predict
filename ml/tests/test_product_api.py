"""Контракт C1 на заглушке: форма ответов, бюджеты, паузы, no_data, real → 501.

Тест формы для владельцев ML1-03, ML1-04, ML1-05b, ML1-07: real-режим заменяет
_real_* в mkl.product_api, а эти проверки stub-режима остаются зелёными.
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mkl import address, config, contract, explain, guard_weekly, product_stub, workorders
from mkl.product_api import create_app
from mkl.product_contract import (
    DirectionItem,
    Health,
    OutcomeResult,
    ReadyResponse,
    ScoreResponse,
    WeeklyResponse,
)

ML_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ML_ROOT.parent
ASOF = dt.date(2026, 6, 15)          # понедельник внутри демо-окна
NO_DATA = dt.date(2026, 6, 1)


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app("stub"))


def _score(client, asof=ASOF, **body) -> ScoreResponse:
    resp = client.post("/api/v1/score", json={"asof": asof.isoformat(), **body})
    assert resp.status_code == 200, resp.text
    return ScoreResponse.model_validate(resp.json())


def _in_budget(resp: ScoreResponse, head: str) -> list:
    return [a for a in resp.alerts if a.head == head and a.in_budget]


def test_health_stub(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    health = Health.model_validate(resp.json())
    assert health.mode == "stub" and health.schema_version == contract.SCHEMA_VERSION


def test_score_shape_and_budgets(client):
    resp = _score(client)
    assert resp.source == "stub"
    assert set(resp.heads) == {"A_link", "D"}
    assert all(h.result_status in ("ok", "empty_valid") for h in resp.heads.values())
    assert len(_in_budget(resp, "A_link")) <= 20
    assert len(_in_budget(resp, "D")) <= 3
    assert _in_budget(resp, "A_link"), "на 2026-06-15 нужен хотя бы один алерт для теста паузы"
    assert resp.data_snapshot == {"data_last_day": "2026-06-30"}

    start = dt.datetime.combine(dt.date(2026, 6, 16), dt.time())  # C1: без таймзоны
    for a in resp.alerts:
        spec = product_stub.HEADS[a.head]
        assert a.model_version == f"stub-{a.head}-v0"
        assert a.valid_from == start
        assert a.valid_to == start + dt.timedelta(hours=spec.horizon_hours)
        assert a.address.obj_name.startswith("Объект-заглушка")
        assert a.address.obj_parent_name.startswith("Комплекс-заглушка")
        assert a.address.picket_label == f"ПК {a.address.picket:g}"
        entity = {"ch": a.address.channel, "obj": a.address.obj}
        assert a.alert_id == contract.make_alert_id(a.head, entity, ASOF)
        assert a.case_key == contract.make_case_key(a.head, entity)
        if a.in_budget:
            assert 2 <= len(a.factors) <= 3
            assert all(f.label for f in a.factors)
        else:
            assert a.factors == []
    assert all(a.address.sensor_type in product_stub.EQUIPMENT_STYPES
               for a in resp.alerts if a.head == "D")
    for head in ("A_link", "D"):
        ranks = [a.rank for a in resp.alerts if a.head == head]
        assert sorted(ranks) == list(range(1, len(ranks) + 1))


def test_score_coverage(client):
    resp = _score(client)
    cov = {c.head: c for c in resp.coverage}
    ref = json.loads(product_stub.reference_path().read_text(encoding="utf-8"))
    equipment = [c for c in ref["channels"] if c["sensor_type"] in product_stub.EQUIPMENT_STYPES]
    assert cov["A_link"].entities_total == len(ref["channels"])
    assert cov["D"].entities_total == len(equipment)
    for head, c in cov.items():
        assert c.entities_scored == len([a for a in resp.alerts if a.head == head])
        assert c.fraction == round(c.entities_scored / c.entities_total, 4)


def test_work_orders_follow_workorders_rules(client):
    resp = _score(client)
    by_id = {a.alert_id: a for a in resp.alerts}
    budget_ids = {a.alert_id for a in resp.alerts if a.in_budget}
    keys = set()
    for w in resp.work_orders:
        items = [by_id[i] for i in w.alert_ids]
        assert set(w.alert_ids) <= budget_ids
        assert {(a.direction, a.address.obj) for a in items} == {(w.direction, w.obj)}
        keys.add((w.direction, w.obj))
        assert w.obj_name.startswith("Объект-заглушка") and w.obj_parent_name
        assert w.priority == workorders._priority(
            max(a.risk for a in items), min(a.horizon_hours for a in items))
        assert w.order_id == workorders._order_id(ASOF, w.direction, w.obj)
        assert w.n_alerts == len(items)
    assert sum(w.n_alerts for w in resp.work_orders) == len(budget_ids)
    assert len(keys) == len(resp.work_orders)


def test_score_is_deterministic(client):
    body = {"asof": ASOF.isoformat()}
    first = client.post("/api/v1/score", json=body)
    second = client.post("/api/v1/score", json=body)
    assert first.content == second.content


@pytest.mark.parametrize("days_ago, blocked", [(3, True), (7, True), (1, True),
                                               (0, False), (8, False)])
def test_issued_history_cooldown(client, days_ago, blocked):
    base = _score(client)
    target = _in_budget(base, "A_link")[0]
    history = {"A_link": [{"channel": target.address.channel,
                           "sent_day": (ASOF - dt.timedelta(days=days_ago)).isoformat()}],
               "D": []}
    resp = _score(client, issued_histories=history,
                  history_complete_from=(ASOF - dt.timedelta(days=7)).isoformat())
    got = {a.alert_id: a.in_budget for a in resp.alerts}
    assert got[target.alert_id] is (not blocked)
    # Освобождённое место не добирается следующим по риску, остальные не меняются.
    others_before = {a.alert_id: a.in_budget for a in base.alerts if a.alert_id != target.alert_id}
    assert {k: got[k] for k in others_before} == others_before
    assert all(h.detail is None for h in resp.heads.values())


def test_incomplete_journal_is_reported(client):
    resp = _score(client)
    assert all("журнал выданного неполон" in h.detail for h in resp.heads.values())


def test_no_data_day(client):
    resp = _score(client, asof=NO_DATA)
    assert {h.result_status for h in resp.heads.values()} == {"no_data"}
    assert resp.alerts == [] and resp.work_orders == []
    assert all(c.entities_scored == 0 for c in resp.coverage)


def test_heads_subset(client):
    resp = _score(client, heads=["D"])
    assert set(resp.heads) == {"D"} and {a.head for a in resp.alerts} == {"D"}


def test_weekly_monday(client):
    resp = client.get("/api/v1/guard-weekly-inspections", params={"asof": ASOF.isoformat()})
    assert resp.status_code == 200, resp.text
    w = WeeklyResponse.model_validate(resp.json())
    assert w.source == "stub" and w.result_status in ("ok", "empty_valid")
    assert (w.valid_from, w.valid_to, w.next_run) == (
        dt.date(2026, 6, 17), dt.date(2026, 6, 24), dt.date(2026, 6, 22))
    assert w.score_type == "relative_priority_not_probability"
    assert len(w.priorities) <= 2
    for p in w.priorities:
        assert p.recommendation_id == contract.make_alert_id(
            "guard_weekly", {"obj": p.obj, "target": "D+2..D+8"}, ASOF)
        assert p.case_key == contract.make_case_key("guard_weekly", {"obj": p.obj})
        assert p.recent_alarm_days_7 >= w.policy["alarm_days_in_last_7_at_least"]
        assert p.obj_name.startswith("Объект-заглушка")


def test_weekly_not_monday_is_422(client):
    resp = client.get("/api/v1/guard-weekly-inspections", params={"asof": "2026-06-16"})
    assert resp.status_code == 422
    assert "понедельник" in resp.json()["detail"]


def test_weekly_no_data(client):
    resp = client.get("/api/v1/guard-weekly-inspections", params={"asof": NO_DATA.isoformat()})
    assert resp.status_code == 200
    w = WeeklyResponse.model_validate(resp.json())
    assert w.result_status == "no_data" and w.priorities == []


def test_weekly_respects_object_cooldown():
    cooldown = product_stub.WEEKLY_POLICY["same_object_cooldown_days"]
    last: dict[str, dt.date] = {}
    monday = dt.date(2026, 1, 5)
    while monday <= dt.date(2026, 6, 29):
        for p in product_stub.weekly(monday).priorities:
            if p.obj in last:
                assert (monday - last[p.obj]).days > cooldown
            last[p.obj] = monday
        monday += dt.timedelta(days=7)
    assert last, "за полгода ротация должна выдать хоть одну рекомендацию"


def test_outcomes(client):
    body = [{"id": f"id-{i}", "kind": "alert", "head": "A_link", "channel": 9000001 + i,
             "asof": "2026-06-10"} for i in range(40)]
    body.append({"id": "rec-1", "kind": "weekly_recommendation", "head": "guard_weekly",
                 "obj": "9101", "asof": "2026-06-08"})
    resp = client.post("/api/v1/outcomes", json=body)
    assert resp.status_code == 200, resp.text
    got = [OutcomeResult.model_validate(x) for x in resp.json()]
    assert [g.id for g in got] == [b["id"] for b in body]
    assert {g.outcome for g in got} <= {"hit", "miss", "unknown"}
    assert client.post("/api/v1/outcomes", json=body).json() == resp.json()


def test_ready(client):
    r = ReadyResponse.model_validate(client.get("/ready", params={"asof": "2026-06-01"}).json())
    assert r.status == "missing_data" and r.source == "stub"
    r = ReadyResponse.model_validate(client.get("/ready", params={"asof": "2026-06-15"}).json())
    assert r.status == "ready" and r.data_last_day == dt.date(2026, 6, 30)
    assert client.get("/ready").status_code == 200


def test_directions_match_vocabulary(client):
    resp = client.get("/api/v1/directions")
    assert resp.status_code == 200
    items = [DirectionItem.model_validate(x) for x in resp.json()]
    heads = {h.head: (i.direction, h.horizon_hours) for i in items for h in i.heads}
    vocab = json.loads((REPO_ROOT / "contracts" / "vocabularies.json").read_text(encoding="utf-8"))
    expected = {s["head"]: (s["direction"], s["horizon_hours"]) for s in vocab["scenario"]}
    assert heads == expected


def test_real_mode_is_501(monkeypatch):
    monkeypatch.setenv("ML_MODE", "real")
    real = TestClient(create_app())
    assert real.get("/health").json()["mode"] == "real"
    cases = [
        ("post", "/api/v1/score", {"json": {"asof": "2026-06-15"}}, "ML1-03"),
        ("get", "/ready", {"params": {"asof": "2026-06-15"}}, "ML1-03"),
        ("get", "/api/v1/directions", {}, "ML1-03"),
        ("get", "/api/v1/guard-weekly-inspections", {"params": {"asof": "2026-06-15"}}, "ML1-04"),
        ("post", "/api/v1/outcomes", {"json": []}, "ML1-07"),
    ]
    for method, path, kw, task in cases:
        resp = getattr(real, method)(path, **kw)
        assert resp.status_code == 501, (path, resp.text)
        assert task in resp.json()["detail"], path


def test_unknown_mode_fails_fast():
    with pytest.raises(ValueError):
        create_app("prod")


def test_constants_match_ml_sources():
    heads = yaml.safe_load((ML_ROOT / "configs" / "heads.yaml").read_text(encoding="utf-8"))
    for name, spec in product_stub.HEADS.items():
        cfg = heads[name]
        assert cfg["product_status"] == "pilot"
        assert spec.direction == cfg["direction"] and spec.title == cfg["title"]
        assert spec.horizon_hours == int(cfg["horizon_days"]) * 24
        assert spec.budget_per_day == cfg["budget_per_day"]
        assert spec.cooldown_days == cfg["cooldown_days"]
        assert spec.budget_per_object == bool(cfg.get("budget_per_object"))
        assert 1 <= spec.model_lag_days <= cfg["max_model_lag_days"]
    assert product_stub.EQUIPMENT_STYPES == config.EQUIPMENT_STYPES
    assert product_stub.OBJ_KIND_RU == address.OBJ_KIND_RU
    assert product_stub.WORK_TYPE.items() <= workorders.WORK_TYPE.items()
    assert product_stub.WEEKLY_POLICY == {
        "alarm_days_in_last_7_at_least": guard_weekly.MIN_ALARM_DAYS,
        "max_objects_per_week": guard_weekly.BUDGET,
        "same_object_cooldown_days": guard_weekly.COOLDOWN_DAYS}
    for pool in product_stub.FACTOR_POOL.values():
        assert all(explain.FEATURE_LABELS[f] == label for f, label in pool)
    for risk in (0.2, 0.5, 0.79, 0.8, 0.95):
        for horizon in (24, 168):
            assert product_stub._priority(risk, horizon) == workorders._priority(risk, horizon)


def test_contracts_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTRACTS_DIR", str(tmp_path))
    assert product_stub.reference_path() == tmp_path / "synthetic_reference.json"
    monkeypatch.delenv("CONTRACTS_DIR")
    assert product_stub.reference_path() == REPO_ROOT / "contracts" / "synthetic_reference.json"


def test_stub_mode_imports_no_heavy_modules():
    code = ("import sys, mkl.product_api; "
            "heavy = [m for m in ('polars', 'duckdb', 'numpy', 'yaml', 'lightgbm', "
            "'mkl.service', 'mkl.serve', 'mkl.train', 'mkl.config') if m in sys.modules]; "
            "print(heavy); sys.exit(1 if heavy else 0)")
    env = {**os.environ, "PYTHONPATH": str(ML_ROOT / "src")}
    proc = subprocess.run([sys.executable, "-c", code], cwd=ML_ROOT, env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
