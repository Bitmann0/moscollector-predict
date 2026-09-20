import numpy as np
import polars as pl
import pytest

from mkl import serve


def test_apply_budget_selects_exactly_budget_rows():
    df = pl.DataFrame({"ch": range(100), "risk": np.linspace(0, 1, 100)})
    out = serve._apply_budget(df, budget=10)
    assert out["alert"].sum() == 10


def test_alerts_are_the_highest_risk_rows():
    df = pl.DataFrame({"ch": range(100), "risk": np.linspace(0, 1, 100)})
    out = serve._apply_budget(df, budget=10)
    assert out.filter(pl.col("alert"))["risk"].min() >= \
           out.filter(~pl.col("alert"))["risk"].max()


def test_budget_larger_than_rows_alerts_everything():
    df = pl.DataFrame({"ch": [1, 2], "risk": [0.1, 0.9]})
    assert serve._apply_budget(df, budget=99)["alert"].sum() == 2


def test_empty_frame_produces_no_alerts():
    df = pl.DataFrame({"ch": [], "risk": []}, schema={"ch": pl.Int64, "risk": pl.Float64})
    out = serve._apply_budget(df, budget=5)
    assert out.height == 0 and "alert" in out.columns


def test_heads_config_lists_every_head():
    heads = serve.load_heads()
    assert set(heads) == {"A_link", "A", "A_strict", "A_deg", "A_prime",
                          "B", "C", "C_armed", "D", "E"}


def test_link_and_failure_are_separate_heads():
    """Прежняя метка объединяла отказ и молчание, и 98.89% её позитивов давала
    ветка молчания. Это разные события с разной ценой выезда."""
    heads = serve.load_heads()
    assert heads["A_link"]["label"] != heads["A"]["label"]
    assert heads["A_link"]["variant"] == "L9"
    assert heads["A"]["variant"] == "L6"


def test_armed_variant_of_intrusion_is_a_separate_head():
    """Тревога при снятой охране — проход персонала. Голова, названная
    «несанкционированный доступ», считала его позитивом наравне с нарушителем."""
    heads = serve.load_heads()
    assert heads["C_armed"]["armed_only"] is True
    assert not heads["C"].get("armed_only")
    for name, cfg in heads.items():
        assert {"entity", "feature_set", "label", "horizon_days",
                "embargo_days", "budget_per_day"} <= cfg.keys()


def test_wear_head_has_wider_embargo_than_horizon():
    heads = serve.load_heads()
    for cfg in heads.values():
        assert cfg["embargo_days"] >= cfg["horizon_days"] + 30


def test_per_object_budget_spreads_alerts():
    """Глобальная отсечка сажает все алерты на худшие объекты."""
    df = pl.DataFrame({
        "obj": ["A"] * 5 + ["B"] * 5,
        "ch": list(range(10)),
        "risk": [0.9, 0.89, 0.88, 0.87, 0.86, 0.1, 0.09, 0.08, 0.07, 0.06],
    })
    glob = serve._apply_budget(df, budget=4, per_object=False)
    assert set(glob.filter(pl.col("alert"))["obj"].to_list()) == {"A"}

    per = serve._apply_budget(df, budget=4, per_object=True)
    by_obj = per.filter(pl.col("alert")).group_by("obj").len().sort("obj")
    assert by_obj["obj"].to_list() == ["A", "B"]
    assert by_obj["len"].to_list() == [2, 2]


def test_per_object_budget_without_obj_is_an_error_not_a_silent_fallback():
    """Молчаливый откат к глобальной отсечке держал три головы из восьми в
    режиме, который они сами в конфигурации называют неприемлемым: score()
    проецировал таблицу на entity = [ch, day], колонка obj терялась, и ветка
    per_object никогда не выполнялась.
    """
    df = pl.DataFrame({"ch": [1, 2, 3], "risk": [0.9, 0.5, 0.1]})
    with pytest.raises(ValueError, match="obj"):
        serve._apply_budget(df, budget=1, per_object=True)


def test_budget_is_never_exceeded_on_ties():
    """На ступенчатом выходе изотоники пороговое значение делят десятки строк.
    Отсечка через `risk >= thr` выдавала до 1.92 бюджета — обещание «не больше
    20 выездов в сутки» не выполнялось.
    """
    df = pl.DataFrame({"ch": range(50), "risk": [0.5] * 50})
    out = serve._apply_budget(df, budget=10)
    assert out["alert"].sum() == 10


def test_tied_rows_are_broken_deterministically():
    df = pl.DataFrame({"obj": ["B"] * 5 + ["A"] * 5, "ch": list(range(10)),
                       "risk": [0.5] * 10})
    first = serve._apply_budget(df, budget=3).filter(pl.col("alert"))["ch"].to_list()
    shuffled = serve._apply_budget(
        df.sample(fraction=1.0, shuffle=True, seed=3), budget=3
    ).filter(pl.col("alert"))["ch"].to_list()
    assert sorted(first) == sorted(shuffled)


def test_every_tuned_head_has_a_gpu_equivalent():
    """Голова с подобранными параметрами LightGBM обязана иметь эквивалент по
    ёмкости для GPU-бэкендов. Иначе при переключении бэкенда она молча получит
    умолчания вдвое меньшей модели, и замер будет мерить ёмкость, а не бэкенд:
    ровно так возник мнимый разрыв в 4.4 пункта точности на голове A_link.
    """
    from mkl import train
    for name, cfg in serve.load_heads().items():
        if not cfg.get("params"):
            continue
        for backend in ("xgb", "cat"):
            assert train.params_for(cfg, backend), (
                f"{name}: нет params_{backend} при заданных params")
