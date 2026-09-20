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


def _year_2021(signal_in_excluded=0.0):
    """Год вокруг периода миграции. Тестовое окно ложится ПОСЛЕ исключённого
    периода, обучающее его накрывает — иначе фильтр проверять нечем."""
    rng = np.random.default_rng(0)
    days = [dt.date(2021, 1, 1) + dt.timedelta(days=i) for i in range(365)]
    rows = []
    for d in days:
        for ch in range(10):
            y = int(rng.random() < 0.2)
            x = y * 3.0 + rng.normal()
            if signal_in_excluded and not config.keep_day(d):
                # Ложный сигнал внутри миграции: если период действительно
                # исключается, на метриках он не отразится никак.
                x = signal_in_excluded * (1 - y)
            rows.append({"ch": ch, "day": d, "x": x, "y": y})
    df = pl.DataFrame(rows)
    return days, df.select(["ch", "day", "x"]), df.select(["ch", "day", "y"])


def _run(days, feats, labs):
    splits = cv.walk_forward(days, n_splits=1, test_days=30, embargo_days=31)
    return train.run("t", feats, labs, splits, params={"n_estimators": 30},
                     budget_per_day=5)


def test_no_excluded_day_reaches_the_training_set():
    days, feats, labs = _year_2021()
    out = _run(days, feats, labs)
    assert out["folds"], "фолд должен построиться"
    got = {dt.date.fromisoformat(d) for d in out["folds"][0]["train_days"]}
    leaked = sorted(d for d in got if not config.keep_day(d))
    assert not leaked, f"в обучение попали сутки миграции: {leaked[:5]}"


def test_training_loses_exactly_the_excluded_days():
    days, feats, labs = _year_2021()
    out = _run(days, feats, labs)
    fold = out["folds"][0]
    span = (dt.date.fromisoformat(fold["train_days"][-1])
            - dt.date.fromisoformat(fold["train_days"][0])).days + 1
    excluded = sum(1 for i in range(span)
                   if not config.keep_day(dt.date.fromisoformat(fold["train_days"][0])
                                          + dt.timedelta(days=i)))
    assert excluded == 91, "апрель-июнь 2021 внутри обучающего окна"
    assert fold["n_train_days"] == span - excluded


def test_false_signal_inside_the_excluded_window_changes_nothing():
    """Самая сильная проверка: внутрь миграции кладётся признак, идеально
    предсказывающий ОТСУТСТВИЕ события. Если период исключается, метрики
    не сдвинутся; если нет — модель этот признак выучит.
    """
    clean = _run(*_year_2021())
    poisoned = _run(*_year_2021(signal_in_excluded=10.0))
    assert clean["mean"]["pr_auc"] == poisoned["mean"]["pr_auc"]
