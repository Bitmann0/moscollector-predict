"""Evaluate the full Laya checkpoint as a D equipment-alert challenger."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import time
from pathlib import Path

try:
    from scripts.eval_laya_link_challenger import (
        MODEL_PACKAGE, MODEL_REPO, MODEL_SHA, WEIGHTS_BYTES, WEIGHTS_SHA256,
        _jsonl, _verified_weights, _write_json,
    )
except ModuleNotFoundError:
    from eval_laya_link_challenger import (
        MODEL_PACKAGE, MODEL_REPO, MODEL_SHA, WEIGHTS_BYTES, WEIGHTS_SHA256,
        _jsonl, _verified_weights, _write_json,
    )

QUESTION = {
    "future_equipment_signal": {
        "type": "noul",
        "instructions": (
            "Given only this equipment channel's history at the end of today, "
            "will it produce a recorded malfunction or alarm signal during "
            "the next seven days?"
        ),
        "criteria": {
            "false": "No recorded malfunction or alarm signal in the next seven days.",
            "true": "A recorded malfunction or alarm signal occurs in the next seven days.",
        },
        "labels": {"false": "B", "true": "A"},
    }
}
QUESTION_HASH = hashlib.sha256(json.dumps(
    QUESTION, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def state(row: dict) -> str:
    def n(name: str, digits: int = 2) -> str:
        value = row.get(name)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return "unknown"
        return str(round(value, digits)) if math.isfinite(value) else "unknown"

    return (
        "One equipment telemetry channel observed at the end of today. "
        f"Events today {n('n_events', 0)}, alarms today {n('n_alarms', 0)}, "
        f"bad readings today {n('n_bad', 0)}. "
        f"Events in 7 days {n('n_events_w7', 0)}, alarms in 7 days {n('n_alarms_w7', 0)}, "
        f"bad readings in 7 days {n('n_bad_w7', 0)}. "
        f"Events in 30 days {n('n_events_w30', 0)}, alarms in 30 days {n('n_alarms_w30', 0)}, "
        f"bad readings in 30 days {n('n_bad_w30', 0)}. "
        f"Active days in 7 days {n('n_active_days_w7', 0)}. "
        f"Time in alarm in 7 days {n('time_in_alarm_s_w7', 0)} seconds; "
        f"time in bad state {n('time_in_bad_s_w7', 0)} seconds. "
        f"Days since last alarm {n('days_since_last_alarm', 0)}; "
        f"days since last bad reading {n('days_since_last_bad', 0)}. "
        f"Chatter rate {n('chatter_rate')}; bad-value fraction {n('bad_value_frac')}. "
        f"Peer event z-score {n('peer_z_events')}; peer bad ratio {n('peer_ratio_bad')}. "
        f"Earlier failures {n('n_prior_failures', 0)}; days since prior failure "
        f"{n('days_since_prior_failure', 0)}."
    )


def prepare(args: argparse.Namespace) -> None:
    import polars as pl
    from mkl import calibrate, cv, db, labels, metrics, serve, store, train
    from mkl.config import EQUIPMENT_STYPES
    cfg = serve.load_heads()["D"]
    start = dt.date(2023, 1, 1)
    test_start, test_end = args.test_start, args.test_start + dt.timedelta(days=args.days - 1)
    threshold_end = test_start - dt.timedelta(days=cfg["horizon_days"] + 1)
    threshold_start = threshold_end - dt.timedelta(days=29)
    calibration_end = threshold_start - dt.timedelta(days=1)
    calibration_start = calibration_end - dt.timedelta(days=29)
    training_end = calibration_start - dt.timedelta(days=cfg["embargo_days"] + 1)
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    lab = con.execute("SELECT * FROM label_wear WHERE day >= ? AND day <= ?",
                      [start, test_end]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], start, test_end)
    feats = feats.filter(pl.col("stype").is_in(EQUIPMENT_STYPES))
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(p) for p in drop)])
    keys = [k for k in ("ch", "obj", "day") if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=keys, how="inner").sort(["day", "obj", "ch"])
    fit = train.run("D", feats, lab,
                    [cv.Split(start, training_end, calibration_start, calibration_end)],
                    params=train.params_for(cfg, "lgbm"), backend="lgbm",
                    budget_per_day=cfg["budget_per_day"], horizon_days=cfg["horizon_days"],
                    budget_per_object=bool(cfg.get("budget_per_object")))
    cal = data.filter((pl.col("day") >= calibration_start) &
                      (pl.col("day") <= calibration_end))
    thr = data.filter((pl.col("day") >= threshold_start) &
                      (pl.col("day") <= threshold_end))
    test = data.filter((pl.col("day") >= test_start) & (pl.col("day") <= test_end))
    cal = cal.filter(pl.col("y").is_not_null())
    iso = calibrate.fit_isotonic(
        fit["model"].predict_proba(train._matrix(cal, fit["feature_names"]))[:, 1],
        cal["y"].to_numpy())
    thr_risk = calibrate.apply(iso, fit["model"].predict_proba(
        train._matrix(thr, fit["feature_names"]))[:, 1])
    pick = metrics.daily_target_operating_point(
        thr["y"].to_numpy(), thr_risk, thr["day"].to_numpy(),
        cfg["budget_per_day"], min_precision=0.70, objects=thr["obj"].to_numpy(),
        min_alerts=30)
    if not pick.get("feasible"):
        raise ValueError("D threshold window has no feasible 70% point")
    test_risk = calibrate.apply(iso, fit["model"].predict_proba(
        train._matrix(test, fit["feature_names"]))[:, 1])
    scored = test.with_columns(pl.Series("lgbm_risk", test_risk))
    shortlisted = (scored.filter(pl.col("lgbm_risk") >= pick["threshold"])
                   .sort(["day", "lgbm_risk", "obj", "ch"],
                         descending=[False, True, False, False])
                   .with_columns(pl.col("day").cum_count().over("day")
                                 .alias("_rank"))
                   .filter(pl.col("_rank") <= args.shortlist).drop("_rank"))
    args.candidates.parent.mkdir(parents=True, exist_ok=True)
    with args.candidates.open("w", encoding="utf-8") as out:
        for row in shortlisted.to_dicts():
            out.write(json.dumps({"id": f"{row['day']}:{row['ch']}",
                                  "day": str(row["day"]), "ch": int(row["ch"]),
                                  "obj": row.get("obj"), "y": row.get("y"),
                                  "lgbm_risk": float(row["lgbm_risk"]),
                                  "state": state(row)}, ensure_ascii=False,
                                 allow_nan=False) + "\n")
    _write_json(args.metadata, {
        "experiment": "laya_full_english_D_rerank_v1", "model_repo": MODEL_REPO,
        "model_sha": MODEL_SHA, "weights_sha256": WEIGHTS_SHA256,
        "weights_bytes": WEIGHTS_BYTES, "model_package": MODEL_PACKAGE,
        "question_hash": QUESTION_HASH, "test_start": str(test_start),
        "test_end": str(test_end), "threshold": float(pick["threshold"]),
        "threshold_selection": pick, "shortlist_per_day": args.shortlist,
        "candidate_count": shortlisted.height,
    })
    print(json.dumps({"candidate_count": shortlisted.height,
                      "threshold": pick["threshold"]}, allow_nan=False), flush=True)


def infer(args: argparse.Namespace) -> None:
    import importlib.metadata
    import laya
    import torch
    if importlib.metadata.version("laya") != MODEL_PACKAGE.split("==")[1]:
        raise ValueError(f"requires {MODEL_PACKAGE}")
    _verified_weights(args.model_dir / "model.safetensors")
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    if metadata["question_hash"] != QUESTION_HASH:
        raise ValueError("question mismatch")
    torch.set_num_threads(args.threads)
    agent = laya.load(str(args.model_dir), device="cpu")
    rows = _jsonl(args.candidates)
    done = {row["id"] for row in _jsonl(args.scores)} if args.scores.exists() else set()
    pending = [row for row in rows if row["id"] not in done]
    args.scores.parent.mkdir(parents=True, exist_ok=True)
    with args.scores.open("a", encoding="utf-8") as out:
        for offset in range(0, len(pending), args.batch_size):
            batch = pending[offset:offset + args.batch_size]
            started = time.perf_counter()
            results = agent.predict_batch([row["state"] for row in batch], QUESTION,
                                          batch_size=args.batch_size)
            elapsed = time.perf_counter() - started
            for row, result in zip(batch, results, strict=True):
                out.write(json.dumps({"id": row["id"],
                                      "laya_risk": result["answers"]
                                      ["future_equipment_signal"]["noul"],
                                      "model_sha": MODEL_SHA,
                                      "question_hash": QUESTION_HASH,
                                      "batch_seconds": elapsed},
                                     allow_nan=False) + "\n")
            out.flush()
            print(f"scored {len(done) + offset + len(batch)}/{len(rows)}",
                  flush=True)


def score(args: argparse.Namespace) -> None:
    import polars as pl
    from mkl import metrics, serve
    candidates, scores = _jsonl(args.candidates), _jsonl(args.scores)
    keyed = {row["id"]: row for row in scores}
    if len(keyed) != len(candidates) or len(scores) != len(candidates):
        raise ValueError("missing or duplicate Laya scores")
    if any(row["model_sha"] != MODEL_SHA or row["question_hash"] != QUESTION_HASH
           for row in scores):
        raise ValueError("mixed model or question versions")
    cfg = serve.load_heads()["D"]
    frame = pl.DataFrame([{**{k: row[k] for k in ("ch", "obj", "y")},
                           "day": dt.date.fromisoformat(row["day"]),
                           "lgbm_risk": row["lgbm_risk"],
                           "laya_risk": keyed[row["id"]]["laya_risk"]}
                          for row in candidates])
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    threshold = float(metadata["threshold"])
    def served_for(col: str, cutoff: float | None) -> pl.DataFrame:
        return serve.alerts_over_time(
            frame.select("ch", "obj", "day", "y", pl.col(col).alias("risk")),
            cfg["budget_per_day"], entity="ch", cooldown_days=7,
            per_object=True, threshold=cutoff)

    def summary(served: pl.DataFrame) -> dict:
        chosen = served.filter(pl.col("alert"))
        hits = chosen.filter(pl.col("y") == 1).height
        unknown = chosen["y"].null_count()
        episodes = metrics.episodes_per_100_alerts(
            served["ch"].to_numpy(), served["day"].to_numpy(),
            served["y"].to_numpy(), served["alert"].to_numpy(), horizon_days=7)
        return {"alerts": chosen.height, "hits": hits,
                "known_misses": chosen.height - hits - unknown,
                "unknown_alerts": unknown,
                "precision_lower_bound": hits / chosen.height if chosen.height else None,
                "recall_within_shortlist": hits / served.filter(pl.col("y") == 1).height
                if served.filter(pl.col("y") == 1).height else None,
                **{f"shortlist_{key}": value for key, value in episodes.items()}}
    lgbm_served = served_for("lgbm_risk", threshold)
    laya_served = served_for("laya_risk", None)
    intersection = (lgbm_served.select("ch", "day", "alert")
                    .rename({"alert": "lgbm_alert"})
                    .join(laya_served.select("ch", "day", "alert")
                          .rename({"alert": "laya_alert"}), on=["ch", "day"])
                    .with_columns((pl.col("lgbm_alert") & pl.col("laya_alert"))
                                  .alias("alert")))
    intersection = frame.select("ch", "obj", "day", "y").join(
        intersection.select("ch", "day", "alert"), on=["ch", "day"])
    base_daily = {row["day"]: row["alert"]
                  for row in lgbm_served.group_by("day").agg(
                      pl.col("alert").sum()).to_dicts()}
    issued = []
    matched = []
    for day in sorted(frame["day"].unique().to_list()):
        candidates = frame.filter(pl.col("day") == day).select(
            "ch", "obj", "day", "y", pl.col("laya_risk").alias("risk"))
        ranked = serve._apply_budget(
            candidates, int(base_daily.get(day, 0)), per_object=True)
        ranked = serve.apply_issued_cooldown(
            ranked, "ch", day, issued, cooldown_days=7)
        issued.extend((row["ch"], day) for row in ranked.filter(pl.col("alert")).to_dicts())
        matched.append(ranked)
    matched_served = pl.concat(matched)
    result = {**metadata,
              "lgbm": summary(lgbm_served),
              "lgbm_fixed_budget": summary(served_for("lgbm_risk", None)),
              "laya": summary(laya_served),
              "laya_matched_daily_quota": summary(matched_served),
              "consensus_intersection": summary(intersection),
              "laya_score_min": frame["laya_risk"].min(),
              "laya_score_max": frame["laya_risk"].max(),
              "laya_score_unique": frame["laya_risk"].n_unique(),
              "total_recorded_batch_seconds": sum(
                  row["batch_seconds"] for row in scores) / args.batch_size,
              "decision": "research_only_until_temporal_folds"}
    _write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("prepare", "infer", "score"))
    p.add_argument("--test-start", type=dt.date.fromisoformat,
                   default=dt.date(2026, 4, 1))
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--shortlist", type=int, default=12)
    p.add_argument("--candidates", type=Path,
                   default=Path("data/tmp/laya_d_candidates.jsonl"))
    p.add_argument("--metadata", type=Path,
                   default=Path("data/tmp/laya_d_metadata.json"))
    p.add_argument("--scores", type=Path,
                   default=Path("data/tmp/laya_d_scores.jsonl"))
    p.add_argument("--report", type=Path,
                   default=Path("reports/laya_d_challenger.json"))
    p.add_argument("--model-dir", type=Path, default=Path("data/tmp/laya_full"))
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--batch-size", type=int, default=32)
    args = p.parse_args()
    {"prepare": prepare, "infer": infer, "score": score}[args.mode](args)


if __name__ == "__main__":
    main()
