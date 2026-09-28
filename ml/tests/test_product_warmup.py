"""Прогрев C1 при старте (ML2-10): заглушку не трогает, /health и /score не ждут,
ошибка шага пишется в журнал и не мешает остальным шагам и сервису."""
import datetime as dt
import logging
import pickle
import threading

import pytest
from fastapi.testclient import TestClient

from mkl import address, product_api, rule_head, serve, store
from mkl.config import Paths
from mkl.product_api import create_app
from mkl.product_contract import ReadyResponse, ScoreResponse

DAY = dt.date(2026, 6, 30)
LINK = {"label": "label_link", "variant": "L9c", "horizon_days": 1,
        "unknown_in_budget": True, "operating_min_precision": 0.7,
        "max_model_lag_days": 8, "feature_set": "sensor"}
RULE = {"serving_rule": "n_bad_w7", "feature_set": "sensor"}


def _ready(asof):
    return ReadyResponse(status="ready", asof=asof, source="live")


def _warmed_app(monkeypatch, warmup):
    monkeypatch.setattr(product_api, "_real_warmup", warmup)
    monkeypatch.setattr(product_api, "_real_ready", _ready)
    return create_app("real")


def test_stub_does_not_warm_up(monkeypatch):
    calls = []
    monkeypatch.setattr(product_api, "_real_warmup",
                        lambda lock: calls.append(lock) or ({}, []))
    app = create_app("stub")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert "прогрев" not in client.get("/ready").json()["detail"]
    assert calls == []
    assert app.state.warmup["status"] == "off"
    assert app.state.warmup["thread"] is None


def test_health_and_score_do_not_wait_for_warmup(monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(lock):
        started.set()
        release.wait(10)
        return {"шаг": 0.1}, []

    app = _warmed_app(monkeypatch, slow)
    monkeypatch.setattr(product_api, "_real_score",
                        lambda req: ScoreResponse(asof=req.asof, source="live", heads={}))
    try:
        with TestClient(app) as client:
            assert started.wait(5)
            health = client.get("/health")
            assert health.status_code == 200 and health.json()["mode"] == "real"
            score = client.post("/api/v1/score", json={"asof": DAY.isoformat()})
            assert score.status_code == 200, score.text
            ready = client.get("/ready").json()
            # Все три ответа пришли, пока прогрев ещё стоит на release.
            assert app.state.warmup["status"] == "running"
            assert ready["status"] == "ready"
            assert "идёт прогрев" in ready["detail"]

            release.set()
            app.state.warmup["thread"].join(5)
            assert app.state.warmup["status"] == "done"
            assert client.get("/ready").json()["detail"] is None
    finally:
        release.set()


def test_warmup_error_is_logged_and_service_keeps_working(monkeypatch, caplog):
    def broken(lock):
        raise RuntimeError("битый бандл")

    app = _warmed_app(monkeypatch, broken)
    with caplog.at_level(logging.ERROR, logger="mkl.product_api"), TestClient(app) as client:
        app.state.warmup["thread"].join(5)
        assert client.get("/health").status_code == 200
        ready = client.get("/ready").json()
    assert app.state.warmup["status"] == "failed"
    assert ready["status"] == "ready"
    assert "RuntimeError: битый бандл" in ready["detail"]
    assert any("warmup failed" in r.getMessage() for r in caplog.records)


def _artifact(threshold_end: dt.date) -> dict:
    return {"model": None, "iso": None, "features": [], "threshold": 0.5,
            "saved_at": f"saved-{threshold_end}",
            "metadata": {"head": "A_link", "threshold_end": threshold_end.isoformat()}}


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Каталог моделей A_link: два датированных, основной и один с чужой датой внутри."""
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(serve, "PATHS", Paths(models=models))
    for end in (dt.date(2026, 6, 15), dt.date(2026, 6, 23)):
        serve.model_path("A_link", end).write_bytes(pickle.dumps(_artifact(end)))
    serve.model_path("A_link").write_bytes(pickle.dumps(_artifact(DAY)))
    serve.model_path("A_link", dt.date(2026, 6, 7)).write_bytes(
        pickle.dumps(_artifact(dt.date(2026, 6, 20))))
    monkeypatch.setattr(serve, "load_heads", lambda: {"A_link": LINK, "D": RULE})
    monkeypatch.setattr(product_api, "_last_feature_day", lambda: DAY)
    for name in ("_by_channel", "_by_object", "_segment_has_picket",
                 "_objects_without_picket"):
        monkeypatch.setattr(address, name, lambda: None)
    product_api._artifact_info.cache_clear()
    yield models
    product_api._artifact_info.cache_clear()


def test_warmup_steps_are_isolated_and_only_the_rule_takes_the_lock(bundle, monkeypatch):
    lock = threading.Lock()
    seen = []
    monkeypatch.setattr(rule_head, "artifact", lambda head, cfg, day: seen.append(
        ("rule", head, day, lock.locked())))
    # Фичестор прогрев не читает: процесс его не кэширует (докстринг _real_warmup).
    monkeypatch.setattr(store, "read_slice", lambda *a, **kw: seen.append("slice"))

    timings, failed = product_api._real_warmup(lock)

    # Битый датированный файл не остановил остальные: три артефакта в кэше.
    assert product_api._artifact_info.cache_info().currsize == 3
    assert len(failed) == 1 and failed[0].startswith("A_link@2026-06-07.pkl: ValueError")
    assert {"импорт", "A_link.pkl", "A_link@2026-06-15.pkl", "A_link@2026-06-23.pkl",
            "D: порог правила", "справочник адресов"} <= set(timings)
    assert seen == [("rule", "D", DAY, True)]
    assert not lock.locked()
