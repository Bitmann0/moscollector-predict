"""Additional, previously viewed 2026 check for the chosen C history features."""
import datetime as dt
import json
import sys

from exp_intrusion_sequence import with_history
from mkl import db, labels, serve, store, train
from mkl.config import PATHS
from mkl.cv import Split

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    cfg = serve.load_heads()["C"]
    start, end = dt.date(2023, 1, 1), dt.date(2026, 6, 29)
    split = Split(start, dt.date(2025, 11, 30),
                  dt.date(2026, 1, 1), end)
    con = db.connect()
    db.attach_parquet(con, "daily_channel")
    labels.build_intrusion(con, armed_only=True)
    lab = con.execute("SELECT * FROM label_intrusion WHERE day BETWEEN ? AND ?",
                      [start, end]).pl()
    con.close()
    base = store.read_slice("object", start, end)
    drop = cfg.get("drop_feature_prefixes") or []
    base = base.select([c for c in base.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    extended, names = with_history(base)
    alarm_only = extended.drop([c for c in names if not c.startswith(
        ("seq_n_alarms_", "seq_n_intrusion_"))])
    results = {"note": "2026 already viewed by team; not a blind holdout"}
    for name, frame in (("baseline", base), ("history_tree", extended),
                        ("alarm_history_tree", alarm_only)):
        fit = train.run("C", frame, lab, [split],
                        params=train.params_for(cfg, "lgbm"),
                        budget_per_day=cfg["budget_per_day"], backend="lgbm")
        row = fit["folds"][0]
        results[name] = {
            "n": row["n"], "positives": row["n_pos"],
            "pr_auc": row["pr_auc"],
            "daily_precision": row["daily_precision_at_k"],
            "daily_recall": row["daily_recall_at_k"]}
        print(name, json.dumps(results[name], ensure_ascii=False), flush=True)
    out = PATHS.reports / "intrusion_sequence_2026.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")


if __name__ == "__main__":
    main()
