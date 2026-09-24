"""Train C specifically for first guarded intrusion alarm after seven quiet days.

Quiet eligibility at feature day d uses known alarm events on d-6..d only.
The target is the existing next-day C label; no future event enters features.
"""
import datetime as dt
import json
import sys

import numpy as np
import polars as pl

from exp_intrusion_event_fusion import armed_intrusion_days, event_day
from exp_intrusion_sequence import with_history
from mkl import cv, db, labels, metrics, serve, store, train
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

START, END = dt.date(2023, 1, 1), dt.date(2025, 12, 31)


def quiet_labels(lab: pl.DataFrame,
                 event_days: set[tuple[str, dt.date]]) -> pl.DataFrame:
    rows = [(obj, day) for obj, day in lab.select("obj", "day").iter_rows()
            if day >= START + dt.timedelta(days=7)
            and not any((obj, day - dt.timedelta(days=offset)) in event_days
                        for offset in range(7))]
    keys = pl.DataFrame(rows, schema=["obj", "day"], orient="row")
    return lab.join(keys, on=["obj", "day"])


def evaluate(fit: dict, frame: pl.DataFrame, lab: pl.DataFrame,
             split, budget: int) -> dict:
    data = frame.join(lab, on=["obj", "day"]).sort(["day", "obj"])
    block = data.filter((pl.col("day") >= split.test_start) &
                        (pl.col("day") <= split.test_end))
    y, days = block["y"].to_numpy(), block["day"].to_numpy()
    p = fit["model"].predict_proba(
        train._matrix(block, fit["feature_names"]))[:, 1]
    daily = metrics.daily_budget_summary(y, p, days, budget)
    return {"n": len(y), "positives": int(y.sum()),
            "pr_auc": metrics.pr_auc(y, p),
            "daily_precision": daily["daily_precision_at_k"],
            "daily_recall": daily["daily_recall_at_k"]}


def main() -> None:
    cfg = serve.load_heads()["C"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [START, END]).pl()
    event_days = armed_intrusion_days(con)
    con.close()
    quiet = quiet_labels(lab, event_days)
    base = store.read_slice("object", START, END)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    history, _ = with_history(base)
    event = pl.concat([event_day(y) for y in (2023, 2024, 2025)])
    fused = base.join(event, on=["obj", "day"], how="left")
    count_cols = [c for c in event.columns if c.startswith("evt_") and
                  not c.startswith("evt_last_") and c != "evt_intrusion_span_s"]
    fused = fused.with_columns([pl.col(c).fill_null(0) for c in count_cols])
    fused_history, _ = with_history(fused)
    variants = {"base_all_train": (base, lab),
                "history_all_train": (history, lab),
                "base_quiet_train": (base, quiet),
                "history_quiet_train": (history, quiet),
                "event_quiet_train": (fused, quiet),
                "event_history_quiet_train": (fused_history, quiet)}
    splits = cv.walk_forward(sorted(lab["day"].unique().to_list()), 3, 90,
                             cfg["embargo_days"])
    results = {"note": "exploratory, all 2025 folds previously viewed",
               "quiet_definition": "no armed intrusion alarm on d-6..d",
               "folds": []}
    for split in splits:
        row = {"start": str(split.test_start), "end": str(split.test_end)}
        for name, (frame, fit_labels) in variants.items():
            fit = train.run("C", frame, fit_labels, [split],
                            params=train.params_for(cfg, "lgbm"),
                            budget_per_day=cfg["budget_per_day"], backend="lgbm")
            if fit["model"] is None:
                raise ValueError(f"{name}: empty fit")
            row[name] = evaluate(fit, frame, quiet, split,
                                 cfg["budget_per_day"])
        results["folds"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    results["mean"] = {
        name: {metric: float(np.mean([row[name][metric] for row in results["folds"]]))
               for metric in ("pr_auc", "daily_precision", "daily_recall")}
        for name in variants}
    (PATHS.reports / "intrusion_onset_experiment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results["mean"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
