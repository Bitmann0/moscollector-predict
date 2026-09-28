"""Протокол температурного бэктеста: порог, срезы фолдов, дневной лимит, смоук."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from mkl.temperature_episode import STYPE, build_features, feature_columns

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import exp_temperature_episode as exp  # noqa: E402


def test_ranking_metrics_apply_daily_budget():
    frame = pd.DataFrame({
        "as_of": pd.to_datetime(["2024-01-01"] * 2 + ["2024-01-02"] * 2),
        "target": [1, 0, 0, 1],
    })
    result = exp.ranking_metrics(frame, [0.9, 0.1, 0.8, 0.7])
    assert result["daily_budget_1"]["alerts"] == 2
    assert result["daily_budget_1"]["precision"] == 0.5
    assert result["precision_at_50pct_recall"] == 1.0


def test_select_threshold_falls_back_when_precision_is_unreachable():
    # Позитивы и негативы вперемешку по баллу: точность 0,7 недостижима нигде.
    target = np.array([1, 0] * 50)
    scores = np.linspace(0, 1, 100)
    _, rule = exp.select_threshold(target, scores)
    assert rule == "fallback_f0.5_precision_constraint_unmet"


def test_select_threshold_needs_ten_alerts_above_precision():
    # Верхние 20 баллов — позитивы: порог с точностью 1,0 и 20 алертами есть.
    target = np.r_[np.zeros(80, dtype=int), np.ones(20, dtype=int)]
    scores = np.linspace(0, 1, 100)
    threshold, rule = exp.select_threshold(target, scores)
    assert rule == "precision_constraint"
    assert exp.binary_metrics(target, scores, threshold)["alerts"] >= 10
    # Пять позитивов наверху: точность 1,0 бывает только при пяти алертах и
    # меньше, а с десятью алертами она не выше 0,5 — ограничение не выполнено.
    target = np.r_[np.zeros(95, dtype=int), np.ones(5, dtype=int)]
    _, rule = exp.select_threshold(target, scores)
    assert rule == "fallback_f0.5_precision_constraint_unmet"


def test_slice_drops_row_whose_label_leaves_the_period():
    days = pd.to_datetime(["2024-06-29", "2024-06-30", "2024-07-01"])
    data = pd.DataFrame({"as_of": days, "label_end": days + pd.Timedelta(days=1)})
    got = exp._slice(data, "2024-01-01", "2024-07-01")
    assert [str(d)[:10] for d in got.as_of] == ["2024-06-29", "2024-06-30"]


def test_embargo_moves_only_period_ends():
    fold = exp.with_embargo(exp.FOLDS[0], 31)
    assert fold.train_end == "2022-12-01"
    assert fold.validation_start == exp.FOLDS[0].validation_start
    assert fold.test_start == exp.FOLDS[0].test_start
    assert fold.test_end == exp.FOLDS[0].test_end


def _toy_samples(test_positives: bool) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    days = pd.date_range("2024-01-01", periods=40, freq="D")
    frame = pd.DataFrame({"as_of": np.repeat(days, 5)})
    frame["label_end"] = frame.as_of + pd.Timedelta(days=1)
    frame["channel"] = np.tile(np.arange(5), len(days))
    frame["channel_bucket"] = 1
    frame["target"] = (np.arange(len(frame)) % 7 == 0).astype(int)
    if not test_positives:
        frame.loc[frame.as_of >= "2024-01-31", "target"] = 0
    for column in feature_columns():
        frame[column] = rng.normal(size=len(frame))
    return frame


def _toy_fold() -> "exp.Fold":
    return exp.Fold("toy", "2024-01-01", "2024-01-21", "2024-01-21", "2024-01-26",
                    "2024-01-26", "2024-01-31", "2024-01-31", "2024-02-10")


def test_evaluate_fold_reports_split_without_positives():
    result, artifact = exp.evaluate_fold(_toy_samples(False), _toy_fold(),
                                         feature_columns(), unseen_channels=False)
    assert result == {"fold": "toy", "status": "insufficient_both_classes"}
    assert artifact is None


def _synthetic_events(con, channels=20, days=120, seed=1):
    """Показания раз в два часа у 20 каналов; изредка выход за диапазон в полдень.

    Вставка одним запросом: 28 800 построчных INSERT идут десятки секунд.
    """
    rng = np.random.default_rng(seed)
    hours = pd.timedelta_range(0, periods=days * 12, freq="2h")
    ts = pd.Timestamp("2024-01-01") + hours
    parts = []
    for ch in range(1, channels + 1):
        value = rng.uniform(10, 25) + rng.normal(0, 0.5, size=len(ts))
        excursion = np.repeat(rng.random(days) < 0.06, 12) & (ts.hour == 12)
        value[excursion] = rng.choice([1.0, 45.0], size=int(excursion.sum()))
        parts.append(pd.DataFrame({"ch": ch, "ts": ts, "val_raw": [f"{v:.1f}" for v in value]}))
    events = pd.concat(parts, ignore_index=True)
    events["event_id"] = np.arange(len(events))
    con.register("synthetic", events)
    con.execute("""
        INSERT INTO ev (event_id, ch, ts, day, alarm, val_raw, stype)
        SELECT event_id, ch, ts, CAST(ts AS DATE), false, val_raw, ?
        FROM synthetic
    """, [STYPE])
    con.unregister("synthetic")


def test_run_smoke_on_synthetic_features(ev_con, tmp_path, monkeypatch):
    _synthetic_events(ev_con)
    features = tmp_path / "features.parquet"
    build_features(ev_con, features)
    monkeypatch.setattr(exp, "FOLDS", (
        exp.Fold("toy-1", "2024-01-02", "2024-02-20", "2024-02-20", "2024-03-10",
                 "2024-03-10", "2024-03-25", "2024-03-25", "2024-04-10"),
    ))
    # Отдельный журнал: настоящий experiments/log.jsonl лежит в git.
    logged = []
    monkeypatch.setattr(exp.experiments, "log", logged.append)
    # Смоук-фолд лежит в 2024-м; для проверки записи сдвигаем отложенный период на него.
    monkeypatch.setattr(exp, "HOLDOUT_START", pd.Timestamp("2024-03-25").date())
    monkeypatch.setattr(exp, "HOLDOUT_END", pd.Timestamp("2024-04-30").date())
    report_path = tmp_path / "report.json"
    report = exp.run(features, report_path, clean_hours=24)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["quality"] == report["quality"]
    assert set(report["protocols"]) == {
        "temporal_all_channels", "unseen_channel_20pct",
        f"temporal_all_channels_embargo{exp.EMBARGO_DAYS}"}
    fold = report["protocols"]["temporal_all_channels"]["folds"][0]
    assert fold["status"] == "ok"
    assert fold["threshold_rule"] in {"precision_constraint",
                                      "fallback_f0.5_precision_constraint_unmet"}
    assert report["protocols"]["temporal_all_channels"]["mean_test"]["precision"] is not None
    assert {r["step"] for r in logged} == {"FINAL"}
    assert {r["head"] for r in logged} == {"temperature_episode"}
    assert "temporal_all_channels" in {r["protocol"] for r in logged}


@pytest.mark.parametrize("command", ["features", "backtest", "audit"])
def test_cli_knows_subcommands(command, capsys):
    with pytest.raises(SystemExit) as stop:
        exp.main([command, "--help"])
    assert stop.value.code == 0
