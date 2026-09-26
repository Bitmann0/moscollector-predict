"""Adversarial audit examples, not estimates of customer-data quality."""
import datetime as dt
import json

import duckdb
import polars as pl
import pytest

from mkl import labels, metrics, serve
from mkl import second_ml_audit as audit
from scripts import audit_second_ml as runner
from scripts.eval_a_link_policy import _threshold


D0 = dt.date(2025, 1, 1)


def day(n):
    return D0 + dt.timedelta(days=n-1)


def panel(rows):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE daily_channel (ch BIGINT, day DATE, stype VARCHAR, "
                "n_bad INTEGER, n_alarms INTEGER)")
    con.executemany("INSERT INTO daily_channel VALUES (?, ?, ?, ?, ?)", rows)
    con.execute("CREATE TABLE episodes (ch BIGINT, t_start TIMESTAMP, "
                "stype VARCHAR, dur_s DOUBLE)")
    return con


def scores(days, channels, risks, ys, objects=None):
    return pl.DataFrame({"day": days, "ch": channels, "risk": risks, "y": ys,
                         "obj": objects or ["A"]*len(days)},
                        schema_overrides={"day": pl.Date, "ch": pl.Int64,
                                          "risk": pl.Float64, "y": pl.Int64})


def test_link_label_is_not_available_until_return():
    rows = [(1, day(i), "Датчик дыма", 0, 0) for i in range(1, 9)]
    rows += [(1, day(20), "Датчик дыма", 0, 0)]
    with panel(rows) as con:
        labels.build_sensor_failure(con, "L9c", table="label_link")
        assert con.execute("SELECT y FROM label_link WHERE day=?", [day(8)]).fetchone() == (1,)
        outcomes, events = audit.build_outcomes(con, "A_link")
    unresolved = audit.labels_asof(outcomes, day(10)).filter(pl.col("day") == day(8))
    resolved = audit.labels_asof(outcomes, day(20)).filter(pl.col("day") == day(8))
    assert unresolved["y"].item() is None
    assert resolved["y"].item() == 1
    assert resolved["available_on"].item() == day(20)
    assert events["event_day"].to_list() == [day(9)]
    assert outcomes.filter(pl.col("day") == day(20))["y"].item() is None


def test_future_append_cannot_reveal_a_link_label_at_past_cutoff():
    prefix = [(1, day(i), "Датчик дыма", 0, 0) for i in range(1, 9)]
    with panel(prefix) as con:
        short, _ = audit.build_outcomes(con, "A_link")
    with panel(prefix+[(1, day(25), "Датчик дыма", 0, 0)]) as con:
        long, _ = audit.build_outcomes(con, "A_link")
    keys = ["ch", "day", "y"]
    assert audit.labels_asof(short, day(10)).select(keys).equals(
        audit.labels_asof(long, day(10)).filter(pl.col("day") <= day(8)).select(keys))


def test_missing_equipment_telemetry_is_not_a_known_negative():
    rows = [(1, day(1), "ИБП", 0, 0)]
    rows += [(2, day(i), "ИБП", 0, 0) for i in range(1, 12)]
    with panel(rows) as con:
        labels.build_wear(con)
        assert con.execute("SELECT y FROM label_wear WHERE ch=1").fetchone() == (0,)
        outcomes, _ = audit.build_outcomes(con, "D")
    assert outcomes.filter(pl.col("ch") == 1)["y"].item() is None
    assert outcomes.filter((pl.col("ch") == 2) & (pl.col("day") == day(1)))["y"].item() == 0
    feats = pl.DataFrame({"ch": [1, 2, 3], "day": [day(1)]*3,
                          "obj": ["A", "B", "C"], "stype": ["ИБП", "ИБП", "Датчик дыма"]})
    got = audit.candidates(feats, outcomes, "D", day(11))
    assert got["ch"].to_list() == [1, 2]
    assert got["y"].to_list() == [None, 0]


