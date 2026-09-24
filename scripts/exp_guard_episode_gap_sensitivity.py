"""Check whether the one-object onset result depends on a seven-day gap."""
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

from exp_guard_channel_episode import END, FIT_END, START, TEST_START, pick
from mkl import guard_queue, store, train
from mkl.config import PATHS


def main() -> None:
    event = pl.read_parquet(guard_queue.EVENT_DAYS)
    positive = defaultdict(list)
    for obj, day in event.filter(pl.col("positive") == True).select(
            "obj", "day").iter_rows():
        positive[str(obj)].append(day)
    for days in positive.values():
        days.sort()
    base = store.read_slice("object", START, END, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    frame = guard_queue.add_alarm_history(
        guard_queue.current_candidates(base).filter(pl.col("day").dt.weekday() == 1),
        event).filter((pl.col("exact_count_30") > 0) &
                      (pl.col("exact_count_7") < 4)).sort(["day", "obj"])
    cols = ["exact_count_7", "exact_count_30", "exact_age"]
    result = {"cohort": "middle-history guarded objects, Monday D, D+2..D+8",
              "fixed_ranking": "one object per week, 28-day cooldown",
              "fit_end": str(FIT_END), "test_start": str(TEST_START), "gaps": {}}
    for gap in (3, 7, 14):
        onsets = {obj: [day for i, day in enumerate(days)
                        if i == 0 or (day - days[i - 1]).days > gap]
                  for obj, days in positive.items()}
        labels = []
        by_key = {}
        for obj, day in frame.select("obj", "day").iter_rows():
            obj = str(obj)
            dates = onsets[obj]
            hit = int(bisect.bisect_right(dates, day + dt.timedelta(days=8)) >
                      bisect.bisect_left(dates, day + dt.timedelta(days=2)))
            labels.append(hit)
            by_key[(obj, day)] = hit
        labeled = frame.with_columns(pl.Series("y", labels))
        fit = labeled.filter(pl.col("day") <= FIT_END)
        test = labeled.filter(pl.col("day") >= TEST_START)
        model = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                              StandardScaler(),
                              LogisticRegression(max_iter=1000, class_weight="balanced"))
        model.fit(train._matrix(fit, cols), fit["y"].to_numpy())
        test = test.with_columns(
            (-100 * pl.col("exact_count_7") - pl.col("exact_age")).alias("rule"),
            pl.Series("model", model.predict_proba(train._matrix(test, cols))[:, 1]))
        policies = {}
        for name in ("rule", "model"):
            selected = pick(test, name, 1, 28)
            hits = sum(by_key[key] for key in selected)
            policies[name] = {"selected": len(selected), "onset_hits": hits,
                              "hit_rate": hits / len(selected)}
        result["gaps"][str(gap)] = {"fit_onsets": int(fit["y"].sum()),
                                    "test_onsets": int(test["y"].sum()),
                                    "policies": policies}
    (PATHS.reports / "guard_episode_gap_sensitivity.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
