"""Compare all-object and quiet-object daily alert policies for head C.

Both policies rank the same out-of-time predictions. Quiet eligibility is known
at decision time from the previous seven calendar days of armed alarm events.
No threshold is tuned on the test period; the daily budget is four.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from exp_intrusion_event_fusion import armed_intrusion_days
from exp_intrusion_sequence import with_history
from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)


def evaluate(block: pl.DataFrame, p: np.ndarray, events: set,
             budget: int) -> dict:
    obj = block["obj"].to_numpy()
    days = block["day"].to_numpy()
    y = block["y"].to_numpy()
    quiet = np.array([
        not any((o, d - dt.timedelta(days=lag)) in events for lag in range(7))
        for o, d in zip(obj, days)], dtype=bool)
    if not np.array_equal(quiet & (y == 1),
                          np.array([
                              (o, d + dt.timedelta(days=1)) in events
                              for o, d in zip(obj, days)], dtype=bool) & quiet):
        raise AssertionError("onset labels disagree with observed event days")
    variants = {"all": np.ones(len(y), dtype=bool), "quiet_only": quiet}
    out = {"n_candidates": len(y), "n_proxy_positive": int(y.sum()),
           "n_quiet_candidates": int(quiet.sum()),
           "n_new_episodes": int((quiet & (y == 1)).sum())}
    for name, candidates in variants.items():
        idx = np.flatnonzero(candidates)
        picked = np.zeros(len(y), dtype=bool)
        picked[idx] = metrics._daily_top_mask(p[idx], days[idx], budget)
        alerts = int(picked.sum())
        proxy_hits = int((picked & (y == 1)).sum())
        new_hits = int((picked & quiet & (y == 1)).sum())
        out[name] = {
            "alerts": alerts, "proxy_positive_hits": proxy_hits,
            "repeat_hits": proxy_hits - new_hits,
            "new_episode_hits": new_hits,
            "proxy_precision": proxy_hits / alerts if alerts else None,
            "new_episode_precision": new_hits / alerts if alerts else None,
            "new_episode_recall": new_hits / out["n_new_episodes"]
                if out["n_new_episodes"] else None,
        }
    return out


def main() -> None:
    cfg = serve.load_heads()["C"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    events = armed_intrusion_days(con)
    con.close()
    base = store.read_slice("object", START, END)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    history, _ = with_history(base)
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"note": "2025 exploratory folds; C proxy, not confirmed intrusion",
               "budget_per_day": cfg["budget_per_day"], "folds": []}
    for split in splits:
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, frame in (("baseline", base), ("history", history)):
            fit = train.run("C", frame, lab, [split],
                            params=train.params_for(cfg, "lgbm"),
                            budget_per_day=cfg["budget_per_day"], backend="lgbm")
            block = frame.join(lab, on=["obj", "day"]).sort(
                ["day", "obj"]).filter(
                    (pl.col("day") >= split.test_start) &
                    (pl.col("day") <= split.test_end))
            p = fit["model"].predict_proba(
                train._matrix(block, fit["feature_names"]))[:, 1]
            row[name] = evaluate(block, p, events, cfg["budget_per_day"])
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    results["total"] = {}
    for variant in ("baseline", "history"):
        total = {}
        for policy in ("all", "quiet_only"):
            sums = {key: sum(r[variant][policy][key] for r in results["folds"])
                    for key in ("alerts", "proxy_positive_hits", "repeat_hits",
                                "new_episode_hits")}
            sums["proxy_precision"] = sums["proxy_positive_hits"] / sums["alerts"]
            sums["new_episode_precision"] = sums["new_episode_hits"] / sums["alerts"]
            sums["new_episode_recall"] = sums["new_episode_hits"] / sum(
                r[variant]["n_new_episodes"] for r in results["folds"])
            total[policy] = sums
        results["total"][variant] = total
    (PATHS.reports / "intrusion_episode_policy_experiment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results["total"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
