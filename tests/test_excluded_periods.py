"""Исключение периода миграции СМВУ весной 2021.

Заказчик указал: во время перехода на новую версию системы сотрудники не могли
работать в двух системах сразу и не снимали объекты с охраны при профилактике,
из-за чего пожарных тревог за апрель-июнь 2021 набралось 1 005 231 против
единиц тысяч в соседние месяцы. Период рекомендован к исключению.
"""
import datetime as dt

import numpy as np
import polars as pl

from mkl import config, cv, train


def test_keep_day_rejects_migration_window():
    assert not config.keep_day(dt.date(2021, 5, 15))
    assert not config.keep_day(dt.date(2021, 4, 1))
    assert not config.keep_day(dt.date(2021, 6, 30))


def test_keep_day_accepts_neighbouring_months():
    assert config.keep_day(dt.date(2021, 3, 31))
    assert config.keep_day(dt.date(2021, 7, 1))
    assert config.keep_day(dt.date(2025, 5, 15))


def test_training_drops_excluded_rows():
    rng = np.random.default_rng(0)
    days = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(500)]
    rows = []
    for d in days:
        for ch in range(10):
            y = int(rng.random() < 0.2)
            rows.append({"ch": ch, "day": d, "x": y * 3.0 + rng.normal(), "y": y})
    df = pl.DataFrame(rows)
    feats, labs = df.select(["ch", "day", "x"]), df.select(["ch", "day", "y"])
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    out = train.run("t", feats, labs, splits, params={"n_estimators": 30},
                    budget_per_day=5)
    # строк в обучении должно стать меньше на объём исключённого окна
    assert out["folds"], "фолд должен построиться"
    assert out["mean"]["n"] < len(rows)
