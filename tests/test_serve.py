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
                          "B", "C", "D", "E"}
    for name, cfg in heads.items():
        assert {"entity", "feature_set", "label", "horizon_days",
                "embargo_days", "budget_per_day"} <= cfg.keys(), name


def test_link_and_failure_are_separate_heads():
    """Прежняя метка объединяла отказ и молчание, и 98.89% её позитивов давала
    ветка молчания. Это разные события с разной ценой выезда."""
    heads = serve.load_heads()
    assert heads["A_link"]["label"] != heads["A"]["label"]
    assert heads["A_link"]["variant"] == "L9"
    assert heads["A"]["variant"] == "L6"


def test_intrusion_head_counts_only_armed_objects():
    """Тревога при снятой охране — проход персонала, то есть ровно
    санкционированный доступ. Голова, названная «несанкционированный доступ»,
    считала его позитивом наравне с нарушителем: по всей истории состояние
    известно у 86.7% тревог и делит их почти пополам.
    """
    assert serve.load_heads()["C"]["armed_only"] is True


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


def test_rejected_feature_families_do_not_reach_the_heads_that_rejected_them():
    """Замер отверг пять новых семейств на канальных головах — значит они не
    должны туда попадать. Отдельная проверка нужна потому, что имя вне общего
    префикса тихо просачивается: так `share_par_bad` попал в голову подтопления
    уже после того, как контекст комплекса для неё был отклонён.
    """
    heads = serve.load_heads()
    families = ("cusum_", "dev90_", "ewma_", "adi_w90", "cv2_w90")
    for name in ("A_link", "A", "A_strict", "A_deg", "D"):
        drop = set(heads[name].get("drop_feature_prefixes") or [])
        missing = [f for f in families if f not in drop]
        assert not missing, f"{name}: отклонённые семейства не отброшены: {missing}"


def test_accepted_families_are_kept_where_they_won():
    """Обратная сторона: принятое замером нельзя отбрасывать заодно."""
    heads = serve.load_heads()
    drop = set(heads["A_prime"].get("drop_feature_prefixes") or [])
    assert "hhi_" not in drop, "концентрация принята для A' тремя фолдами из трёх"
    assert not (heads["C"].get("drop_feature_prefixes") or []), \
        "для C принято сочетание концентрации и увлажнения"


class _ConstantModel:
    """Заглушка модели: пиклится, потому что объявлена на уровне модуля."""

    def predict_proba(self, X):
        import numpy as np
        return np.column_stack([np.zeros(len(X)), np.full(len(X), 0.5)])


class _FakeArtifactPath:
    def __init__(self, art):
        self._art = art

    def exists(self):
        return True

    def open(self, mode="rb"):
        import io as _io
        import pickle as _p
        return _io.BytesIO(_p.dumps(self._art))


def test_armed_only_head_does_not_score_objects_without_arming_data(monkeypatch):
    """Метка несанкционированного доступа определена только там, где известно
    состояние охраны. На остальных объектах модель выдавала бы риск, обученный
    на другом определении события, — это хуже молчания.
    """
    import datetime as dt

    import polars as pl

    feats = pl.DataFrame({
        "obj": ["1", "2", "3"], "day": [dt.date(2026, 1, 1)] * 3,
        "obj_armed": [1, None, 0], "x": [0.1, 0.2, 0.3],
    })
    art = {"model": _ConstantModel(), "iso": None, "features": ["x"],
           "threshold": None, "feature_signature": None}
    monkeypatch.setattr(serve, "load_heads", lambda: {
        "C": {"entity": ["obj", "day"], "feature_set": "object",
              "label": "label_intrusion", "horizon_days": 1, "embargo_days": 31,
              "budget_per_day": 4, "armed_only": True, "direction": "x",
              "title": "t"}})
    monkeypatch.setattr(serve.store, "latest_snapshot", lambda name: feats)
    monkeypatch.setattr(serve, "model_path", lambda h: _FakeArtifactPath(art))

    got = serve.score("C")
    assert sorted(got["obj"].to_list()) == ["1", "3"], "объект без охраны пропущен"


def test_saved_validation_threshold_gates_alerts(monkeypatch):
    import datetime as dt

    feats = pl.DataFrame({"obj": ["1", "2"], "day": [dt.date(2026, 1, 1)] * 2,
                          "x": [0.1, 0.2]})
    art = {"model": _ConstantModel(), "iso": None, "features": ["x"],
           "threshold": 0.7, "feature_signature": None}
    monkeypatch.setattr(serve, "load_heads", lambda: {
        "B": {"entity": ["obj", "day"], "feature_set": "object",
              "budget_per_day": 2, "direction": "x", "title": "t"}})
    monkeypatch.setattr(serve.store, "latest_snapshot", lambda name: feats)
    monkeypatch.setattr(serve, "model_path", lambda h: _FakeArtifactPath(art))
    got = serve.score("B")
    assert got["above_thr"].sum() == 0
    assert got["alert"].sum() == 0


def test_armed_only_head_fails_loudly_without_the_column(monkeypatch):
    import datetime as dt

    import polars as pl

    feats = pl.DataFrame({"obj": ["1"], "day": [dt.date(2026, 1, 1)], "x": [0.1]})
    art = {"model": _ConstantModel(), "iso": None, "features": ["x"],
           "threshold": None, "feature_signature": None}
    monkeypatch.setattr(serve, "load_heads", lambda: {
        "C": {"entity": ["obj", "day"], "feature_set": "object",
              "label": "label_intrusion", "horizon_days": 1, "embargo_days": 31,
              "budget_per_day": 4, "armed_only": True, "direction": "x",
              "title": "t"}})
    monkeypatch.setattr(serve.store, "latest_snapshot", lambda name: feats)
    monkeypatch.setattr(serve, "model_path", lambda h: _FakeArtifactPath(art))
    with pytest.raises(ValueError, match="obj_armed"):
        serve.score("C")


def test_per_object_budget_never_exceeds_the_declared_budget():
    """Прежняя формула max(1, бюджет // объектов) при бюджете меньше числа
    объектов давала по одному каждому и выдавала БОЛЬШЕ бюджета: десять
    объектов при бюджете пять давали десять алертов. Обещание «не больше N
    выездов в сутки» нарушалось вдвое.
    """
    df = pl.DataFrame({"obj": [str(i // 3) for i in range(30)],
                       "ch": list(range(30)),
                       "risk": [0.9 - i * 0.01 for i in range(30)]})
    for budget in (1, 5, 7, 12, 29):
        out = serve._apply_budget(df, budget=budget, per_object=True)
        assert out["alert"].sum() == budget, f"бюджет {budget}"


def test_per_object_budget_still_spreads_across_objects():
    """Смысл побъектной отсечки — не дать всем алертам осесть на худших
    объектах. Точное соблюдение бюджета не должно это ломать."""
    df = pl.DataFrame({
        "obj": ["A"] * 5 + ["B"] * 5 + ["C"] * 5,
        "ch": list(range(15)),
        "risk": [0.99, 0.98, 0.97, 0.96, 0.95] + [0.5] * 5 + [0.1] * 5,
    })
    out = serve._apply_budget(df, budget=3, per_object=True)
    assert out.filter(pl.col("alert"))["obj"].n_unique() == 3
