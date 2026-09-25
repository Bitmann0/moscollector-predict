"""One frozen, local Laya challenger test for A_link's daily shortlist.

Run prepare/score with the project environment; run infer with an isolated
environment containing laya==0.3.20. The full official English checkpoint is
loaded from a pinned local snapshot. Test outcomes never enter the model state.

This is a *reranker* experiment: the existing LightGBM selects up to 40
eligible channels per day, then both methods compete for the same 20 daily
slots with the same seven-day no-backfill cooldown. It is not a full-population
Laya replacement test and does not change the deployed policy.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import time
from pathlib import Path


MODEL_REPO = "convaiinnovations/laya"
MODEL_SHA = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
WEIGHTS_SHA256 = "891102d372688fc2a094dac56a384bc537b87c63f21f9f3dac0be2b7cbc8d86c"
WEIGHTS_BYTES = 842_609_210
MODEL_PACKAGE = "laya==0.3.20"
STATE_VERSION = "link_rhythm_state_v1"
QUESTION = {
    "unusual_gap": {
        "type": "noul",
        "instructions": (
            "Given the telemetry history available at the end of this day, "
            "will this channel begin an unusually long absence of reports "
            "tomorrow relative to its own recent reporting rhythm?"
        ),
        "criteria": {
            "false": "The channel follows its usual reporting rhythm tomorrow.",
            "true": "An unusually long reporting gap starts tomorrow.",
        },
        "labels": {"false": "B", "true": "A"},
    },
    "recommended_action": {
        "type": "choice",
        "instructions": "What should the dispatcher do with this candidate?",
        "criteria": {
            "manual_link_diagnostics": "Schedule a remote link diagnostic.",
            "defer": "Do not schedule a diagnostic yet.",
            "insufficient_data": "Ask for more telemetry before deciding.",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgently should this candidate be reviewed?",
        "criteria": ["routine", "soon", "urgent"],
    },
}
QUESTION_HASH = hashlib.sha256(json.dumps(
    {"state_version": STATE_VERSION, "question": QUESTION},
    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _num(row: dict, name: str, digits: int = 2) -> str:
    value = row.get(name)
    if value is None:
        return "unknown"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "unknown"
    return str(round(value, digits)) if math.isfinite(value) else "unknown"


def render_state(row: dict) -> str:
    """Compact, ID-free evidence from features known at forecast time."""
    return (
        "One telemetry channel, observed at the end of today. "
        f"Reports today: {_num(row, 'n_events', 0)}. "
        f"Reports in past 7 days: {_num(row, 'n_events_w7', 0)}; "
        f"past 30 days: {_num(row, 'n_events_w30', 0)}. "
        f"Days with reports in past 7 days: {_num(row, 'n_active_days_w7', 0)}; "
        f"past 30 days: {_num(row, 'n_active_days_w30', 0)}. "
        f"Last inter-report gap in days: {_num(row, 'prev_gap_days')}; "
        f"mean of recent gaps: {_num(row, 'prev_gap_days_mean_w30')}; "
        f"largest recent gap: {_num(row, 'prev_gap_days_max_w30')}. "
        f"Latest gap divided by own usual rhythm: {_num(row, 'gap_vs_own_rhythm')}. "
        f"Activity-days ratio: {_num(row, 'activity_days_ratio')}. "
        f"Observed activity frequency on this weekday: {_num(row, 'dow_active_rate_w12')}. "
        f"Previous unusual gap episodes: {_num(row, 'n_prior_episodes', 0)}; "
        f"days since previous episode: {_num(row, 'days_since_prior_episode', 0)}."
    )


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               allow_nan=False), encoding="utf-8")


def _verified_weights(path: Path) -> None:
    if not path.exists() or path.stat().st_size != WEIGHTS_BYTES:
        raise ValueError("expected the full, unquantized official Laya checkpoint")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != WEIGHTS_SHA256:
        raise ValueError("Laya checkpoint SHA256 mismatch")


def prepare(args: argparse.Namespace) -> None:
    import polars as pl
    from mkl import calibrate, cv, db, labels, metrics, serve, store, train
    try:
        from scripts.eval_a_link_policy import _summary_unknown
        from scripts.eval_d_live_policy import _period, _scored
    except ModuleNotFoundError:  # python scripts/eval_laya_link_challenger.py
        from eval_a_link_policy import _summary_unknown
        from eval_d_live_policy import _period, _scored

    cfg = serve.load_heads()["A_link"]
    if cfg["variant"] != "L9c" or cfg["budget_per_day"] != 20:
        raise ValueError("A_link target or budget changed; redesign the challenger")
    test_start = args.test_start
    test_end = test_start + dt.timedelta(days=args.days - 1)
    start = dt.date(2023, 1, 1)
    threshold_end = test_start - dt.timedelta(days=cfg["horizon_days"] + 1)
    threshold_start = threshold_end - dt.timedelta(days=29)
    calibration_end = threshold_start - dt.timedelta(days=1)
    calibration_start = calibration_end - dt.timedelta(days=29)
    training_end = calibration_start - dt.timedelta(days=cfg["embargo_days"] + 1)

    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    labels.build_for_head(con, cfg)
    lab = con.execute(
        "SELECT * FROM label_link WHERE day >= ? AND day <= ?",
        [start, test_end]).pl()
    con.close()
    feats = store.read_slice(cfg["feature_set"], start, test_end)
    drop = cfg.get("drop_feature_prefixes") or []
    if drop:
        feats = feats.select([c for c in feats.columns
                              if not any(c.startswith(prefix) for prefix in drop)])
    keys = [k for k in ("ch", "obj", "day")
            if k in feats.columns and k in lab.columns]
    data = feats.join(lab, on=keys, how="left").sort(["day", "obj", "ch"])
    fit = train.run(
        "A_link", feats, lab,
        [cv.Split(start, training_end, calibration_start, calibration_end)],
        params=train.params_for(cfg, "lgbm"), backend="lgbm",
        budget_per_day=cfg["budget_per_day"],
        horizon_days=cfg["horizon_days"])
    model, names = fit["model"], fit["feature_names"]
    if model is None:
        raise ValueError("no train/calibration positives")
    cal = _period(data, calibration_start, calibration_end).filter(
        pl.col("y").is_not_null())
    thr = _period(data, threshold_start, threshold_end)
    test = _period(data, test_start, test_end)
    iso = calibrate.fit_isotonic(
        model.predict_proba(train._matrix(cal, names))[:, 1],
        cal["y"].to_numpy())
    threshold_scores = _scored(thr, model, iso, names)
    pick = metrics.daily_target_operating_point(
        threshold_scores["y"].fill_null(0).to_numpy(),
        threshold_scores["risk"].to_numpy(),
        threshold_scores["day"].to_numpy(), cfg["budget_per_day"],
        min_precision=0.50, min_alerts=30)
    if not pick.get("feasible"):
        raise ValueError("frozen 50% baseline would abstain on this past window")
    threshold = float(pick["threshold"])
    risk = calibrate.apply(
        iso, model.predict_proba(train._matrix(test, names))[:, 1])
    scored = test.with_columns(pl.Series("risk", risk))
    slim = scored.select("ch", "obj", "day", "y", "risk")
    full_baseline = _summary_unknown(slim, threshold, cfg)
    args.full_eval.parent.mkdir(parents=True, exist_ok=True)
    slim.write_parquet(args.full_eval)

    # Laya sees only the old model's shortlist, never y, IDs, object names,
    # or the old risk. Stable ties make this deterministic across reruns.
    shortlisted = (scored.filter(pl.col("risk") >= threshold)
                   .sort(["day", "risk", "obj", "ch"],
                         descending=[False, True, False, False])
                   .with_columns(pl.col("day").cum_count().over("day")
                                 .alias("_candidate_rank"))
                   .filter(pl.col("_candidate_rank") <= args.shortlist)
                   .drop("_candidate_rank"))
    shortlist_baseline = _summary_unknown(
        shortlisted.select("ch", "obj", "day", "y", "risk"), threshold, cfg)
    if (shortlist_baseline["alerts"], shortlist_baseline["hits"]) != \
            (full_baseline["alerts"], full_baseline["hits"]):
        raise AssertionError("shortlist changed the original LightGBM policy")

    args.candidates.parent.mkdir(parents=True, exist_ok=True)
    with args.candidates.open("w", encoding="utf-8") as output:
        for row in shortlisted.to_dicts():
            item = {"id": f"{row['day']}:{row['ch']}",
                    "day": str(row["day"]), "ch": int(row["ch"]),
                    "obj": row.get("obj"), "y": row.get("y"),
                    "lgbm_risk": float(row["risk"]),
                    "state": render_state(row)}
            output.write(json.dumps(item, ensure_ascii=False,
                                    allow_nan=False) + "\n")
    _write_json(args.metadata, {
        "experiment": "laya_full_english_a_link_rerank_v1",
        "model_repo": MODEL_REPO, "model_sha": MODEL_SHA,
        "weights_sha256": WEIGHTS_SHA256, "weights_bytes": WEIGHTS_BYTES,
        "model_package": MODEL_PACKAGE,
        "state_version": STATE_VERSION, "question_hash": QUESTION_HASH,
        "test_start": str(test_start), "test_end": str(test_end),
        "training_end": str(training_end),
        "calibration": [str(calibration_start), str(calibration_end)],
        "threshold_window": [str(threshold_start), str(threshold_end)],
        "threshold": threshold, "threshold_selection": pick,
        "shortlist_per_day": args.shortlist,
        "candidate_count": shortlisted.height,
        "candidate_days": shortlisted["day"].n_unique(),
        "full_lgbm_policy": full_baseline,
    })
    print(json.dumps({"candidate_count": shortlisted.height,
                      "threshold": threshold, "baseline": full_baseline},
                     ensure_ascii=False, allow_nan=False), flush=True)


def infer(args: argparse.Namespace) -> None:
    import importlib.metadata
    import laya
    import torch

    if importlib.metadata.version("laya") != MODEL_PACKAGE.split("==")[1]:
        raise ValueError(f"requires {MODEL_PACKAGE}")
    _verified_weights(args.model_dir / "model.safetensors")
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    if metadata["model_sha"] != MODEL_SHA or metadata["question_hash"] != QUESTION_HASH:
        raise ValueError("model or prompt mismatch")
    torch.set_num_threads(args.threads)
    agent = laya.load(str(args.model_dir), device="cpu")
    rows = _jsonl(args.candidates)
    done = {x["id"] for x in _jsonl(args.scores)} if args.scores.exists() else set()
    if len(done) != (len(_jsonl(args.scores)) if args.scores.exists() else 0):
        raise ValueError("duplicate inference IDs")
    pending = [row for row in rows if row["id"] not in done]
    args.scores.parent.mkdir(parents=True, exist_ok=True)
    with args.scores.open("a", encoding="utf-8") as output:
        for offset in range(0, len(pending), args.batch_size):
            batch = pending[offset:offset + args.batch_size]
            started = time.perf_counter()
            results = agent.predict_batch([row["state"] for row in batch],
                                          QUESTION, batch_size=args.batch_size)
            elapsed = time.perf_counter() - started
            for row, result in zip(batch, results, strict=True):
                p = float(result["answers"]["unusual_gap"]["noul"])
                if not 0 <= p <= 1 or not math.isfinite(p):
                    raise ValueError("Laya returned an invalid probability")
                answers = result["answers"]
                action = answers["recommended_action"]
                urgency = answers["urgency"]
                output.write(json.dumps({"id": row["id"], "laya_risk": p,
                                         "laya_action": action.get("choice"),
                                         "laya_action_confidence": action.get("confidence"),
                                         "laya_urgency": urgency.get("score"),
                                         "laya_urgency_confidence": urgency.get("confidence"),
                                         "model_sha": MODEL_SHA,
                                         "question_hash": QUESTION_HASH,
                                         "batch_seconds": elapsed},
                                        allow_nan=False) + "\n")
            output.flush()
            if offset % (args.batch_size * 10) == 0:
                print(f"scored {len(done) + offset + len(batch)}/{len(rows)} "
                      f"in {elapsed:.2f}s for latest batch", flush=True)


def score(args: argparse.Namespace) -> None:
    import polars as pl
    from mkl import metrics, serve
    try:
        from scripts.eval_a_link_policy import _summary_unknown
    except ModuleNotFoundError:  # python scripts/eval_laya_link_challenger.py
        from eval_a_link_policy import _summary_unknown

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    candidates = _jsonl(args.candidates)
    scores = _jsonl(args.scores)
    keyed = {row["id"]: row for row in scores}
    partial = len(keyed) != len(candidates)
    if partial and not args.allow_partial:
        raise ValueError("missing or duplicate Laya scores")
    if len(keyed) != len(scores):
        raise ValueError("duplicate Laya scores")
    candidates = [row for row in candidates if row["id"] in keyed]
    if any(row["model_sha"] != MODEL_SHA or row["question_hash"] != QUESTION_HASH
           for row in scores):
        raise ValueError("mixed model or prompt versions")
    cfg = serve.load_heads()["A_link"]
    frame = pl.DataFrame([{**{k: row[k] for k in ("ch", "obj", "y")},
                           "day": dt.date.fromisoformat(row["day"]),
                           "lgbm_risk": row["lgbm_risk"],
                           "laya_risk": keyed[row["id"]]["laya_risk"]}
                          for row in candidates])
    threshold = float(metadata["threshold"])
    baseline = _summary_unknown(
        frame.select("ch", "obj", "day", "y",
                     pl.col("lgbm_risk").alias("risk")), threshold, cfg)
    if not partial and (baseline["alerts"], baseline["hits"]) != \
            (metadata["full_lgbm_policy"]["alerts"],
             metadata["full_lgbm_policy"]["hits"]):
        raise AssertionError("LightGBM baseline changed during scoring")
    challenger = _summary_unknown(
        frame.select("ch", "obj", "day", "y",
                     pl.col("laya_risk").alias("risk")), None, cfg)
    full = pl.read_parquet(args.full_eval).select("ch", "day", "y")

    def policy_summary(served: pl.DataFrame) -> dict:
        chosen = served.filter(pl.col("alert"))
        hits = chosen.filter(pl.col("y") == 1).height
        unknown = chosen["y"].null_count()
        return {
            "alerts": chosen.height, "hits": hits,
            "known_misses": chosen.height - hits - unknown,
            "unknown_alerts": unknown,
            "precision_lower_bound": hits / chosen.height if chosen.height else None,
            "precision_known_only": hits / (chosen.height - unknown)
            if chosen.height > unknown else None,
            "recall_known": hits / full_positive_rows if full_positive_rows else None,
        }

    def full_episode_recall(risk_col: str, cutoff: float | None) -> dict:
        ranked = frame.select(
            "ch", "obj", "day", "y", pl.col(risk_col).alias("risk"))
        served = serve.alerts_over_time(
            ranked, cfg["budget_per_day"], entity="ch",
            cooldown_days=7, threshold=cutoff)
        issued = served.filter(pl.col("alert")).select("ch", "day").with_columns(
            pl.lit(True).alias("alert"))
        population = full.join(issued, on=["ch", "day"], how="left").with_columns(
            pl.col("alert").fill_null(False))
        return metrics.episodes_per_100_alerts(
            population["ch"].to_numpy(), population["day"].to_numpy(),
            population["y"].fill_null(0).to_numpy(),
            population["alert"].to_numpy(), horizon_days=1)

    full_baseline_episodes = (full_episode_recall("lgbm_risk", threshold)
                              if not partial else None)
    full_laya_episodes = (full_episode_recall("laya_risk", None)
                          if not partial else None)
    full_positive_rows = full.filter(pl.col("y") == 1).height
    lgbm_served = serve.alerts_over_time(
        frame.select("ch", "obj", "day", "y",
                     pl.col("lgbm_risk").alias("risk")),
        cfg["budget_per_day"], entity="ch", cooldown_days=7,
        threshold=threshold)
    laya_served = serve.alerts_over_time(
        frame.select("ch", "obj", "day", "y",
                     pl.col("laya_risk").alias("risk")),
        cfg["budget_per_day"], entity="ch", cooldown_days=7)
    join_keys = ["ch", "day"]
    consensus = (lgbm_served.select(join_keys + ["alert"])
                 .rename({"alert": "lgbm_alert"})
                 .join(laya_served.select(join_keys + ["alert"])
                       .rename({"alert": "laya_alert"}), on=join_keys)
                 .with_columns((pl.col("lgbm_alert") & pl.col("laya_alert"))
                               .alias("alert")))
    consensus = frame.select(join_keys + ["y"]).join(
        consensus.select(join_keys + ["alert"]), on=join_keys)
    consensus_summary = policy_summary(consensus)
    ranked = frame.with_columns(
        (pl.col("lgbm_risk").rank("ordinal", descending=True).over("day") /
         pl.col("lgbm_risk").count().over("day")).alias("lgbm_rank"),
        (pl.col("laya_risk").rank("ordinal", descending=True).over("day") /
         pl.col("laya_risk").count().over("day")).alias("laya_rank"))
    ranked = ranked.with_columns(
        ((1.0 - pl.col("lgbm_rank") + 1.0 - pl.col("laya_rank")) / 2.0)
        .alias("ensemble_risk"))
    ensemble_summary = _summary_unknown(
        ranked.select("ch", "obj", "day", "y",
                      pl.col("ensemble_risk").alias("risk")), None, cfg)
    if partial:
        for key in ("episodes", "episodes_caught", "episode_recall",
                    "episodes_per_100_alerts"):
            ensemble_summary.pop(key, None)
    action_counts = {}
    action_hits = {}
    for row in candidates:
        score_row = keyed[row["id"]]
        action = score_row.get("laya_action") or "missing"
        action_counts[action] = action_counts.get(action, 0) + 1
        if row.get("y") == 1:
            action_hits[action] = action_hits.get(action, 0) + 1
    for summary, episode in ((baseline, full_baseline_episodes),
                             (challenger, full_laya_episodes)):
        # _summary_unknown sees only the shortlist here. Its local recall
        # would overstate coverage by roughly an order of magnitude, so report
        # the whole test population instead of silently keeping that value.
        summary["shortlist_recall_known"] = summary.pop("recall_known")
        summary["recall_known"] = (
            summary["hits"] / full_positive_rows if full_positive_rows else None)
        if episode is not None:
            for key in ("episodes", "episodes_caught", "episode_recall",
                        "episodes_per_100_alerts"):
                summary[key] = episode[key]
        elif partial:
            for key in ("episodes", "episodes_caught", "episode_recall",
                        "episodes_per_100_alerts"):
                summary.pop(key, None)
    result = {**metadata, "weights_sha256": WEIGHTS_SHA256,
              "weights_bytes": WEIGHTS_BYTES, "laya": challenger,
              "lgbm_same_shortlist": baseline,
              "consensus_intersection": consensus_summary,
              "rank_ensemble": ensemble_summary,
              "action_counts": action_counts,
              "action_hits_on_recorded_positive": action_hits,
              "full_population_episodes": {
                  "lgbm": full_baseline_episodes,
                  "laya": full_laya_episodes},
              "full_positive_rows": full_positive_rows,
              "laya_score_min": frame["laya_risk"].min(),
              "laya_score_max": frame["laya_risk"].max(),
              "laya_score_unique": frame["laya_risk"].n_unique(),
              "total_recorded_batch_seconds": sum(
                  row["batch_seconds"] for row in scores) / args.batch_size,
              "partial_inference": partial,
              "decision": (
                  "routing_slice_only" if partial else
                  "needs_more_temporal_folds" if
                  challenger["precision_lower_bound"] is not None and
                  challenger["precision_lower_bound"] >
                  baseline["precision_lower_bound"] and
                  challenger["hits"] > baseline["hits"] else
                  "research_only_not_better")}
    _write_json(args.report, result)
    print(json.dumps({"lgbm": baseline, "laya": challenger,
                      "decision": result["decision"]},
                     ensure_ascii=False, allow_nan=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "infer", "score"))
    parser.add_argument("--test-start", type=dt.date.fromisoformat,
                        default=dt.date(2026, 4, 1))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--shortlist", type=int, default=40)
    parser.add_argument("--candidates", type=Path,
                        default=Path("data/tmp/laya_link_candidates.jsonl"))
    parser.add_argument("--metadata", type=Path,
                        default=Path("data/tmp/laya_link_metadata.json"))
    parser.add_argument("--scores", type=Path,
                        default=Path("data/tmp/laya_link_scores.jsonl"))
    parser.add_argument("--full-eval", type=Path,
                        default=Path("data/tmp/laya_link_full_eval.parquet"))
    parser.add_argument("--report", type=Path,
                        default=Path("reports/laya_link_challenger.json"))
    parser.add_argument("--model-dir", type=Path,
                        default=Path("data/tmp/laya_full"))
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--allow-partial", action="store_true",
                        help="write a routing slice report when inference was interrupted")
    args = parser.parse_args()
    if args.days <= 0 or args.shortlist < 20 or args.batch_size <= 0:
        parser.error("days > 0, shortlist >= 20 and batch-size > 0 required")
    {"prepare": prepare, "infer": infer, "score": score}[args.mode](args)


if __name__ == "__main__":
    main()
