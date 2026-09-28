"""Датированные артефакты окна демо (ML1-05b): отсечка данных, план окна, выбор по asof.

Всё на синтетике: крошечные панели, эпизоды и артефакты с моделью-константой.
"""
import datetime as dt
import os
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import numpy as np
import polars as pl
import pytest

from conftest import PANEL_SCHEMA, insert_day
from mkl import api, cv, db, labels, product_api, rule_head, serve, states
from mkl.config import Paths

JUNE = [dt.date(2026, 6, 1) + dt.timedelta(days=i) for i in range(30)]
LINK = {"label": "label_link", "variant": "L9c", "horizon_days": 1,
        "embargo_days": 31, "unknown_in_budget": True,
        "operating_min_precision": 0.5, "max_model_lag_days": 8,
        "entity": ["ch", "day"], "feature_set": "sensor", "budget_per_day": 2,
        "direction": "sensor_failure", "title": "t", "product_status": "pilot"}


class _Const:
    """Модель-константа; объявлена на уровне модуля, чтобы пиклиться."""

    def __init__(self, p: float):
        self.p = p

    def predict_proba(self, X):
        return np.column_stack([np.zeros(len(X)), np.full(len(X), self.p)])


def _artifact(threshold_end: dt.date, p: float = 0.5, cfg: dict = LINK) -> dict:
    return {"model": _Const(p), "iso": None, "features": ["x"], "threshold": 0.1,
            "feature_signature": None, "saved_at": f"saved-{threshold_end}",
            "metadata": {"head": "A_link", "label": cfg["label"],
                         "variant": cfg["variant"],
                         "horizon_days": cfg["horizon_days"],
                         "unknown_in_budget": cfg["unknown_in_budget"],
                         "operating_min_precision": cfg["operating_min_precision"],
                         "threshold_end": threshold_end.isoformat()}}


def _write(path: Path, art: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(art))
    return path


@pytest.fixture
def models(tmp_path, monkeypatch):
    """Каталог моделей во временной папке и конфигурация одной головы A_link."""
    monkeypatch.setattr(serve, "PATHS", Paths(models=tmp_path / "models"))
    monkeypatch.setattr(serve, "load_heads", lambda: {"A_link": LINK})
    product_api._artifact_info.cache_clear()
    return tmp_path / "models"


PLAN_ENDS = [dt.date(2026, 5, 31), dt.date(2026, 6, 8),
             dt.date(2026, 6, 16), dt.date(2026, 6, 24)]


def _window_models(models: Path) -> None:
    for end in PLAN_ENDS:
        _write(serve.model_path("A_link", end), _artifact(end))
    _write(serve.model_path("A_link"), _artifact(dt.date(2026, 6, 29)))


# --- выбор артефакта по asof ---------------------------------------------------

@pytest.mark.parametrize("asof, expected", [
    (dt.date(2026, 6, 1), dt.date(2026, 5, 31)),
    (dt.date(2026, 6, 8), dt.date(2026, 5, 31)),
    (dt.date(2026, 6, 9), dt.date(2026, 6, 8)),
    (dt.date(2026, 6, 16), dt.date(2026, 6, 8)),
    (dt.date(2026, 6, 17), dt.date(2026, 6, 16)),
    (dt.date(2026, 6, 25), dt.date(2026, 6, 24)),
    (dt.date(2026, 6, 30), dt.date(2026, 6, 24)),
    (dt.date(2026, 7, 2), dt.date(2026, 6, 24)),
])
def test_selection_takes_latest_dated_artifact_before_asof(models, asof, expected):
    _window_models(models)
    assert serve.artifact_path("A_link", asof) == serve.model_path("A_link", expected)


@pytest.mark.parametrize("asof", [
    dt.date(2026, 5, 31),   # окно порога кончается в сам день расчёта: задержка 0
    dt.date(2026, 7, 3),    # последний датированный старше 8 суток
    None,                   # без дня расчёта — прежний {head}.pkl
])
def test_selection_falls_back_to_undated_artifact(models, asof):
    _window_models(models)
    assert serve.artifact_path("A_link", asof) == serve.model_path("A_link")