def test_duration_episode_waits_for_recorded_evidence():
    rows = [(1, day(i), "ИБП", 0, 0) for i in range(1, 13)]
    with panel(rows) as con:
        con.execute("INSERT INTO episodes VALUES (1, ?, 'ИБП', 172800)",
                    [dt.datetime.combine(day(8), dt.time(23, 50))])
        outcomes, _ = audit.build_outcomes(con, "D")
    row = outcomes.filter(pl.col("day") == day(1))
    assert row["available_on"].item() == day(10)
    assert audit.labels_asof(row, day(9))["y"].item() is None
    assert audit.labels_asof(row, day(10))["y"].item() == 1


def test_cooldown_changes_threshold_eligibility_and_sample_size():
    frame = scores([day(i) for i in range(1, 9)], [1]*8, [.9]*8, [0]+[1]*7)
    legacy = metrics.daily_target_operating_point(
        frame["y"].to_numpy(), frame["risk"].to_numpy(), frame["day"].to_numpy(),
        1, min_precision=.7, min_alerts=3)
    assert legacy["feasible"] and legacy["k"] == 8
    top = audit.daily_top(frame, 1, False)
    selected = audit.replay(top, legacy["threshold"])
    assert selected.filter(pl.col("alert"))["y"].to_list() == [0]
    assert not audit.select_threshold(top, .7, min_alerts=3)["feasible"]


def test_infeasible_unbounded_baseline_must_abstain_on_future_larger_scores():
    past = scores([day(1)], [1], [2.0], [0])
    future = scores([day(2)], [1], [10.0], [1])
    pick = audit.select_threshold(past, .7, min_alerts=1)
    assert not pick["feasible"]
    legacy_threshold = _threshold(pick, past)
    assert serve.alerts_over_time(future, 1, "ch", threshold=legacy_threshold)["alert"].item()
    assert not audit.replay(future, pick["threshold"])["alert"].item()


@pytest.mark.parametrize("per_object", [False, True])
def test_replay_matches_service_with_ties_gaps_unknowns_and_no_backfill(per_object):
    frame = scores([day(d) for d in (1, 2, 8, 9) for _ in range(4)],
                   [1, 2, 3, 4]*4, [.9, .9, .8, .7]*4,
                   [None, 1, 0, 1]*4, ["A", "A", "B", "C"]*4)
    top = audit.daily_top(frame.reverse(), 3, per_object)
    selected = audit.replay(top, .8)
    legacy = serve.alerts_over_time(frame, 3, "ch", cooldown_days=7,
                                    per_object=per_object, threshold=.8)
    keys = ["ch", "day"]
    assert selected.filter(pl.col("alert")).select(keys).sort(keys).equals(
        legacy.filter(pl.col("alert")).select(keys).sort(keys))
    assert selected.filter((pl.col("day") == day(2)) & pl.col("alert")).is_empty()
    assert selected.filter((pl.col("day") == day(8)) & pl.col("alert")).is_empty()
    assert selected.filter((pl.col("day") == day(9)) & pl.col("alert")).height == (2 if per_object else 3)


def test_cooldown_carries_between_refreshes_and_same_day_reruns():
    frame = scores([day(8)], [1], [.9], [1])
    assert not audit.replay(frame, .5, history=[(1, day(1))])["alert"].item()
    frame = frame.with_columns(pl.lit(day(9)).alias("day"))
    assert audit.replay(frame, .5, history=[(1, day(1))])["alert"].item()
    assert audit.replay(frame, .5, history=[(1, day(9))])["alert"].item()


def test_episode_match_uses_real_event_day_not_start_of_future_label():
    frame = scores([day(1), day(2)], [1, 1], [.9, .1], [0, 1])
    issued = frame.with_columns(pl.Series("alert", [True, False]))
    legacy = metrics.episodes_per_100_alerts(
        frame["ch"].to_numpy(), frame["day"].to_numpy(), frame["y"].to_numpy(),
        issued["alert"].to_numpy(), horizon_days=1)
    assert legacy["episodes_caught"] == 1
    events = pl.DataFrame({"ch": [1], "event_day": [day(3)],
                           "last_signal_day": [day(3)], "onset_observed": [True],
                           "episode_id": ["1:2025-01-03"]})
    got = audit.summarize(frame, issued, events, day(1), day(2), 1)
    assert got["episodes_eligible"] == 1 and got["episodes_caught"] == 0
    assert got["hits"] == 0 and got["known_misses"] == 1


