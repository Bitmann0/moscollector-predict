"""Fixed 2023-24 fit; compare weekly ML to history rule on 2025-26."""
import bisect
import datetime as dt
import json
from collections import defaultdict

import polars as pl

from mkl import guard_queue, serve, store, train
from mkl.config import PATHS

START = dt.date(2023, 2, 1)
END = dt.date(2026, 6, 22)
FIT_END = dt.date(2024, 12, 23)
TEST_START = dt.date(2025, 1, 6)


def main() -> None:
    cfg = serve.load_heads()["C"]
    base = store.read_slice("object", START, END)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    event = pl.read_parquet(guard_queue.EVENT_DAYS)
    frame = guard_queue.add_alarm_history(guard_queue.current_candidates(base), event)
    frame = frame.filter(pl.col("day").dt.weekday() == 1).sort(["day", "obj"])
    positive = defaultdict(list)
    for obj, day in event.filter(pl.col("positive") == True).select("obj", "day").iter_rows():
        positive[str(obj)].append(day)
    for dates in positive.values():
        dates.sort()
    y = []
    for obj, day in frame.select("obj", "day").iter_rows():
        dates = positive[str(obj)]
        y.append(int(bisect.bisect_right(dates, day+dt.timedelta(days=8)) >
                     bisect.bisect_left(dates, day+dt.timedelta(days=2))))
    frame = frame.with_columns(pl.Series("y", y))
    features = train.feature_columns(frame)
    fit = frame.filter(pl.col("day") <= FIT_END)
    test = frame.filter(pl.col("day") >= TEST_START)
    yf = fit["y"].to_numpy()
    model = train._build_model("lgbm", {"n_estimators": 250, "num_leaves": 15,
                                       "min_child_samples": 40, "n_jobs": 4},
                               (len(yf)-yf.sum())/yf.sum())
    model.fit(train._matrix(fit, features), yf)
    test = guard_queue.priority_score(test).with_columns(pl.Series(
        "ml_score", model.predict_proba(train._matrix(test, features))[:, 1]))
    periods = ((dt.date(2025, 1, 1), dt.date(2025, 6, 30)),
               (dt.date(2025, 7, 1), dt.date(2025, 12, 31)),
               (dt.date(2026, 1, 1), END))
    result = {"fit_end": str(FIT_END), "test_start": str(TEST_START),
              "target": "recorded guarded alarm in days D+2..D+8",
              "note": "not blind; no threshold tuned on test; same current candidates",
              "features": features, "results": []}
    for min_week in (0, 3, 4):
        for method, score in (("rule", "priority_score"), ("ml", "ml_score")):
            block = test.filter(pl.col("exact_count_7") >= min_week)
            selections = pl.concat([day.sort([score, "obj"], descending=[True, False]).head(4)
                                    for day in block.partition_by("day", maintain_order=True)])
            vals = []
            for start, end in periods:
                part = selections.filter(pl.col("day").is_between(start, end))
                vals.append({"start": str(start), "end": str(end),
                             "alerts": part.height, "hits": int(part["y"].sum()),
                             "hit_rate": float(part["y"].mean()) if part.height else None})
            n, h = sum(x["alerts"] for x in vals), sum(x["hits"] for x in vals)
            result["results"].append({"min_alarm_days_in_last_7": min_week,
                                      "method": method, "periods": vals,
                                      "pooled": {"alerts": n, "hits": h,
                                                 "hit_rate": h/n if n else None}})
    path = PATHS.reports / "guard_weekly_model.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in result["results"]:
        print(row["min_alarm_days_in_last_7"], row["method"], row["pooled"],
              [(p["alerts"], p["hits"]) for p in row["periods"]])


if __name__ == "__main__":
    main()