def test_selection_prefers_the_freshest_of_overlapping_artifacts(models):
    for end in (dt.date(2026, 6, 3), dt.date(2026, 6, 5), dt.date(2026, 6, 8)):
        _write(serve.model_path("A_link", end), _artifact(end))
    assert serve.artifact_path("A_link", dt.date(2026, 6, 10)) == \
        serve.model_path("A_link", dt.date(2026, 6, 8))
    # 08.06 для дня 08.06 — задержка 0, берётся предыдущий.
    assert serve.artifact_path("A_link", dt.date(2026, 6, 8)) == \
        serve.model_path("A_link", dt.date(2026, 6, 5))


def test_selection_skips_artifact_beyond_lag_even_if_latest(models):
    """Самый поздний датированный до asof, но старше лага, не берётся."""
    _write(serve.model_path("A_link", dt.date(2026, 6, 1)), _artifact(dt.date(2026, 6, 1)))
    assert serve.artifact_path("A_link", dt.date(2026, 6, 10)) == serve.model_path("A_link")
    assert serve.artifact_path("A_link", dt.date(2026, 6, 9)) == \
        serve.model_path("A_link", dt.date(2026, 6, 1))


def test_selection_ignores_other_heads_and_foreign_names(models):
    _write(models / "A@2026-06-05.pkl", _artifact(dt.date(2026, 6, 5)))
    _write(models / "A_link_old@2026-06-05.pkl", _artifact(dt.date(2026, 6, 5)))
    _write(models / "A_link@latest.pkl", _artifact(dt.date(2026, 6, 5)))
    assert serve.dated_model_paths("A_link") == {}
    assert serve.dated_model_paths("A") == {dt.date(2026, 6, 5): models / "A@2026-06-05.pkl"}
    assert serve.has_model("A_link") is False
    assert serve.has_model("A") is True


def test_dated_name_must_match_metadata(models):
    path = _write(serve.model_path("A_link", dt.date(2026, 6, 8)),
                  _artifact(dt.date(2026, 6, 16)))
    with pytest.raises(ValueError, match="в имени 2026-06-08"):
        serve.load_artifact(path)


def test_pilot_status_reports_the_chosen_artifact(models):
    _window_models(models)
    art = product_api._pilot_artifact("A_link", dt.date(2026, 6, 12))
    assert art["metadata"]["threshold_end"] == "2026-06-08"
    assert art["saved_at"] == "saved-2026-06-08"
    art = product_api._pilot_artifact("A_link", dt.date(2026, 6, 30))
    assert art["metadata"]["threshold_end"] == "2026-06-24"


def test_fallback_outside_lag_is_rejected(models):
    """Без датированного артефакта для 20.06 полная модель (порог по 29.06)
    проверку задержки не проходит, и голова отвечает stale, а не будущим."""
    _write(serve.model_path("A_link"), _artifact(dt.date(2026, 6, 29)))
    _write(serve.model_path("A_link", dt.date(2026, 6, 8)), _artifact(dt.date(2026, 6, 8)))
    with pytest.raises(ValueError, match="outside the 1..8 day pilot lag"):
        product_api._pilot_artifact("A_link", dt.date(2026, 6, 20))
    art = product_api._pilot_artifact("A_link", dt.date(2026, 6, 30))
    assert art["metadata"]["threshold_end"] == "2026-06-29"


def test_score_uses_the_same_artifact_as_the_status(models, monkeypatch):
    """Риск считает та модель, чьи метаданные попали в ответ."""
    _write(serve.model_path("A_link", dt.date(2026, 6, 8)),
           _artifact(dt.date(2026, 6, 8), p=0.2))
    _write(serve.model_path("A_link", dt.date(2026, 6, 16)),
           _artifact(dt.date(2026, 6, 16), p=0.8))

    def read_slice(name, start, end, *args, **kwargs):
        return pl.DataFrame({"ch": [1, 2], "obj": ["A", "B"],
                             "day": [start, start], "x": [0.0, 1.0]})

    monkeypatch.setattr(serve.store, "read_slice", read_slice)
    early, art, _ = serve.score_with_internals("A_link", dt.date(2026, 6, 12))
    assert early["risk"].to_list() == [0.2, 0.2]
    assert art["metadata"]["threshold_end"] == "2026-06-08"
    late = serve.score("A_link", dt.date(2026, 6, 20))
    assert late["risk"].to_list() == [0.8, 0.8]


