import datetime as dt

import numpy as np
import polars as pl

from mkl import cv, train


def _dataset(seed=0, signal=3.0):
    rng = np.random.default_rng(seed)
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(400)]
    rows = []
    for d in days:
        for ch in range(20):
            y = int(rng.random() < 0.2)
            rows.append({"ch": ch, "day": d,
                         "x": y * signal + rng.normal(),
                         "noise": rng.normal(),
                         "y": y})
    df = pl.DataFrame(rows)
    return days, df.select(["ch", "day", "x", "noise"]), df.select(["ch", "day", "y"])


def test_run_learns_a_separable_signal():
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 60}, budget_per_day=5)
    assert out["mean"]["pr_auc"] > 0.5
    assert len(out["folds"]) == 2


def test_run_finds_no_signal_in_pure_noise():
    days, feats, labels = _dataset(signal=0.0)
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 60}, budget_per_day=5)
    assert out["mean"]["pr_auc"] < 0.35, "на чистом шуме PR-AUC не должен уходить далеко от базовой ставки 0.2"


def test_feature_columns_excludes_keys_and_target():
    _, feats, labels = _dataset()
    data = feats.join(labels, on=["ch", "day"], how="inner")
    cols = train.feature_columns(data)
    assert set(cols) == {"x", "noise"}


def test_segment_episode_metric_uses_object_and_segment():
    """Соседние участки одного объекта — независимые пожарные эпизоды."""
    frame = pl.DataFrame({"obj": ["A", "A", "B"],
                          "seg": [0, 1, 0],
                          "day": [dt.date(2025, 1, 1)] * 3})
    entities = train._episode_entities(frame)
    assert len(set(entities)) == 3
    assert entities[0] != entities[1]


def test_all_backends_learn_the_same_signal():
    """Смена семейства бустинга не должна ломать интерфейс обучения."""
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    # Проверка API модели не должна зависеть от версии CUDA-драйвера машины.
    for backend, params in [("lgbm", {"n_estimators": 40}),
                            ("xgb", {"n_estimators": 40, "device": "cpu"}),
                            ("cat", {"iterations": 40, "task_type": "CPU"})]:
        out = train.run("test", feats, labels, splits, params=params,
                        budget_per_day=5, backend=backend)
        assert out["mean"]["pr_auc"] > 0.5, backend
        assert out["backend"] == backend
        top = train.importance(out["model"], out["feature_names"], top=1)
        assert top["feature"][0] == "x", backend


def test_importance_ranks_the_informative_feature_first():
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 60}, budget_per_day=5)
    top = train.importance(out["model"], out["feature_names"], top=1)
    assert top["feature"][0] == "x"


def test_result_does_not_depend_on_input_row_order():
    """Метки приходят из DuckDB, который при параллельном сканировании порядок
    строк не обещает, и polars-join его не сохраняет. Пока матрица собиралась
    в произвольном порядке, один и тот же вход в двух процессах давал разные
    числа — на голове B нормированная PR-AUC гуляла между 0.0316 и 0.0362.
    Это шире шума, который я готов был считать несущественным, и часть решений
    о признаках принималась на таких замерах.
    """
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    kw = {"params": {"n_estimators": 60}, "budget_per_day": 5}
    straight = train.run("test", feats, labels, splits, **kw)
    shuffled = train.run("test", feats.sample(fraction=1.0, shuffle=True, seed=7),
                         labels.sample(fraction=1.0, shuffle=True, seed=13), splits, **kw)
    for k in ("pr_auc", "pr_auc_norm", "precision_at_k", "op_precision"):
        assert straight["mean"][k] == shuffled["mean"][k], (
            f"{k} зависит от порядка строк на входе")


def test_backend_comes_from_environment_when_not_given(monkeypatch):
    """Ручка нужна, потому что перевод расчёта на CUDA — это смена семейства
    модели, а не флаг: LightGBM из pip-колеса собран без поддержки GPU.
    Такое решение должно задаваться одним местом и фиксироваться явно.
    """
    monkeypatch.delenv("MKL_BACKEND", raising=False)
    assert train.default_backend() == "xgb", "по умолчанию CUDA"
    monkeypatch.setenv("MKL_BACKEND", "lgbm")
    assert train.default_backend() == "lgbm"


def test_explicit_backend_wins_over_environment(monkeypatch):
    monkeypatch.setenv("MKL_BACKEND", "cat")
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    out = train.run("t", feats, labels, splits, backend="lgbm",
                    params={"n_estimators": 20}, budget_per_day=5)
    assert out["backend"] == "lgbm"


def test_fold_slices_select_exactly_the_days_of_the_split():
    """Фолды режутся непрерывным срезом, а не булевой маской: при сортировке по
    суткам это представление, а не копия, и на большой голове экономит 2 ГБ.
    Замена молчаливо сменила бы состав фолдов, если бы границы считались неверно.
    """
    days, feats, labels = _dataset()
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    out = train.run("t", feats, labels, splits, params={"n_estimators": 20},
                    budget_per_day=5)
    for fold, s in zip(out["folds"], splits):
        got = {dt.date.fromisoformat(d) for d in fold["train_days"]}
        assert min(got) >= s.train_start and max(got) <= s.train_end
        assert fold["test_start"] == str(s.test_start)
        expected = sum(1 for d in days if s.train_start <= d <= s.train_end)
        assert fold["n_train_days"] == expected


def test_excluded_period_inside_the_training_span_does_not_break_slicing():
    """Исключённые сутки просто отсутствуют в отсортированном массиве, и
    границы среза обязаны это учитывать."""
    import polars as pl

    rng = np.random.default_rng(1)
    days = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(400)]
    rows = [{"ch": ch, "day": d, "x": rng.normal(), "y": int(rng.random() < 0.2)}
            for d in days for ch in range(5)]
    df = pl.DataFrame(rows)
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    out = train.run("t", df.select(["ch", "day", "x"]), df.select(["ch", "day", "y"]),
                    splits, params={"n_estimators": 20}, budget_per_day=5)
    got = {dt.date.fromisoformat(d) for d in out["folds"][0]["train_days"]}
    assert not any(dt.date(2021, 4, 1) <= d <= dt.date(2021, 6, 30) for d in got)
