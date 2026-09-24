"""Fixed model/policy comparison for alarms armed at the event timestamp.

Run exp_intrusion_label_timing.py first. Labels require telemetry tomorrow;
unresolved guard states are not negative examples. Threshold selection uses
60 past days, separated by 31-day gaps from both training and future testing.
Test periods were previously explored for other targets: this is retrospective.
"""
import bisect
import datetime as dt
import json
import sys
import time
from collections import defaultdict

import numpy as np
import polars as pl
from catboost import CatBoostClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from exp_intrusion_event_fusion import event_day
from exp_intrusion_sequence import with_history
from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")
START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 30)


def build_data():
    cfg = serve.load_heads()["C"]
    base = store.read_slice("object", START, END + dt.timedelta(days=1))
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    base_cols = train.feature_columns(base)
    history, _ = with_history(base)
    event = pl.concat([event_day(y) for y in (2023, 2024, 2025)])
    full = history.join(event, on=["obj", "day"], how="left")
    count_cols = [c for c in event.columns if c.startswith("evt_") and
                  not c.startswith("evt_last_") and c != "evt_intrusion_span_s"]
    full = full.with_columns([pl.col(c).fill_null(0) for c in count_cols])
    audit = pl.read_parquet(PATHS.features / "intrusion_target_audit.parquet")
    positive, unknown, observed = defaultdict(list), defaultdict(list), defaultdict(list)
    for obj, day, pos, amb in audit.select(
            "obj", "day", "alarm_flag_and_asof", "unknown_guard_candidate").iter_rows():
        if pos:
            positive[obj].append(day)
        if amb:
            unknown[obj].append(day)
    for obj, day in base.select("obj", "day").iter_rows():
        observed[obj].append(day)
    for mapping in (positive, unknown, observed):
        for dates in mapping.values():
            dates.sort()
    pos_set = {(o, d) for o, dates in positive.items() for d in dates}
    amb_set = {(o, d) for o, dates in unknown.items() for d in dates}
    obs_set = {(o, d) for o, dates in observed.items() for d in dates}

    def count(dates, day, window):
        return bisect.bisect_right(dates, day) - bisect.bisect_left(
            dates, day - dt.timedelta(days=window-1))

    rows = []
    for obj, day in full.select("obj", "day").iter_rows():
        tomorrow = day + dt.timedelta(days=1)
        pos = positive[obj]
        right = bisect.bisect_right(pos, day)
        c1, c7, c30 = (count(pos, day, w) for w in (1, 7, 30))
        quiet = c7 == 0 and count(unknown[obj], day, 7) == 0 and count(
            observed[obj], day, 7) == 7
        y = int((obj, tomorrow) in pos_set)
        eligible = ((obj, tomorrow) in obs_set and
                    (y == 1 or (obj, tomorrow) not in amb_set))
        rows.append((obj, day, c1, c7, c30,
                     min((day-pos[right-1]).days, 366) if right else 366,
                     quiet, eligible, y))
    extra = pl.DataFrame(rows, schema=["obj", "day", "exact_count_1", "exact_count_7",
        "exact_count_30", "exact_age", "quiet", "eligible", "exact_y"], orient="row")
    full = full.join(extra, on=["obj", "day"])
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    old = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [START, END]).pl().rename({"y": "legacy_y"})
    con.close()
    data = full.join(old, on=["obj", "day"]).filter(
        pl.col("eligible") & (pl.col("day") >= START + dt.timedelta(days=30)))
    data = data.rename({"exact_y": "y"}).sort(["day", "obj"])
    feature_cols = [c for c in train.feature_columns(data)
                    if c not in ("legacy_y", "quiet", "eligible")]
    return data, base_cols, feature_cols, cfg


def measures(y, p, days, k, threshold=None):
    result = metrics.daily_budget_summary(y, p, days, k, threshold=threshold)
    n = result["daily_alerts"]
    precision = result["daily_precision_at_k"]
    return {"alerts": n, "hits": round(precision*n) if n else 0,
            "precision": precision if n else None,
            "recall": result["daily_recall_at_k"] if y.sum() else None}


def evaluate(cal, test, pc, pt):
    out = {}
    for name in ("all", "quiet"):
        cm = np.ones(len(cal), dtype=bool) if name == "all" else cal["quiet"].to_numpy()
        tm = np.ones(len(test), dtype=bool) if name == "all" else test["quiet"].to_numpy()
        cy, ty = cal["y"].to_numpy()[cm], test["y"].to_numpy()[tm]
        cd, td = cal["day"].to_numpy()[cm], test["day"].to_numpy()[tm]
        cp, tp = pc[cm], pt[tm]
        row = {"n": len(ty), "positives": int(ty.sum()),
               "pr_auc": metrics.pr_auc(ty, tp),
               "top1": measures(ty, tp, td, 1), "top4": measures(ty, tp, td, 4)}
        for target in (0.5, 0.7):
            choice = metrics.daily_target_operating_point(
                cy, cp, cd, 4, min_precision=target, min_alerts=20)
            threshold = choice["threshold"] if choice["feasible"] else float("inf")
            row[f"precision_{target}"] = {
                "selection": choice,
                "test": measures(ty, tp, td, 4, threshold=threshold)}
        out[name] = row
    return out


