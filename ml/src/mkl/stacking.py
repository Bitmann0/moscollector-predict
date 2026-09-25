"""Стекинг: предсказание плотной головы как признак для редкой.

У редких голов база около 1% и им не хватает объёма для калибровки, тогда как
голова A обучается на 26% позитивов и даёт плотный, хорошо оценённый приор.
Вероятности считаются строго out-of-fold по той же walk-forward схеме и с тем
же embargo: иначе признак содержит ответ и результат ничего не значит.
"""
import numpy as np
import polars as pl

from . import train
from .cv import Split, walk_forward


def oof_predictions(head: str, features: pl.DataFrame, labels: pl.DataFrame,
                    splits: list[Split], params: dict | None = None,
                    entity: str = "ch") -> pl.DataFrame:
    """Out-of-fold вероятности головы на тестовых окнах её собственных фолдов.

    Возвращает таблицу (сущность, день, вероятность). Строки вне тестовых окон
    не возвращаются вовсе: подставлять туда предсказание модели, видевшей эти
    сутки при обучении, значит протащить ответ в признак.
    """
    join_keys = [k for k in train.KEYS
                 if k in features.columns and k in labels.columns]
    data = features.join(labels, on=join_keys, how="inner")
    cols = train.feature_columns(data)
    X = train._matrix(data, cols)
    y = data["y"].to_numpy()
    days = data["day"].to_numpy()

    out = []
    for s in splits:
        tr = (days >= np.datetime64(s.train_start)) & (days <= np.datetime64(s.train_end))
        te = (days >= np.datetime64(s.test_start)) & (days <= np.datetime64(s.test_end))
        if tr.sum() == 0 or te.sum() == 0 or y[tr].sum() == 0:
            continue
        pos = int(y[tr].sum())
        model = train._build_model("lgbm", params, (tr.sum() - pos) / pos)
        model.fit(X[tr], y[tr])
        block = data.filter(pl.Series(te)).select([entity, "day"])
        out.append(block.with_columns(
            pl.Series(f"prior_{head}", model.predict_proba(X[te])[:, 1])))
    if not out:
        return pl.DataFrame()
    return pl.concat(out)


def attach_prior(features: pl.DataFrame, prior: pl.DataFrame,
                 on: list[str]) -> pl.DataFrame:
    """Приклеить вероятность-приор к признакам.

    Строки без приора остаются с пустым значением, а не с нулём: ноль модель
    прочитает как «риск отсутствует», тогда как на самом деле приор просто
    не посчитан для этих суток.
    """
    if prior.is_empty():
        return features
    return features.join(prior, on=on, how="left")


def recency_weights(days: np.ndarray, half_life_days: float = 180.0) -> np.ndarray:
    """Экспоненциальный вес наблюдений по свежести.

    За 7,5 лет состав парка и конфигурация СМВУ менялись: пожарных каналов
    стало 1189 -> 5701, тревоги газовой подсистемы выросли с 845 до 35 864 за
    год. Старые строки полезны, но их вклад должен убывать.
    """
    d = np.asarray(days, dtype="datetime64[D]")
    age = (d.max() - d).astype("timedelta64[D]").astype(float)
    return np.power(0.5, age / half_life_days)
