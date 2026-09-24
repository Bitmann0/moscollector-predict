"""Post-selection 2026 check; this period was already viewed repeatedly."""
import datetime as dt
import json
import sys

from mkl import db, labels, serve, store, train
from mkl.config import PATHS
from mkl.cv import Split

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    cfg = serve.load_heads()["B"]
    start = dt.date(2023, 1, 1)
    train_end = dt.date(2025, 11, 30)
    test_start = dt.date(2026, 1, 1)
    test_end = dt.date(2026, 6, 29)  # 30 June has no observable next day.
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_fire(con)
    lab = con.execute("SELECT * FROM label_fire WHERE day BETWEEN ? AND ?",
                      [start, test_end]).pl()
    con.close()
    feat = store.read_slice("segment", start, test_end)
    original = feat.select([col for col in feat.columns
                            if not col.startswith(("sensor_", "fire_", "days_since_fire"))])
    history = feat.select([col for col in feat.columns if not col.startswith("sensor_")])
    split = Split(start, train_end, test_start, test_end)
    result = {}
    for name, frame in (("original", original), ("history", history)):
        fit = train.run("B", frame, lab, [split], backend="lgbm",
                        params=train.params_for(cfg, "lgbm"),
                        budget_per_day=cfg["budget_per_day"])
        fold = fit["folds"][0]
        result[name] = {key: fold[key] for key in
                        ("n", "n_pos", "base_rate", "pr_auc", "roc_auc",
                         "daily_precision_at_k", "daily_recall_at_k",
                         "episode_recall")}
        print(name, json.dumps(result[name]), flush=True)
    (PATHS.reports / "fire_2026_check.json").write_text(
        json.dumps({"warning": "2026 period previously inspected; not a sealed holdout",
                    "train_end": str(train_end), "test_start": str(test_start),
                    "test_end": str(test_end), "results": result},
                   ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