def main():
    data, base_cols, full_cols, cfg = build_data()
    # Fixed alternatives; no tuning on the future blocks.
    variants = ("legacy_lgb", "exact_lgb", "exact_counts_lgb", "exact_history_lgb",
                "exact_recent_lgb", "exact_logistic", "exact_catboost")
    splits = cv.walk_forward(data["day"].unique().to_list(), 3, 90, 31)
    results = {"note": "new event-time proxy; previously explored calendar periods, not blind",
               "threshold_protocol": "60 days selection, 31 days embargo on either side, min 20 alerts",
               "rows": len(data), "positives": int(data["y"].sum()), "folds": []}
    prediction_rows = []
    for split in splits:
        cal_end = split.test_start - dt.timedelta(days=32)
        cal_start = cal_end - dt.timedelta(days=59)
        fit_end = cal_start - dt.timedelta(days=32)
        tr = data.filter(pl.col("day") <= fit_end)
        cal = data.filter(pl.col("day").is_between(cal_start, cal_end))
        test = data.filter(pl.col("day").is_between(split.test_start, split.test_end))
        row = {"train_end": str(fit_end), "selection_start": str(cal_start),
               "selection_end": str(cal_end), "test_start": str(split.test_start),
               "test_end": str(split.test_end), "variants": {}}
        for name in variants:
            cols = base_cols if name in ("legacy_lgb", "exact_lgb") else full_cols
            if name == "exact_counts_lgb":
                cols = base_cols + [c for c in full_cols if c.startswith("exact_")]
            xtr, xc, xt = [train._matrix(f, cols) for f in (tr, cal, test)]
            ytr = tr["legacy_y" if name == "legacy_lgb" else "y"].to_numpy()
            started = time.perf_counter()
            if name == "exact_logistic":
                model = make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                    StandardScaler(), LogisticRegression(C=0.1, class_weight="balanced",
                                                        max_iter=1500, random_state=42))
                model.fit(xtr, ytr)
            elif name == "exact_catboost":
                model = CatBoostClassifier(iterations=500, depth=6, learning_rate=0.05,
                    auto_class_weights="Balanced", task_type="CPU", thread_count=4,
                    random_seed=42, verbose=False, allow_writing_files=False)
                model.fit(xtr, ytr)
            else:
                model = train._build_model("lgbm", {**cfg["params"], "n_jobs": 4},
                                           (len(ytr)-ytr.sum())/ytr.sum())
                weights = None
                if name == "exact_recent_lgb":
                    age = (np.datetime64(fit_end) - tr["day"].to_numpy()).astype("timedelta64[D]").astype(float)
                    weights = np.exp2(-age/180)
                model.fit(xtr, ytr, sample_weight=weights)
            pc, pt = model.predict_proba(xc)[:, 1], model.predict_proba(xt)[:, 1]
            row["variants"][name] = evaluate(cal, test, pc, pt)
            row["variants"][name]["seconds"] = time.perf_counter()-started
            prediction_rows.append(test.select("obj", "day", "y", "quiet").with_columns(
                pl.Series("p", pt, dtype=pl.Float64), pl.lit(name).alias("model")))
            print(split.test_start, name, json.dumps({view: row["variants"][name][view]["top4"]
                  for view in ("all", "quiet")}), flush=True)
        results["folds"].append(row)
        (PATHS.tmp / "intrusion_precise_suite_checkpoint.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    pl.concat(prediction_rows).write_parquet(PATHS.features / "intrusion_precise_suite_predictions.parquet")
    results["pooled"] = {}
    for name in variants:
        pooled = {}
        for view in ("all", "quiet"):
            npos = sum(f["variants"][name][view]["positives"] for f in results["folds"])
            summary = {"positives": npos}
            for policy in ("top1", "top4", "precision_0.5", "precision_0.7"):
                vals = [f["variants"][name][view][policy] for f in results["folds"]]
                if policy.startswith("precision"):
                    vals = [v["test"] for v in vals]
                hits, alerts = sum(v["hits"] for v in vals), sum(v["alerts"] for v in vals)
                summary[policy] = {"alerts": alerts, "hits": hits,
                    "precision": hits/alerts if alerts else None,
                    "recall": hits/npos if npos else None}
            pooled[view] = summary
        results["pooled"][name] = pooled
    (PATHS.reports / "intrusion_precise_suite.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(results["pooled"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
