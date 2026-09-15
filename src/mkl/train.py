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
    "max_bin": 63,  # 255 бинов на 4 млн строк не окупаются точностью
    "verbose": -1,
    "n_jobs": 8,
    "random_state": 42,
}

XGB_PARAMS = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "max_depth": 8,
    "min_child_weight": 20,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "max_bin": 64,
    "tree_method": "hist",
    "device": "cuda",
    "verbosity": 0,
    "random_state": 42,
}

CAT_PARAMS = {
    "iterations": 400,
    "learning_rate": 0.05,
    "depth": 8,
    "l2_leaf_reg": 3.0,
    "border_count": 64,
    "task_type": "GPU",
    "devices": "0",
    "verbose": 0,
    "random_seed": 42,
}


def _build_model(backend: str, params: dict | None, spw: float):
    """Модель нужного семейства. XGBoost и CatBoost считаются на GPU."""
    if backend == "lgbm":
        return lgb.LGBMClassifier(**{**DEFAULT_PARAMS, **(params or {})},
                                  scale_pos_weight=spw)
    if backend == "xgb":
        import xgboost as xgb
        return xgb.XGBClassifier(**{**XGB_PARAMS, **(params or {})},
                                 scale_pos_weight=spw)
    if backend == "cat":
        import catboost as cb
        return cb.CatBoostClassifier(**{**CAT_PARAMS, **(params or {})},
                                     scale_pos_weight=spw)
    raise ValueError(f"неизвестный бэкенд: {backend}")

KEYS = ("ch", "obj", "seg", "day")
METRIC_KEYS = ("pr_auc", "pr_auc_norm", "precision_at_k", "recall_at_k",
               "lift_at_k", "precision", "recall", "brier", "base_rate",
               "op_precision", "op_recall", "op_k", "p_at_r50",
               "episode_recall", "episode_precision", "days_per_episode")


def feature_columns(df: pl.DataFrame) -> list[str]:
    return [c for c in df.columns
            if c not in KEYS and c != "y" and df.schema[c].is_numeric()]


def _matrix(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    """float32 вместо float64: на 15 млн строк и 60 признаках это 3,6 ГБ вместо 7,2."""
    return df.select([pl.col(c).cast(pl.Float32) for c in cols]).to_numpy()


def run(head: str, features: pl.DataFrame, labels: pl.DataFrame,
        splits: list[Split], params: dict | None = None,
        budget_per_day: int = 20, backend: str = "lgbm",
        horizon_days: int = 1) -> dict:
    """Обучение головы по walk-forward схеме.

    Дисбаланс лечится только scale_pos_weight — никакого oversampling:
    он портит калибровку и не двигает PR-AUC.

    Матрица признаков строится один раз на весь датасет, а фолды режутся
    булевыми масками: пересборка на каждом фолде через polars-фильтр съедала
    больше времени, чем само обучение.
    """
    join_keys = [k for k in KEYS if k in features.columns and k in labels.columns]
    data = features.join(labels, on=join_keys, how="inner")
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
        model = _build_model(backend, params, (len(ytr) - pos) / pos)
        model.fit(X[tr_m], ytr)
        proba = model.predict_proba(X[te_m])[:, 1]
        n_days = (s.test_end - s.test_start).days + 1
        res = metrics.summary(yte, proba, budget=budget_per_day * n_days)
        # Эпизодный замер рядом с посуточным: длинный отказ должен считаться
        # одним событием, а не серией независимых попаданий.
        ent_col = next((k for k in KEYS if k != "day" and k in data.columns), None)
        if ent_col is not None:
            te = data.filter(pl.Series(te_m))
            res.update(metrics.episode_summary(
                te[ent_col].to_numpy(), te["day"].to_numpy(), yte, proba,
                horizon_days=horizon_days))
        res["test_start"], res["test_end"] = str(s.test_start), str(s.test_end)
        folds.append(res)

    mean = ({k: float(np.nanmean([f[k] for f in folds])) for k in METRIC_KEYS}
            if folds else {})
    if folds:
        mean["n_pos"] = int(np.sum([f["n_pos"] for f in folds]))
        mean["episodes"] = int(np.sum([f.get("episodes", 0) for f in folds]))
        mean["n"] = int(np.sum([f["n"] for f in folds]))
        mean["op_feasible_folds"] = int(sum(f["op_feasible"] for f in folds))
    return {"head": head, "folds": folds, "mean": mean, "backend": backend,
            "model": model, "feature_names": cols}


def importance(model, feature_names: list[str], top: int = 25) -> pl.DataFrame:
    """Важность признаков независимо от семейства модели."""
    if hasattr(model, "booster_"):
        gain = model.booster_.feature_importance("gain")
    elif hasattr(model, "get_feature_importance"):
        gain = model.get_feature_importance()
    else:
        gain = model.feature_importances_
    return (
        pl.DataFrame({"feature": feature_names, "gain": np.asarray(gain, dtype=float)})
        .sort("gain", descending=True)
        .head(top)
    )