def test_equipment_gap_does_not_split_continuing_case_and_repeats_are_explicit():
    rows = [(1, day(i), "ИБП", int(i in (2, 3, 10, 13)), 0)
            for i in (1, 2, 3, 10, 11, 12, 13, 20)]
    with panel(rows) as con:
        _, events = audit.build_outcomes(con, "D")
    assert events["event_day"].to_list() == [day(2), day(13)]
    assert events["last_signal_day"].to_list() == [day(10), day(13)]
    assert events["onset_observed"].to_list() == [True, True]
    frame = scores([day(1), day(3), day(11), day(12)], [1]*4, [.9]*4, [1]*4)
    got = audit.summarize(frame, frame.with_columns(pl.lit(True).alias("alert")),
                          events, day(1), day(12), 7)
    assert got["episodes_caught"] == 2
    assert got["repeat_episode_alerts"] == 1
    assert got["ongoing_episode_alerts"] == 1
    assert got["alerts_with_new_episode"] == 2


def test_unknowns_remain_in_precision_bounds_and_months_include_zero_days():
    frame = scores([day(1), day(1)], [1, 2], [.9, .8], [None, 1])
    events = pl.DataFrame(schema={"ch": pl.Int64, "event_day": pl.Date,
                                 "last_signal_day": pl.Date, "onset_observed": pl.Boolean,
                                 "episode_id": pl.String})
    issued = audit.replay(audit.daily_top(frame, 1, False), .5)
    got = audit.summarize(frame, issued, events, day(1), day(32), 1)
    assert got["unknown_alerts"] == 1 and got["known_misses"] == 0
    assert got["precision_lower_bound"] == 0 and got["precision_upper_bound"] == 1
    assert got["days_without_alerts"] == 31 and got["monthly"]["2025-02"]["alerts"] == 0


def test_original_d_artifact_gate_does_not_check_label_maturation():
    # Regression evidence about the reviewed code; no production mutation here.
    artifact = {"threshold": .7, "metadata": {"head": "D", "label": "label_wear",
                "horizon_days": 7, "operating_min_precision": .7,
                "unknown_in_budget": False, "threshold_end": str(day(9))}}
    serve.validate_pilot_artifact("D", artifact, day(10))


def test_preflight_reports_missing_data_and_preserves_provenance(tmp_path):
    got = runner.preflight(tmp_path)
    assert got["status"] == "blocked_missing_data"
    assert got["historical_metrics"] is None
    inherited = got["inherited_evidence"]
    assert inherited["heads"]["D"]["hits"] == 473
    assert inherited["heads"]["A_link"]["hits"] == 5024
    assert inherited["infeasible_baseline_emissions"]
    assert len(inherited["link_episode_overcount"]) == 5
    json.dumps(got, allow_nan=False)
    assert "Пересчёт не выполнен" in runner.markdown_report(got)


def test_unknown_episode_start_and_current_catalog_coverage():
    with panel([(1, day(1), "ИБП", 1, 0), (1, day(10), "ИБП", 0, 0)]) as con:
        _, events = audit.build_outcomes(con, "D")
    assert events["onset_observed"].to_list() == [False]
    frame = scores([day(1), day(1)], [1, 99], [.9, .8], [None, None])
    result = audit.summarize(frame, frame.with_columns(pl.lit(False).alias("alert")),
                             events, day(1), day(1), 7, 2, {1, 2})
    assert result["reference_coverage"] == .5
    assert result["channels_outside_current_catalog"] == 1


