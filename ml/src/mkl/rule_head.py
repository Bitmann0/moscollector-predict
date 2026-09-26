"""Голова-правило: риск — готовый признак, порог выбирается перед каждым днём расчёта.

Модель D заменена правилом `n_bad_w7` по заранее записанному протоколу:
reports/RULE_VS_MODEL_PROTOCOL.md, результат — reports/RULE_VS_MODEL_RESULT.md.
На валидации 11.09–31.12.2025 модель D при рабочем пороге 70 % молчала во всех
окнах, правило поймало 22 новых эпизода при нижней границе precision 63,8 %.

Порог выбирается тем же способом, что в стенде, где правило проверено
(second_ml_audit.select_threshold), и так же раз в неделю: в понедельник
недели расчёта. Окно порога — 30 суток, кончающихся за horizon + 1 суток до
понедельника; исходы — только известные к воскресенью; дневной лимит,
распределение по объектам и пауза — из heads.yaml. Ежедневный пересчёт не
проверялся: при минимуме в 30 рекомендаций порог на соседних днях то находится,
то нет, и голова «мигала» бы.

Обученной модели и файла у такой головы нет: артефакт собирается при расчёте.
Задержка порога — от 8 суток в понедельник до 14 в воскресенье, в пределах
max_model_lag_days головы D.
"""
from __future__ import annotations

import datetime as dt
import math
from functools import lru_cache

import polars as pl

from . import cv, db, store
from . import second_ml_audit as audit
from .config import PATHS


def is_rule(cfg: dict) -> bool:
    return bool(cfg.get("serving_rule"))


def inputs_ready() -> bool:
    """Лёгкая проверка для /ready: исходы для выбора порога есть на диске."""
    return all((PATHS.interim / f"{name}.parquet").exists()
               for name in ("daily_channel", "episodes"))


def _source_key() -> tuple[int, int]:
    return tuple((PATHS.interim / f"{name}.parquet").stat().st_mtime_ns
                 for name in ("daily_channel", "episodes"))


@lru_cache(maxsize=2)
def _outcomes(head: str, source_key: tuple[int, int]) -> pl.DataFrame:
    """Исходы с датой появления; кэш сбрасывается, когда панель пересобрана."""
    con = db.connect()
    try:
        db.attach_parquet(con, "daily_channel", "episodes")
        outcomes, _ = audit.build_outcomes(con, head)
    finally:
        con.close()
    return outcomes


def select(head: str, cfg: dict, day: dt.date,
           features: pl.DataFrame, outcomes: pl.DataFrame) -> dict:
    """Порог правила по окну перед днём `day`. Чистая функция для тестов."""
    rule = cfg["serving_rule"]
    frame = audit.candidates(features, outcomes, head, day - dt.timedelta(days=1))
    values = (frame[rule].cast(pl.Float64).fill_nan(0.0).fill_null(0.0)
              if frame.height else pl.Series(rule, [], dtype=pl.Float64))
    scored = frame.select("ch", "obj", "day", "y").with_columns(values.alias("risk"))
    top = audit.daily_top(scored, int(cfg["budget_per_day"]),
                          bool(cfg.get("budget_per_object")))
    return audit.select_threshold(
        top, float(cfg.get("operating_min_precision", 0.7)),
        int(cfg.get("operating_min_alerts", 30)),
        cooldown=int(cfg.get("cooldown_days", 7)))


_ARTIFACTS: dict[tuple, dict] = {}


def artifact(head: str, cfg: dict, day: dt.date) -> dict:
    """Артефакт в той же форме, что у модели, чтобы serve и C1 не ветвились дальше.

    Неосуществимый порог — бесконечность: голова молчит, а не выдаёт top-k.
    Один /score спрашивает артефакт дважды (C1 и serve), поэтому он кэшируется
    по дню и отпечатку панели.
    """
    refresh = refresh_day(day)
    key = (head, cfg["serving_rule"], refresh, _source_key())
    if key not in _ARTIFACTS:
        if len(_ARTIFACTS) > 64:
            _ARTIFACTS.clear()
        _ARTIFACTS[key] = _build(head, cfg, refresh)
    return _ARTIFACTS[key]


def refresh_day(day: dt.date) -> dt.date:
    """Понедельник недели расчёта: порог правила пересчитывается раз в неделю."""
    return day - dt.timedelta(days=day.weekday())


def _build(head: str, cfg: dict, day: dt.date) -> dict:
    rule = cfg["serving_rule"]
    windows = cv.live_windows(day - dt.timedelta(days=int(cfg["horizon_days"]) + 1),
                              int(cfg["embargo_days"]))
    start, end = windows["threshold_start"], windows["threshold_end"]
    features = store.read_slice(cfg["feature_set"], start, end,
                                columns=["ch", "obj", "day", "stype", rule])
    pick = select(head, cfg, day, features, _outcomes(head, _source_key()))
    return {
        "model": None, "iso": None, "rule": rule, "features": [rule],
        "threshold": pick["threshold"] if pick["feasible"] else math.inf,
        "selection": pick,
        "saved_at": f"rule:{rule}@{end.isoformat()}",
        "metadata": {
            "head": head, "rule": rule,
            "label": cfg["label"], "horizon_days": cfg["horizon_days"],
            "variant": cfg.get("variant"),
            "unknown_in_budget": bool(cfg.get("unknown_in_budget")),
            "operating_min_precision": float(cfg.get("operating_min_precision", 0.7)),
            "threshold_start": start.isoformat(), "threshold_end": end.isoformat(),
            "refreshed_on": day.isoformat(),
            "threshold_outcomes": "D_observed_v1" if head == "D" else "L9c_available_at_return",
        },
    }


def feasible(art: dict) -> bool:
    """Порог осуществим: у правила — конечен, у модели — не выше 1 (train_latest.py)."""
    threshold = art.get("threshold")
    if threshold is None or not math.isfinite(threshold):
        return False
    return bool(art.get("rule")) or threshold <= 1.0
