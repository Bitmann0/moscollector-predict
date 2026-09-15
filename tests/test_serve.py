import numpy as np
import polars as pl

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
    assert set(heads) == {"A", "A_strict", "A_deg", "A_prime", "B", "C", "D", "E"}
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


def test_per_object_budget_falls_back_without_obj_column():
    df = pl.DataFrame({"ch": [1, 2, 3], "risk": [0.9, 0.5, 0.1]})
    out = serve._apply_budget(df, budget=1, per_object=True)
    assert out["alert"].sum() == 1