def test_unknown_equipment_counters_do_not_confirm_negative():
    with panel([(1, day(i), "ИБП", None, None) for i in range(1, 10)]) as con:
        outcomes, _ = audit.build_outcomes(con, "D")
    assert outcomes["y"].null_count() == outcomes.height


def test_duplicate_panel_rows_fail_loudly():
    row = (1, day(1), "ИБП", 0, 0)
    with panel([row, row]) as con, pytest.raises(ValueError, match="duplicate"):
        audit.build_outcomes(con, "D")


def test_empty_scoring_slice_does_not_call_model():
    frame = scores([], [], [], [])
    result = runner.score(frame, ["missing"], None, None)
    assert result.is_empty() and result["risk"].dtype == pl.Float64


def generated_history():
    start = dt.date(2023, 1, 1)
    rows, features = [], []
    for i in range(300):
        for ch in range(1, 5):
            if (i+ch) % 13 == 0:
                continue
            bad = int((i+ch) % 17 < 3)
            current = start+dt.timedelta(days=i)
            rows.append((ch, current, "ИБП", bad, 0))
            features.append({"ch": ch, "day": current, "obj": str(ch), "stype": "ИБП",
                             "n_bad_w7": float(bad), "gap_vs_own_rhythm": float(i % 5),
                             "day_of_week": current.weekday()})
    return start, rows, pl.DataFrame(features)


@pytest.mark.parametrize("head", ["D", "A_link"])
def test_weekly_historical_runner_smoke_on_generated_history(head):
    start, rows, frame = generated_history()
    cfg = serve.load_heads()[head]
    with panel(rows) as con:
        report = runner.evaluate_head(con, frame, head, cfg,
            start+dt.timedelta(days=285), n_splits=2, test_days=8,
            min_alerts=1, model_params={"n_estimators": 5, "min_child_samples": 2,
                                       "num_leaves": 7, "n_jobs": 1},
            training_start=start, reference_channels=4)
    assert len(report["refreshes"]) == 3
    assert all(r["training_rows"] > 0 for r in report["refreshes"])
    assert report["total"]["model_topk"]["alerts"] > 0
    for name, summary in report["total"].items():
        assert summary["alerts"] <= cfg["budget_per_day"]*16
        assert summary["candidates"] == report["total"]["model_topk"]["candidates"]
        assert summary["alerts"] == sum(f["policies"][name]["alerts"] for f in report["folds"])
        if head == "A_link":
            assert summary["episodes_caught"] == summary["hits"]
    json.dumps(report, allow_nan=False)
    assert "model_50" in runner.markdown_report({"status": "synthetic_test_only",
                                                "historical_metrics": [report]})


def test_historical_reads_parquet_inputs_and_fingerprints_them(tmp_path, monkeypatch):
    _, rows, frame = generated_history()
    (tmp_path/"interim").mkdir()
    (tmp_path/"features").mkdir()
    frame.write_parquet(tmp_path/"features"/"sensor.parquet")
    frame.select("ch", "stype").unique().write_parquet(tmp_path/"interim"/"channels.parquet")
    with panel(rows) as con:
        for table in ("daily_channel", "episodes"):
            con.execute(f"SELECT * FROM {table}").pl().write_parquet(tmp_path/"interim"/f"{table}.parquet")
    configs = serve.load_heads()
    for head in ("D", "A_link"):
        configs[head]["params"] = {"n_estimators": 5, "min_child_samples": 2, "num_leaves": 7}
    monkeypatch.setattr(serve, "load_heads", lambda: configs)
    result = runner.historical(tmp_path, n_splits=1, test_days=8, threads=1)
    assert result["status"] == "historical_run_complete"
    assert {row["head"] for row in result["historical_metrics"]} == {"D", "A_link"}
    assert set(result["data_sha256"]) == set(runner.INPUTS)
    assert result["runtime"]["cpu_seconds"] > 0
    assert result["runtime"]["peak_rss_bytes"] > 0
    assert all(row["total"]["model_topk"]["reference_coverage"] == 1
               for row in result["historical_metrics"])
    json.dumps(result, allow_nan=False)
