import lightgbm as lgb
import numpy as np
import polars as pl

from . import metrics
from .cv import Split

DEFAULT_PARAMS = {
    "objective": "binary",
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_child_samples": 100,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "max_bin": 63,          # 255 бинов на 4 млн строк не окупаются точностью
    "force_col_wise": True,  # снимает переоценку гистограмм на каждом фолде
    "verbose": -1,
    "n_jobs": 8,
    "random_state": 42,
}

KEYS = ("ch", "obj", "seg", "day")
METRIC_KEYS = ("pr_auc", "precision_at_k", "recall_at_k", "lift_at_k",
               "precision", "recall", "brier", "base_rate",
               "op_precision", "op_recall", "op_k")


def feature_columns(df: pl.DataFrame) -> list[str]:
    return [c for c in df.columns
            if c not in KEYS and c != "y" and df.schema[c].is_numeric()]


def _matrix(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    """float32 вместо float64: на 15 млн строк и 60 признаках это 3,6 ГБ вместо 7,2."""
    return df.select([pl.col(c).cast(pl.Float32) for c in cols]).to_numpy()


def run(head: str, features: pl.DataFrame, labels: pl.DataFrame,
        splits: list[Split], params: dict | None = None,
        budget_per_day: int = 20) -> dict:
    """Обучение головы по walk-forward схеме.

    Дисбаланс лечится только scale_pos_weight — никакого oversampling:
    он портит калибровку и не двигает PR-AUC.

    Матрица признаков строится один раз на весь датасет, а фолды режутся
    булевыми масками: пересборка на каждом фолде через polars-фильтр съедала
    больше времени, чем само обучение.
    """
    join_keys = [k for k in KEYS if k in features.columns and k in labels.columns]
    data = features.join(labels, on=join_keys, how="inner")
    p = {**DEFAULT_PARAMS, **(params or {})}
    cols = feature_columns(data)

    X = _matrix(data, cols)
    y_all = data["y"].to_numpy()
    days = data["day"].to_numpy()

    folds: list[dict] = []
    model = None
    for s in splits:
        tr_m = (days >= np.datetime64(s.train_start)) & (days <= np.datetime64(s.train_end))
        te_m = (days >= np.datetime64(s.test_start)) & (days <= np.datetime64(s.test_end))
        ytr, yte = y_all[tr_m], y_all[te_m]
        if len(ytr) == 0 or len(yte) == 0 or ytr.sum() == 0 or yte.sum() == 0:
            continue
        pos = int(ytr.sum())
        model = lgb.LGBMClassifier(**p, scale_pos_weight=(len(ytr) - pos) / pos)
        model.fit(X[tr_m], ytr)
        proba = model.predict_proba(X[te_m])[:, 1]
        n_days = (s.test_end - s.test_start).days + 1
        res = metrics.summary(yte, proba, budget=budget_per_day * n_days)
        res["test_start"], res["test_end"] = str(s.test_start), str(s.test_end)
        folds.append(res)

    mean = ({k: float(np.nanmean([f[k] for f in folds])) for k in METRIC_KEYS}
            if folds else {})
    if folds:
        mean["n_pos"] = int(np.sum([f["n_pos"] for f in folds]))
        mean["n"] = int(np.sum([f["n"] for f in folds]))
        mean["op_feasible_folds"] = int(sum(f["op_feasible"] for f in folds))
    return {"head": head, "folds": folds, "mean": mean,
            "model": model, "feature_names": cols}


def importance(model, feature_names: list[str], top: int = 25) -> pl.DataFrame:
    return (
        pl.DataFrame({"feature": feature_names,
                      "gain": model.booster_.feature_importance("gain")})
        .sort("gain", descending=True)
        .head(top)
    )
