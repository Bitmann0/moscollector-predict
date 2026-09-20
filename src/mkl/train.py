import os

import lightgbm as lgb
import numpy as np
import polars as pl

from . import metrics
from .config import EXCLUDED_PERIODS
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
    # Без этой пары одни и те же данные в двух процессах дают разный результат:
    # на восьми потоках суммы гистограмм складываются в разном порядке, и на
    # слабых головах расхождение доходило до 4.6% относительных. Заказчику
    # нужен воспроизводимый список алертов, а мне — чтобы дельта рычага не
    # тонула в шуме запуска.
    "deterministic": True,
    "force_row_wise": True,
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


# Бэкенд по умолчанию задаётся переменной окружения MKL_BACKEND: так его можно
# сменить для всего пайплайна одним местом, не трогая каждый вызов. Причина
# существования этой ручки — LightGBM из pip-колеса собран без поддержки GPU
# ("GPU Tree Learner was not enabled in this build"), поэтому перевод расчёта
# на CUDA означает смену семейства модели, а не флага, и такое решение должно
# приниматься замером и фиксироваться явно.
def default_backend() -> str:
    """По умолчанию XGBoost на CUDA.

    Замер при РАВНОЙ ёмкости: на объектных головах (около 115 тыс. строк) CUDA
    вдвое быстрее при том же качестве — 17 с против 32 с, ROC 0.9001 против
    0.8986. На канальных (3.95 млн строк) выигрыша нет вовсе: 479 с против 485,
    и это тоже результат, потому что ожидание было обратным. Качество всех трёх
    семейств совпадает в пределах пункта, так что переход ничего не стоит и
    кое-где экономит вдвое.

    LightGBM остаётся доступен: pip-колесо собрано без поддержки GPU
    ("GPU Tree Learner was not enabled in this build"), и на больших головах
    он не медленнее. MKL_BACKEND=lgbm возвращает его.
    """
    return os.environ.get("MKL_BACKEND", "xgb")


def params_for(cfg: dict, backend: str) -> dict | None:
    """Параметры головы под конкретный бэкенд.

    В heads.yaml `params` записаны в терминах LightGBM (num_leaves,
    min_child_samples), и XGBoost с CatBoost их не примут. Пер-бэкендные ключи
    `params_xgb` и `params_cat` задают эквивалент по ёмкости; если их нет,
    берутся умолчания семейства.

    Смешивать нельзя: сравнение «lgbm с 800 деревьями против xgb с 400»
    показывало разрыв в 4.4 пункта точности, который целиком объяснялся
    разной ёмкостью, а не бэкендом.
    """
    if backend == "lgbm":
        return cfg.get("params")
    return cfg.get(f"params_{backend}")


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
METRIC_KEYS = ("pr_auc", "pr_auc_norm", "roc_auc", "precision_at_k", "recall_at_k",
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
        budget_per_day: int = 20, backend: str | None = None,
        horizon_days: int = 1, half_life_days: float | None = None) -> dict:
    """Обучение головы по walk-forward схеме.

    Дисбаланс лечится только scale_pos_weight — никакого oversampling:
    он портит калибровку и не двигает PR-AUC.

    Матрица признаков строится один раз на весь датасет, а фолды режутся
    булевыми масками: пересборка на каждом фолде через polars-фильтр съедала
    больше времени, чем само обучение.
    """
    backend = backend or default_backend()
    join_keys = [k for k in KEYS if k in features.columns and k in labels.columns]
    data = features.join(labels, on=join_keys, how="inner").sort(join_keys)
    # Сортировка здесь не косметика. Метки приходят из DuckDB, который при
    # параллельном сканировании порядок строк не обещает, и polars-join его
    # тоже не сохраняет — матрица собиралась каждый раз по-своему. От порядка
    # зависят и подвыборка, и разрыв ничьих при отсечке бюджета, так что один
    # и тот же вход в двух процессах давал разные числа: на голове B норм.
    # PR-AUC гуляла между 0.0316 и 0.0362, то есть 14% относительных.
    # Периоды, исключённые заказчиком, выбрасываются здесь — в единственной
    # точке, через которую проходит и обучение, и оценка.
    for a, b in EXCLUDED_PERIODS:
        data = data.filter((pl.col("day") < a) | (pl.col("day") > b))
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
        if half_life_days:
            # Вес по свежести: за 7,5 лет состав парка и конфигурация СМВУ
            # менялись, старые строки полезны, но их вклад должен убывать.
            from .stacking import recency_weights
            model.fit(X[tr_m], ytr,
                      sample_weight=recency_weights(days[tr_m], half_life_days))
        else:
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
                horizon_days=horizon_days, budget=budget_per_day * n_days))
        res["test_start"], res["test_end"] = str(s.test_start), str(s.test_end)
        # Состав обучающих суток отдаётся наружу, чтобы исключение периодов
        # можно было проверить по существу, а не по размеру выборки: прежний
        # тест сравнивал сумму тестовых фолдов с числом всех строк и потому
        # проходил даже при полностью удалённом фильтре.
        tr_days = np.unique(days[tr_m])
        res["train_days"] = [str(np.datetime_as_string(d, unit="D")) for d in tr_days]
        res["n_train_days"] = int(len(tr_days))
        res["n_train"] = int(tr_m.sum())
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
