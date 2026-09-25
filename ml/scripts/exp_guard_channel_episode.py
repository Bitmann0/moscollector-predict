"""Out-of-time test of channel features for first guarded-alarm episodes.

Forecast Monday D -> recorded episode onset D+2..D+8, restricted to objects
with 30-day alarm history but fewer than four alarm days in the last week.
An episode begins after more than seven calendar days without a positive day.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import random
from collections import Counter, defaultdict

import numpy as np
import polars as pl
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from mkl import db, guard_queue, serve, store, train
from mkl.config import INTRUSION_STATES, PATHS


START = dt.date(2023, 2, 1)
END = dt.date(2026, 6, 22)
FIT_END = dt.date(2024, 12, 23)
TEST_START = dt.date(2025, 1, 6)
WINDOW = 7
HISTORY_START = START - dt.timedelta(days=31)
EVENT_STOP = END + dt.timedelta(days=9)


def channel_alarm_days() -> dict[str, list[tuple[dt.date, str, int]]]:
    """Apply the same event-time armed check as the v2 target, retaining ch."""
    con = db.connect("6GB", 4)
    db.attach_events(con)
    quoted = ",".join("'" + x.replace("'", "''") + "'" for x in sorted(INTRUSION_STATES))
    rows = con.execute(f"""
      WITH guards AS (
        SELECT obj, ts AS guard_ts,
          CASE WHEN count(DISTINCT val_raw)=1
               THEN max(CASE WHEN val_raw='На охране' THEN 1 ELSE 0 END)
               ELSE NULL END AS armed
        FROM ev WHERE val_raw IN ('На охране', 'Снято с охраны')
          AND ts < DATE '{EVENT_STOP}'
        GROUP BY obj, ts
      ), alarms AS (
        SELECT a.obj, a.ch, a.ts,
          CASE WHEN g.guard_ts=a.ts THEN NULL ELSE g.armed END AS armed
        FROM (SELECT obj, ch, ts FROM ev
              WHERE alarm AND val_raw IN ({quoted})
                AND ts >= DATE '{HISTORY_START}' AND ts < DATE '{EVENT_STOP}') a
        ASOF LEFT JOIN guards g ON a.obj=g.obj AND a.ts>=g.guard_ts
      )
      SELECT obj, ch, CAST(ts AS DATE) AS day, count(*) AS n
      FROM alarms WHERE armed=1 AND ch IS NOT NULL
      GROUP BY obj, ch, CAST(ts AS DATE)
    """).fetchall()
    con.close()
    by_obj = defaultdict(list)
    for obj, ch, day, count in rows:
        by_obj[str(obj)].append((day, str(ch), count))
    for values in by_obj.values():
        values.sort()
    return by_obj


def add_channel_history(frame: pl.DataFrame,
                        alarm_days: dict[str, list[tuple[dt.date, str, int]]]
                        ) -> tuple[pl.DataFrame, list[str]]:
    names = ["channel_count_7", "channel_count_30", "channel_events_7",
             "channel_events_30", "channel_top_share_30", "channel_new_7",
             "channel_recent_fraction"]
    by_obj = {obj: ([r[0] for r in rows], rows)
              for obj, rows in alarm_days.items()}
    values = []
    for obj, day in frame.select("obj", "day").iter_rows():
        dates, rows = by_obj.get(str(obj), ([], []))
        end = bisect.bisect_right(dates, day)
        week = bisect.bisect_left(dates, day - dt.timedelta(days=6))
        month = bisect.bisect_left(dates, day - dt.timedelta(days=29))
        week_counts = Counter()
        month_counts = Counter()
        prior_counts = Counter()
        for _, ch, n in rows[month:week]:
            prior_counts[ch] += n
            month_counts[ch] += n
        for _, ch, n in rows[week:end]:
            week_counts[ch] += n
            month_counts[ch] += n
        n7, n30 = sum(week_counts.values()), sum(month_counts.values())
        values.append((len(week_counts), len(month_counts), n7, n30,
                       max(month_counts.values(), default=0) / n30 if n30 else 0.0,
                       len(week_counts.keys() - prior_counts.keys()),
                       n7 / n30 if n30 else 0.0))
    return frame.hstack(pl.DataFrame(values, schema=names, orient="row")), names


def pick(frame: pl.DataFrame, score: str, budget: int,
         cooldown: int = 0) -> list[tuple[str, dt.date]]:
    last = {}
    selected = []
    for day in frame.partition_by("day", maintain_order=True):
        chosen = 0
        for row in day.sort([score, "obj"], descending=[True, False]).to_dicts():
            obj = str(row["obj"])
            date = row["day"]
            if obj in last and (date - last[obj]).days <= cooldown:
                continue
            selected.append((obj, date))
            last[obj] = date
            chosen += 1
            if chosen == budget:
                break
    return selected


def evaluate(selected: list[tuple[str, dt.date]], outcomes: dict,
             test_start: dt.date = TEST_START, test_end: dt.date = END) -> dict:
    hit = sum(outcomes[key]["onset"] for key in selected)
    periods = {}
    for year in sorted({date.year for _, date in selected}):
        block = [key for key in selected if key[1].year == year]
        periods[str(year)] = {"selected": len(block),
                           "onset_hits": sum(outcomes[key]["onset"] for key in block),
                           "unknown": sum(not outcomes[key]["known"] for key in block),
                           "unique_objects": len({obj for obj, _ in block})}
    return {"selected": len(selected), "onset_hits": hit,
            "onset_hit_rate": hit / len(selected) if selected else None,
            "recorded_any_hits": sum(outcomes[key]["any"] for key in selected),
            "unknown": sum(not outcomes[key]["known"] for key in selected),
            "unique_objects": len({obj for obj, _ in selected}),
            "periods": periods,
            "onset_recall": hit / sum(r["onset"] for k, r in outcomes.items()
                                        if test_start <= k[1] <= test_end and r["middle"])}


def paired_weeks(a: list[tuple[str, dt.date]], b: list[tuple[str, dt.date]],
                 outcomes: dict) -> dict:
    """Paired weekly hit-count difference, including zero-pick weeks."""
    by_a, by_b = defaultdict(int), defaultdict(int)
    for key in a:
        by_a[key[1]] += int(outcomes[key]["onset"])
    for key in b:
        by_b[key[1]] += int(outcomes[key]["onset"])
    days = sorted(set(by_a) | set(by_b))
    diffs = [by_a[day] - by_b[day] for day in days]
    rng = random.Random(42)
    samples = sorted(sum(rng.choice(diffs) for _ in days) for _ in range(5000))
    month_diffs = defaultdict(int)
    for day, diff in zip(days, diffs):
        month_diffs[(day.year, day.month)] += diff
    months = list(month_diffs.values())
    month_samples = sorted(sum(rng.choice(months) for _ in months)
                           for _ in range(5000))
    return {"weeks": len(days), "extra_hits": sum(diffs),
            "better_weeks": sum(x > 0 for x in diffs),
            "worse_weeks": sum(x < 0 for x in diffs),
            "equal_weeks": sum(x == 0 for x in diffs),
            "bootstrap_95pct_extra_hits": [samples[125], samples[4874]],
            "bootstrap_month_95pct_extra_hits": [month_samples[125], month_samples[4874]],
            "note": "exploratory paired resampling; reused objects and post-hoc task choice limit inference"}


def main() -> None:
    cfg = serve.load_heads()["C"]
    drop = cfg.get("drop_feature_prefixes") or []
    base = store.read_slice("object", START, END)
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    event = pl.read_parquet(guard_queue.EVENT_DAYS)
    frame = guard_queue.add_alarm_history(guard_queue.current_candidates(base), event)
    frame = frame.filter((pl.col("day").dt.weekday() == 1) &
                         (pl.col("exact_count_30") > 0) &
                         (pl.col("exact_count_7") < 4)).sort(["day", "obj"])
    baseline_features = train.feature_columns(frame)
    alarm_channel_days = channel_alarm_days()
    channel, channel_features = add_channel_history(frame, alarm_channel_days)
    positive = defaultdict(list)
    unresolved = set()
    for obj, day, pos, unk in event.select("obj", "day", "positive", "unresolved").iter_rows():
        obj = str(obj)
        if pos:
            positive[obj].append(day)
        if unk:
            unresolved.add((obj, day))
    event_positive_days = {(obj, day) for obj, days in positive.items()
                           for day in days if HISTORY_START <= day < EVENT_STOP}
    channel_positive_days = {(obj, day) for obj, rows in alarm_channel_days.items()
                             for day, _, _ in rows}
    if event_positive_days != channel_positive_days:
        raise AssertionError("channel event query disagrees with v2 event-day cache; rebuild cache")
    onsets = {}
    for obj, days in positive.items():
        days.sort()
        onsets[obj] = [day for i, day in enumerate(days)
                       if i == 0 or (day - days[i-1]).days > WINDOW]
    observed = {(str(obj), day) for obj, day in pl.read_parquet(
        PATHS.features / "object.parquet", columns=["obj", "day"]).iter_rows()}
    outcomes = {}
    for obj, day in frame.select("obj", "day").iter_rows():
        obj = str(obj)
        left, right = day + dt.timedelta(days=2), day + dt.timedelta(days=8)
        any_hit = (bisect.bisect_right(positive[obj], right) >
                   bisect.bisect_left(positive[obj], left))
        onset = (bisect.bisect_right(onsets[obj], right) >
                 bisect.bisect_left(onsets[obj], left))
        known = any_hit or all((obj, day + dt.timedelta(days=i)) in observed and
                               (obj, day + dt.timedelta(days=i)) not in unresolved
                               for i in range(2, 9))
        outcomes[(obj, day)] = {"any": any_hit, "onset": onset, "known": known,
                                "middle": True}
    y = [int(outcomes[(str(obj), day)]["onset"])
         for obj, day in frame.select("obj", "day").iter_rows()]
    known = [outcomes[(str(obj), day)]["known"]
             for obj, day in frame.select("obj", "day").iter_rows()]
    frame = frame.with_columns(pl.Series("y", y), pl.Series("known", known))
    channel = channel.with_columns(pl.Series("y", y), pl.Series("known", known))
    result = {"target": "new recorded guarded-alarm episode onset D+2..D+8",
              "episode_gap_days": WINDOW, "fit_end": str(FIT_END),
              "test_start": str(TEST_START),
              "unknown_note": "training uses absence of a recorded onset, not proof of no physical event; evaluation counts unknown separately",
              "channel_day_agreement": {
                  "event_positive_days": len(event_positive_days),
                  "channel_positive_days": len(channel_positive_days),
                  "missing_channel_days": len(event_positive_days - channel_positive_days),
                  "extra_channel_days": len(channel_positive_days - event_positive_days),
              },
              "channel_features": channel_features,
              "fit_rows": int(frame.filter(pl.col("day") <= FIT_END).height),
              "fit_onsets": int(frame.filter(pl.col("day") <= FIT_END)["y"].sum()),
              "test_rows": int(frame.filter(pl.col("day") >= TEST_START).height),
              "test_onsets": int(frame.filter(pl.col("day") >= TEST_START)["y"].sum()),
              "policies": {}, "strata_by_recent_alarm_days": {},
              "strata_zero_recent_by_age": {}}
    selections = {}
    for period, subset in (("fit", frame.filter(pl.col("day") <= FIT_END)),
                           ("test", frame.filter(pl.col("day") >= TEST_START))):
        result["strata_by_recent_alarm_days"][period] = [
            {"n7": n7[0] if isinstance(n7, tuple) else n7,
             "rows": block.height, "onsets": int(block["y"].sum())}
            for n7, block in subset.partition_by("exact_count_7", as_dict=True).items()
        ]
        zero = subset.filter(pl.col("exact_count_7") == 0).with_columns(
            (pl.col("exact_age") // 7).alias("age_week"))
        result["strata_zero_recent_by_age"][period] = [
            {"age_week": age[0] if isinstance(age, tuple) else age,
             "rows": block.height, "onsets": int(block["y"].sum())}
            for age, block in zero.partition_by("age_week", as_dict=True).items()
        ]
    for name, data, features, known_only in (
            ("object_model", frame, baseline_features, False),
            ("object_model_known_fit", frame, baseline_features, True),
            ("channel_model", channel, baseline_features + channel_features, False)):
        fit = data.filter(pl.col("day") <= FIT_END)
        if known_only:
            fit = fit.filter(pl.col("known"))
        test = data.filter(pl.col("day") >= TEST_START)
        yf = fit["y"].to_numpy()
        model = train._build_model("lgbm", {"n_estimators": 100, "num_leaves": 7,
                                           "min_child_samples": 40, "n_jobs": 4},
                                   (len(yf)-yf.sum())/yf.sum())
        model.fit(train._matrix(fit, features), yf)
        result.setdefault("model_fit_rows", {})[name] = fit.height
        result.setdefault("top_feature_importances", {})[name] = sorted(
            zip(features, model.feature_importances_.tolist()),
            key=lambda row: row[1], reverse=True)[:15]
        test = guard_queue.priority_score(test).with_columns(
            (-100 * pl.col("exact_count_7") - pl.col("exact_age")).alias(
                "onset_rule_score"),
            pl.Series("ml_score", model.predict_proba(
                train._matrix(test, features))[:, 1]))
        for score_name, score in (("repeat_rule", "priority_score"),
                                  ("onset_rule", "onset_rule_score"),
                                  (name, "ml_score")):
            if score_name in ("repeat_rule", "onset_rule") and name != "object_model":
                continue
            for budget in (1, 4):
                for cooldown in (0, 28):
                    key = f"{score_name}_k{budget}_cd{cooldown}"
                    selections[key] = pick(test, score, budget, cooldown)
                    result["policies"][key] = evaluate(selections[key], outcomes)
    result["paired_comparisons"] = {}
    for budget in (1, 4):
        for a_name, b_name in (("object_model", "onset_rule"),
                               ("channel_model", "object_model")):
            a_key = f"{a_name}_k{budget}_cd28"
            b_key = f"{b_name}_k{budget}_cd28"
            result["paired_comparisons"][f"{a_name}_vs_{b_name}_k{budget}_cd28"] = paired_weeks(
                selections[a_key], selections[b_key], outcomes)
    early_fit_end = dt.date(2023, 12, 18)
    early_test_start = dt.date(2024, 1, 1)
    early_test_end = dt.date(2024, 12, 23)
    early_fit = frame.filter(pl.col("day") <= early_fit_end)
    early_test = frame.filter(pl.col("day").is_between(early_test_start, early_test_end))
    ye = early_fit["y"].to_numpy()
    early_model = train._build_model("lgbm", {"n_estimators": 100,
                                               "num_leaves": 7,
                                               "min_child_samples": 40,
                                               "n_jobs": 4},
                                     (len(ye)-ye.sum())/ye.sum())
    early_model.fit(train._matrix(early_fit, baseline_features), ye)
    early_test = early_test.with_columns(
        (-100 * pl.col("exact_count_7") - pl.col("exact_age")).alias(
            "onset_rule_score"),
        pl.Series("ml_score", early_model.predict_proba(
            train._matrix(early_test, baseline_features))[:, 1]))
    early_rule = pick(early_test, "onset_rule_score", 1, 28)
    early_ml = pick(early_test, "ml_score", 1, 28)
    result["earlier_temporal_check"] = {
        "fit_end": str(early_fit_end), "test_start": str(early_test_start),
        "test_end": str(early_test_end), "fit_rows": early_fit.height,
        "fit_onsets": int(ye.sum()), "test_rows": early_test.height,
        "test_onsets": int(early_test["y"].sum()),
        "onset_rule_k1_cd28": evaluate(early_rule, outcomes,
                                        early_test_start, early_test_end),
        "object_model_k1_cd28": evaluate(early_ml, outcomes,
                                          early_test_start, early_test_end),
        "paired": paired_weeks(early_ml, early_rule, outcomes),
    }
    profiles = {
        "alarm_history": ["exact_count_7", "exact_count_30", "exact_age"],
        "alarm_and_telemetry": ["exact_count_7", "exact_count_30", "exact_age",
                                "days_since_arm_event", "n_channels",
                                "n_intrusion", "n_alarms_w30", "n_bad_w30"],
    }
    result["simple_models"] = {}
    for profile, cols in profiles.items():
        for split_name, fit_end, test_start, test_end in (
                ("2024", early_fit_end, early_test_start, early_test_end),
                ("2025_2026", FIT_END, TEST_START, END),
                ("2025", FIT_END, TEST_START, dt.date(2025, 12, 22)),
                ("2026", dt.date(2025, 12, 22), dt.date(2026, 1, 5), END)):
            fit = frame.filter(pl.col("day") <= fit_end)
            test = frame.filter(pl.col("day").is_between(test_start, test_end))
            xfit = train._matrix(fit, cols)
            xtest = train._matrix(test, cols)
            xfit[~np.isfinite(xfit)] = np.nan
            xtest[~np.isfinite(xtest)] = np.nan
            linear = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                                   StandardScaler(),
                                   LogisticRegression(max_iter=1000, class_weight="balanced"))
            linear.fit(xfit, fit["y"].to_numpy())
            test = test.with_columns(
                (-100 * pl.col("exact_count_7") - pl.col("exact_age")).alias(
                    "onset_rule_score"),
                pl.Series("linear_score", linear.predict_proba(xtest)[:, 1]))
            block = {"features": cols, "fit_rows": fit.height,
                     "standardized_coefficients": dict(zip(
                         cols, linear.named_steps["logisticregression"].coef_[0].tolist()))}
            for budget in (1, 4):
                rule = pick(test, "onset_rule_score", budget, 28)
                model = pick(test, "linear_score", budget, 28)
                block[f"k{budget}"] = {
                    "rule": evaluate(rule, outcomes, test_start, test_end),
                    "model": evaluate(model, outcomes, test_start, test_end),
                    "paired": paired_weeks(model, rule, outcomes),
                }
            result["simple_models"][f"{profile}_{split_name}"] = block
    path = PATHS.reports / "guard_channel_episode.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