def test_save_writes_dated_path_and_keeps_undated(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "PATHS", Paths(models=tmp_path))
    monkeypatch.setattr(serve, "load_heads", lambda: {"A_link": {"feature_set": "sensor"}})
    monkeypatch.setattr(serve.store, "load_registry", lambda: {})
    monkeypatch.setattr(serve, "feature_signature", lambda _: "signature")
    undated = serve.save("A_link", model="full", iso=None, feature_names=["x"])
    dated = serve.save("A_link", model="june", iso=None, feature_names=["x"],
                       path=serve.model_path("A_link", dt.date(2026, 6, 8)))
    assert undated == tmp_path / "A_link.pkl"
    assert dated == tmp_path / "A_link@2026-06-08.pkl"
    assert pickle.loads(undated.read_bytes())["model"] == "full"
    assert sorted(p.name for p in tmp_path.iterdir()) == \
        ["A_link.pkl", "A_link@2026-06-08.pkl"]


# --- кэши -----------------------------------------------------------------------

def test_api_cache_generation_changes_per_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "PATHS", SimpleNamespace(
        root=tmp_path, features=tmp_path / "features",
        interim=tmp_path / "interim", models=tmp_path / "models"))
    _write(tmp_path / "models" / "A_link.pkl", {})
    before = api._cache_generation()
    dated = _write(tmp_path / "models" / "A_link@2026-06-08.pkl", {})
    added = api._cache_generation()
    assert added != before
    stamp = dated.stat().st_mtime_ns + 10**9
    os.utime(dated, ns=(stamp, stamp))
    assert api._cache_generation() != added


def test_artifact_info_cache_is_per_file(models):
    _window_models(models)
    first = serve.model_path("A_link", dt.date(2026, 6, 8))
    second = serve.model_path("A_link", dt.date(2026, 6, 16))
    got = [product_api._artifact_info(p, p.stat().st_mtime_ns)["metadata"]["threshold_end"]
           for p in (first, second)]
    assert got == ["2026-06-08", "2026-06-16"]


# --- план окна --------------------------------------------------------------------

def test_threshold_end_follows_horizon_like_live_windows():
    assert cv.live_threshold_end(dt.date(2026, 6, 30), 1, 31) == dt.date(2026, 6, 29)
    assert cv.live_threshold_end(dt.date(2026, 6, 30), 7, 37) == dt.date(2026, 6, 23)


