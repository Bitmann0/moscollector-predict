"""How much positive evidence of channel activity precedes a recorded onset?

No daily channel event does NOT prove a telemetry outage: many sensors may be
event-driven. This audit only counts evidence of recent reporting on the exact
channels that later emitted a guarded alarm.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
from collections import defaultdict

import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from exp_guard_channel_episode import (
    END, FIT_END, HISTORY_START, START, TEST_START, channel_alarm_days, pick,
)
from mkl import guard_queue, store, train
from mkl.config import PATHS


def main() -> None:
    event = pl.read_parquet(guard_queue.EVENT_DAYS)
    by_obj = defaultdict(list)
    for obj, day in event.filter(pl.col("positive") == True).select(
            "obj", "day").iter_rows():
        by_obj[str(obj)].append(day)
    onsets = {}
    for obj, days in by_obj.items():
        days.sort()
        onsets[obj] = [day for i, day in enumerate(days)
                       if i == 0 or (day - days[i - 1]).days > 7]
    base = store.read_slice("object", START, END, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    frame = guard_queue.add_alarm_history(
        guard_queue.current_candidates(base).filter(pl.col("day").dt.weekday() == 1),
        event).filter((pl.col("exact_count_30") > 0) &
                      (pl.col("exact_count_7") < 4)).sort(["day", "obj"])
    target_onsets = set()
    test_target_onsets = set()
    for obj, day in frame.select("obj", "day").iter_rows():
        obj = str(obj)
        dates = onsets[obj]
        left = bisect.bisect_left(dates, day + dt.timedelta(days=2))
        right = bisect.bisect_right(dates, day + dt.timedelta(days=8))
        target_onsets.update((obj, date) for date in dates[left:right])
        if day >= TEST_START:
            test_target_onsets.update((obj, date) for date in dates[left:right])
    channel_days = channel_alarm_days()
    trigger_channels = defaultdict(set)
    for obj, rows in channel_days.items():
        for day, ch, _ in rows:
            if (obj, day) in target_onsets:
                trigger_channels[(obj, day)].add(int(ch))
    if set(trigger_channels) != target_onsets:
        raise AssertionError("missing triggering channel for a positive onset")
    relevant = {ch for channels in trigger_channels.values() for ch in channels}
    daily = pl.scan_parquet(PATHS.interim / "daily_channel.parquet").select(
        "ch", "day", "n_events").filter(
        pl.col("ch").is_in(relevant) &
        pl.col("day").is_between(HISTORY_START, END + dt.timedelta(days=8)) &
        (pl.col("n_events") > 0)).collect()
    active = defaultdict(list)
    for ch, day in daily.select("ch", "day").iter_rows():
        active[int(ch)].append(day)
    for ch, days in active.items():
        active[ch] = sorted(set(days))
    object_daily = pl.scan_parquet(PATHS.features / "object.parquet").select(
        "obj", "day", "n_events").filter(
        pl.col("day").is_between(HISTORY_START, END + dt.timedelta(days=8)) &
        (pl.col("n_events") > 0)).collect()
    object_active = {(str(obj), day) for obj, day in object_daily.select(
        "obj", "day").iter_rows()}
    records = []
    for (obj, day), channels in sorted(trigger_channels.items()):
        counts = []
        prior_30 = []
        for ch in channels:
            days = active[ch]
            end = bisect.bisect_left(days, day)
            week = bisect.bisect_left(days, day - dt.timedelta(days=7))
            month = bisect.bisect_left(days, day - dt.timedelta(days=30))
            counts.append(end - week)
            prior_30.append(end > month)
        records.append({"obj": obj, "day": day, "trigger_channels": len(channels),
                        "any_trigger_active_7d": any(n > 0 for n in counts),
                        "all_trigger_active_7d": all(n > 0 for n in counts),
                        "any_trigger_active_30d": any(prior_30),
                        "object_active_days_prior_7d": sum(
                            (obj, day - dt.timedelta(days=i)) in object_active
                            for i in range(1, 8)),
                        "total_trigger_active_channel_days_7d": sum(counts)})
    test_records = [r for r in records if (r["obj"], r["day"]) in test_target_onsets]
    result = {"target": "recorded guarded-alarm episode onset in D+2..D+8",
              "unit": "unique object-onset day in the 2025-2026 middle-history cohort",
              "limitation": "an event-driven sensor can be healthy without daily events; activity is positive evidence only",
              "total_onsets": len(test_records),
              "with_any_trigger_channel_active_prior_7d": sum(
                  r["any_trigger_active_7d"] for r in test_records),
              "with_all_trigger_channels_active_prior_7d": sum(
                  r["all_trigger_active_7d"] for r in test_records),
              "with_any_trigger_channel_active_prior_30d": sum(
                  r["any_trigger_active_30d"] for r in test_records),
              "with_any_object_event_prior_7d": sum(
                  r["object_active_days_prior_7d"] > 0 for r in test_records),
              "with_object_events_all_7d": sum(
                  r["object_active_days_prior_7d"] == 7 for r in test_records),
              "no_trigger_activity_but_object_active_prior_7d": sum(
                  not r["any_trigger_active_7d"] and
                  r["object_active_days_prior_7d"] > 0 for r in test_records),
              "by_year": {str(year): {
                  "onsets": sum(r["day"].year == year for r in test_records),
                  "any_active_7d": sum(r["day"].year == year and
                                       r["any_trigger_active_7d"] for r in test_records),
                  "all_active_7d": sum(r["day"].year == year and
                                       r["all_trigger_active_7d"] for r in test_records),
              } for year in (2025, 2026)}}
    evidence = {(r["obj"], r["day"]): r for r in records}
    replay = frame
    history_cols = ["exact_count_7", "exact_count_30", "exact_age"]
    y = []
    y_active = []
    for obj, day in replay.select("obj", "day").iter_rows():
        obj = str(obj)
        dates = onsets[obj]
        left = bisect.bisect_left(dates, day + dt.timedelta(days=2))
        right = bisect.bisect_right(dates, day + dt.timedelta(days=8))
        future = dates[left:right]
        y.append(int(bool(future)))
        y_active.append(int(any(evidence[(obj, date)]["any_trigger_active_7d"]
                                for date in future)))
    replay = replay.with_columns(pl.Series("y", y), pl.Series("y_active", y_active))
    fit = replay.filter(pl.col("day") <= FIT_END)
    test = replay.filter(pl.col("day") >= TEST_START)
    if len(test_target_onsets) != int(test["y"].sum()) or len(test_records) != len(test_target_onsets):
        raise AssertionError("onset-day count differs from held-out positive object-weeks")
    model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                          StandardScaler(),
                          LogisticRegression(max_iter=1000, class_weight="balanced"))
    model.fit(train._matrix(fit, history_cols), fit["y"].to_numpy())
    active_model = make_pipeline(
        SimpleImputer(strategy="median", keep_empty_features=True),
        StandardScaler(),
        LogisticRegression(max_iter=1000, class_weight="balanced"))
    active_model.fit(train._matrix(fit, history_cols), fit["y_active"].to_numpy())
    test = test.with_columns(
        (-100 * pl.col("exact_count_7") - pl.col("exact_age")).alias("rule_score"),
        pl.Series("model_score", model.predict_proba(
            train._matrix(test, history_cols))[:, 1]),
        pl.Series("active_model_score", active_model.predict_proba(
            train._matrix(test, history_cols))[:, 1]))

    def selected_outcomes(score: str) -> dict:
        selected = pick(test, score, 1, 28)
        hit_days = set()
        for obj, day in selected:
            for onset in onsets[obj]:
                if day + dt.timedelta(days=2) <= onset <= day + dt.timedelta(days=8):
                    hit_days.add((obj, onset))
        return {"recommendations": len(selected), "recorded_onset_hits": len(hit_days),
                "hits_with_any_trigger_channel_active_prior_7d": sum(
                    evidence[key]["any_trigger_active_7d"] for key in hit_days),
                "hits_with_all_trigger_channels_active_prior_7d": sum(
                    evidence[key]["all_trigger_active_7d"] for key in hit_days),
                "hits_with_any_trigger_channel_active_prior_30d": sum(
                    evidence[key]["any_trigger_active_30d"] for key in hit_days),
                "hits_with_any_object_event_prior_7d": sum(
                    evidence[key]["object_active_days_prior_7d"] > 0
                    for key in hit_days)}

    result["selected_one_per_week_cooldown28"] = {
        "onset_rule": selected_outcomes("rule_score"),
        "history_logistic": selected_outcomes("model_score"),
        "active_channel_target_logistic": selected_outcomes("active_model_score"),
    }
    result["active_channel_target"] = {
        "fit_positive_weeks": int(fit["y_active"].sum()),
        "test_positive_weeks": int(test["y_active"].sum()),
        "definition": "future recorded onset with at least one triggering channel active in previous seven days; this observation condition uses future outcome information, never a ranking feature",
    }
    (PATHS.reports / "guard_episode_observability.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