def test_window_plan_covers_june_with_lag_8():
    plan = cv.window_plan(JUNE[0], JUNE[-1], horizon_days=1, embargo_days=31,
                          max_lag_days=8)
    assert [item["threshold_end"] for item in plan] == PLAN_ENDS
    assert [item["source_end"] for item in plan] == [
        dt.date(2026, 6, 1), dt.date(2026, 6, 9), dt.date(2026, 6, 17), dt.date(2026, 6, 25)]
    # 30 суток при лаге 8: меньше четырёх моделей не хватит.
    assert len(plan) == -(-len(JUNE) // 8)
    for day in JUNE:
        assert any(0 < (day - item["threshold_end"]).days <= 8 for item in plan), day


def test_window_plan_files_are_selected_for_every_june_day(models):
    plan = cv.window_plan(JUNE[0], JUNE[-1], 1, 31, serve.max_lag_days(LINK))
    for item in plan:
        end = item["threshold_end"]
        _write(serve.model_path("A_link", end), _artifact(end))
    for day in JUNE:
        path = serve.artifact_path("A_link", day)
        art = serve.load_artifact(path)
        serve.validate_pilot_artifact("A_link", art, day, max_lag_days=8)


def test_d_rule_threshold_is_within_lag_on_every_june_day():
    """D — правило, порог пересчитывается по понедельникам: датированные модели
    ему не нужны, задержка в июне держится в 8..14 сутках."""
    cfg = serve.load_heads()["D"]
    assert rule_head.is_rule(cfg)
    for day in JUNE:
        refresh = rule_head.refresh_day(day)
        end = cv.live_windows(refresh - dt.timedelta(days=cfg["horizon_days"] + 1),
                              cfg["embargo_days"])["threshold_end"]
        assert 8 <= (day - end).days <= serve.max_lag_days(cfg), day


def test_train_latest_output_path(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    sys.modules.pop("train_latest", None)
    import train_latest

    monkeypatch.setattr(serve, "PATHS", Paths(models=tmp_path / "models"))
    end = dt.date(2026, 6, 8)
    assert train_latest.output_path(None, "A_link", end, dated=False) == \
        tmp_path / "models" / "A_link.pkl"
    assert train_latest.output_path(None, "A_link", end, dated=True) == \
        tmp_path / "models" / "A_link@2026-06-08.pkl"
    assert train_latest.output_path(tmp_path / "out", "A_link", end, dated=False) == \
        tmp_path / "out" / "A_link@2026-06-08.pkl"
    assert train_latest.output_path(tmp_path / "x.pkl", "A_link", end, dated=True) == \
        tmp_path / "x.pkl"


def test_train_latest_window_plan_prints_and_trains_nothing(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    sys.modules.pop("train_latest", None)
    import json

    import train_latest

    monkeypatch.setattr(train_latest, "refresh", lambda *a, **kw: pytest.fail("trained"))
    # The CLI contract test must not depend on whether customer data happens
    # to be mounted locally. Missing-day behavior has its own test below.
    monkeypatch.setattr(train_latest, "_missing_panel_days", lambda *a: frozenset())
    monkeypatch.setattr(sys, "argv", ["train_latest.py", "A_link", "D", "--window-plan"])
    train_latest.main()
    out = capsys.readouterr().out.splitlines()
    plan = json.loads(out[0])
    assert plan["max_model_lag_days"] == 8
    assert [a["source_end"] for a in plan["artifacts"]] == [
        "2026-06-01", "2026-06-09", "2026-06-17", "2026-06-25"]
    assert out[1].startswith("D: правило n_bad_w7")


# --- обрезка источников по source_end ---------------------------------------------

def _episode(ch, start, end, obj="A"):
    t0, t1 = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    return (ch, obj, "Датчик дыма", t0, t1, int((t1 - t0).total_seconds()), 2,
            "Неисправен", 60, False)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Панель, эпизоды и групповые отказы в parquet, как их пишут стадии panel и states.

    Четыре канала объекта A ложатся в одно пятиминутное окно 10.06, но эпизод
    канала 4 кончается 11.06. Канал 11 молчит 10–11.06 и возвращается 12.06.
    """
    con = duckdb.connect(":memory:")
    con.execute(PANEL_SCHEMA)
    for day in [dt.date(2026, 5, 20) + dt.timedelta(days=i) for i in range(24)]:
        insert_day(con, ch=10, day=day.isoformat())
        if day <= dt.date(2026, 6, 9) or day >= dt.date(2026, 6, 12):
            insert_day(con, ch=11, day=day.isoformat())
    con.execute("""CREATE TABLE episodes (ch BIGINT, obj VARCHAR, stype VARCHAR,
        t_start TIMESTAMP, t_end TIMESTAMP, dur_s BIGINT, n_events BIGINT,
        states VARCHAR, gap_before_s BIGINT, is_group BOOLEAN)""")
    rows = [_episode(ch, "2026-06-10 10:00", "2026-06-10 12:00") for ch in (1, 2, 3)]
    rows.append(_episode(4, "2026-06-10 10:01", "2026-06-11 01:00"))
    rows.append(_episode(5, "2026-06-12 08:00", "2026-06-12 10:00"))
    con.executemany("INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    states.build_group_outages(con)
    states.mark_group_episodes(con)
    for name in ("daily_channel", "episodes", "group_outages"):
        con.execute(f"COPY {name} TO '{tmp_path / (name + '.parquet')}' (FORMAT PARQUET)")
    con.close()
    monkeypatch.setattr(db, "PATHS", Paths(interim=tmp_path, tmp=tmp_path))
    return tmp_path


def _attached(source_end):
    con = duckdb.connect(":memory:")
    db.attach_label_sources(con, source_end)
    return con


def test_uncut_sources_are_the_parquet_files(sources):
    con = _attached(None)
    assert con.execute("SELECT max(day) FROM daily_channel").fetchone()[0] == dt.date(2026, 6, 12)
    assert con.execute("SELECT count(*) FROM group_outages").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM episodes WHERE is_group").fetchone()[0] == 4


def test_cut_far_in_future_rebuilds_the_same_groups(sources):
    full, late = _attached(None), _attached(dt.date(2027, 1, 1))
    query = "SELECT ch, t_start, is_group FROM episodes ORDER BY ch"
    assert late.execute(query).fetchall() == full.execute(query).fetchall()
    query = "SELECT obj, bucket, t_start, t_end, n_channels FROM group_outages"
    assert late.execute(query).fetchall() == full.execute(query).fetchall()


def test_cut_drops_days_and_episodes_ending_after_source_end(sources):
    con = _attached(dt.date(2026, 6, 10))
    assert con.execute("SELECT max(day) FROM daily_channel").fetchone()[0] == dt.date(2026, 6, 10)
    # Эпизод канала 4 начался 10.06, но его длина известна только 11.06.
    kept = con.execute("SELECT ch FROM episodes ORDER BY ch").fetchall()
    assert [row[0] for row in kept] == [1, 2, 3]
    # Без него в окне три канала из четырёх нужных: группы на 10.06 ещё нет.
    assert con.execute("SELECT count(*) FROM group_outages").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM episodes WHERE is_group").fetchone()[0] == 0


def test_labels_after_cut_do_not_see_days_after_source_end(sources):
    """L9c по полной панели знает, что канал 11 вернулся 12.06, и ставит 09.06
    позитив. К концу 10.06 возврата ещё нет: строка 09.06 — последняя у канала,
    исход неизвестен, и в метке её быть не должно."""
    full = _attached(None)
    labels.build_for_head(full, LINK)
    assert full.execute("SELECT y FROM label_link WHERE ch = 11 AND day = DATE '2026-06-09'"
                        ).fetchone()[0] == 1

    source_end = dt.date(2026, 6, 10)
    cut = _attached(source_end)
    labels.build_for_head(cut, LINK)
    assert cut.execute("SELECT count(*) FROM label_link WHERE ch = 11 AND day = DATE '2026-06-09'"
                       ).fetchone()[0] == 0
    last = cut.execute("SELECT max(day) FROM label_link").fetchone()[0]
    assert last == cv.live_threshold_end(source_end, LINK["horizon_days"], LINK["embargo_days"])


def test_refresh_refuses_source_end_beyond_the_panel(sources, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    sys.modules.pop("train_latest", None)
    import train_latest

    with pytest.raises(ValueError, match="панель кончается 2026-06-12"):
        train_latest.refresh("A_link", LINK, source_end=dt.date(2026, 6, 20))


def test_window_plan_skips_days_without_data():
    """06-01 в журнале пустой: отсечка сдвигается на 05-31, июнь покрыт без дыр."""
    missing = frozenset({dt.date(2026, 6, 1)})
    plan = cv.window_plan(JUNE[0], JUNE[-1], 1, 31, 8, missing)
    assert plan[0]["source_end"] == dt.date(2026, 5, 31)
    assert not {p["source_end"] for p in plan} & missing
    for day in JUNE:
        assert any(p["valid_from"] <= day <= p["valid_to"] for p in plan), day
