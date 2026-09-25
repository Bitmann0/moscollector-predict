"""Paired Laya test: does a planned-work month change a dispatcher decision?

Historical telemetry states are transplanted to a future as-of date solely as
counterfactual inputs. There are no future outcome labels, so this is a behavior
and hallucination check, never a precision/recall backtest.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from mkl.maintenance import load_mapping, maintenance_context, source_sha256
from scripts.eval_laya_link_challenger import MODEL_SHA, _verified_weights


QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Choose the next human dispatcher action. A plan is not proof that work happened.",
        "criteria": {
            "verify_planned_work": "Ask maintenance staff whether the planned work actually affected this channel, then diagnose the link if it did not.",
            "remote_link_check": "Run a remote link diagnostic for a potentially unexpected telemetry gap.",
            "monitor": "Continue routine monitoring without a manual check.",
        },
    },
    "planned_cause_confirmed": {
        "type": "noul",
        "instructions": "Is there evidence that actual planned work caused this channel's telemetry gap? A schedule entry alone does not establish this.",
        "criteria": {"false": "No confirmed actual work or cause is given.",
                     "true": "Actual work and its causal connection to this channel are confirmed."},
        "labels": {"false": "B", "true": "A"},
    },
}
QUESTION_HASH = hashlib.sha256(json.dumps(
    QUESTIONS, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def render_scenarios(state: str, context: dict, forecast_day: dt.date,
                     include_issued_alert: bool = False) -> tuple[str, str]:
    """Keep telemetry identical; vary only the structured schedule evidence."""
    if context["status"] != "schedule_overlap_unconfirmed":
        raise ValueError("paired scenario requires an available planned-work month")
    work = next((m for m in context["matches"] if m["kind"] == "planned_to_month"), None)
    if work is None or work["mapping_status"] != "inferred":
        raise ValueError("this experiment requires an inferred TO month")
    prefix = f"{state} Forecast day: {forecast_day.isoformat()}. "
    if include_issued_alert:
        prefix += ("The A_link model issued a manual remote link diagnostic alert "
                   "for this channel. This is a recommendation, not a confirmed failure. ")
    no_plan = (prefix + "Maintenance context: no schedule entry is provided in this "
               "scenario. No actual maintenance execution or cause is confirmed.")
    with_plan = (prefix + f"Maintenance context: GASB service ({work['work_type']}) is "
                 f"planned sometime in month {work['month']} of {forecast_day.year} "
                 "for this complex. The object link is inferred, not customer-confirmed; "
                 "the exact day and actual execution are unknown. No cause is confirmed.")
    return no_plan, with_plan


def prepare(args: argparse.Namespace) -> None:
    import polars as pl

    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    mapping = load_mapping(args.mapping, schedule)
    catalog = pl.read_parquet(args.channels).select("ch", "obj_parent", "stype")
    catalog_by_ch = {r["ch"]: r for r in catalog.to_dicts()}
    seen = set()
    selected = []
    for candidate in _jsonl(args.candidates):
        ch = candidate["ch"]
        address = catalog_by_ch.get(ch)
        if ch in seen or not address or address["stype"] != "Газовый датчик":
            continue
        context = maintenance_context(
            schedule, mapping, obj_parent=address["obj_parent"],
            sensor_type=address["stype"], asof=args.asof,
            window_start=args.asof + dt.timedelta(days=1),
            window_end=args.asof + dt.timedelta(days=1))
        if context["status"] != "schedule_overlap_unconfirmed":
            continue
        if not any(m["kind"] == "planned_to_month" for m in context["matches"]):
            continue
        seen.add(ch)
        selected.append((candidate, context))
        if len(selected) >= args.max_pairs:
            break
    if not selected:
        raise ValueError("no catalog gas candidates overlap an available TO month")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as out:
        for candidate, context in selected:
            base, planned = render_scenarios(
                candidate["state"], context, args.asof + dt.timedelta(days=1),
                include_issued_alert=args.include_issued_alert)
            for variant, state in (("no_plan", base), ("planned_month", planned)):
                payload = {
                    "pair_id": candidate["id"], "variant": variant,
                    "scenario_asof": args.asof.isoformat(), "state": state,
                    "source_case_id": candidate["id"],
                    "source_case_day": candidate["day"],
                }
                if args.include_issued_alert:
                    payload["include_issued_alert"] = True
                payload["schedule_status"] = context["status"]
                out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(json.dumps({"pairs": len(selected), "states": 2 * len(selected),
                      "source_sha256": source_sha256(args.output)},
                     ensure_ascii=True), flush=True)


def infer(args: argparse.Namespace) -> None:
    import importlib.metadata
    import laya
    import torch

    if importlib.metadata.version("laya") != "0.3.20":
        raise ValueError("requires laya==0.3.20")
    _verified_weights(args.model_dir / "model.safetensors")
    torch.set_num_threads(args.threads)
    cases = _jsonl(args.cases)
    if not cases:
        raise ValueError("no prepared paired states")
    agent = laya.load(str(args.model_dir), device="cpu")
    args.scores.parent.mkdir(parents=True, exist_ok=True)
    with args.scores.open("w", encoding="utf-8") as out:
        for offset in range(0, len(cases), args.batch_size):
            batch = cases[offset:offset + args.batch_size]
            started = time.perf_counter()
            results = agent.predict_batch([r["state"] for r in batch], QUESTIONS,
                                          batch_size=args.batch_size)
            elapsed = time.perf_counter() - started
            for case, result in zip(batch, results, strict=True):
                out.write(json.dumps({
                    "pair_id": case["pair_id"], "variant": case["variant"],
                    "answers": result.get("answers", {}),
                    "model_revision": MODEL_SHA, "question_hash": QUESTION_HASH,
                    "batch_seconds": elapsed,
                }, ensure_ascii=False, allow_nan=False) + "\n")
            out.flush()
            print(f"scored {offset + len(batch)}/{len(cases)}", flush=True)


def summarize(cases: list[dict], scores: list[dict]) -> dict:
    if len(cases) != len(scores) or len({(r["pair_id"], r["variant"]) for r in scores}) != len(scores):
        raise ValueError("missing or duplicate paired scores")
    if {(r["pair_id"], r["variant"]) for r in cases} != \
            {(r["pair_id"], r["variant"]) for r in scores}:
        raise ValueError("paired states and Laya scores differ")
    if any(r["model_revision"] != MODEL_SHA or r["question_hash"] != QUESTION_HASH
           for r in scores):
        raise ValueError("mixed Laya checkpoint or question schema")
    by_pair: dict[str, dict] = {}
    route_counts: dict[str, Counter] = {"no_plan": Counter(), "planned_month": Counter()}
    cause_scores: dict[str, list[float]] = {"no_plan": [], "planned_month": []}
    for row in scores:
        variant = row["variant"]
        answers = row["answers"]
        route_raw = answers.get("route", {})
        route = route_raw.get("choice", route_raw.get("value")) if isinstance(route_raw, dict) else route_raw
        cause_raw = answers.get("planned_cause_confirmed", {})
        cause = cause_raw.get("noul", cause_raw.get("value")) if isinstance(cause_raw, dict) else cause_raw
        route_counts[variant][str(route)] += 1
        if cause is not None:
            cause_scores[variant].append(float(cause))
        by_pair.setdefault(row["pair_id"], {})[variant] = str(route)
    changed = sum(pair.get("no_plan") != pair.get("planned_month")
                  for pair in by_pair.values())
    return {
        "experiment": "laya_schedule_counterfactual_behavior_only",
        "pairs": len(by_pair), "paired_route_changes": changed,
        "route_counts": {k: dict(v) for k, v in route_counts.items()},
        "mean_claimed_cause_score": {
            k: sum(v) / len(v) if v else None for k, v in cause_scores.items()},
        "model_revision": MODEL_SHA, "question_hash": QUESTION_HASH,
        "predictive_metric_valid": False,
        "reason": "telemetry states were transplanted to a later date; no post-release outcomes or actual work labels exist",
    }


def score(args: argparse.Namespace) -> None:
    cases = _jsonl(args.cases)
    report = summarize(cases, _jsonl(args.scores))
    report["include_issued_alert"] = all(r.get("include_issued_alert", False) for r in cases)
    report["case_file_sha256"] = source_sha256(args.cases)
    report["score_file_sha256"] = source_sha256(args.scores)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "infer", "score"))
    parser.add_argument("--schedule", type=Path,
                        default=Path("data/interim/maintenance_2026.json"))
    parser.add_argument("--mapping", type=Path,
                        default=Path("resources/maintenance_mapping_candidates.json"))
    parser.add_argument("--channels", type=Path,
                        default=Path("data/interim/channels.parquet"))
    parser.add_argument("--candidates", type=Path,
                        default=Path("data/tmp/laya_link_candidates.jsonl"))
    parser.add_argument("--asof", type=dt.date.fromisoformat,
                        default=dt.date(2026, 11, 1))
    parser.add_argument("--max-pairs", type=int, default=8)
    parser.add_argument("--include-issued-alert", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=Path("data/tmp/laya_maintenance_pairs.jsonl"))
    parser.add_argument("--cases", type=Path,
                        default=Path("data/tmp/laya_maintenance_pairs.jsonl"))
    parser.add_argument("--scores", type=Path,
                        default=Path("data/tmp/laya_maintenance_scores.jsonl"))
    parser.add_argument("--report", type=Path,
                        default=Path("reports/laya_maintenance_counterfactual.json"))
    parser.add_argument("--model-dir", type=Path, default=Path("data/tmp/laya_full"))
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    {"prepare": prepare, "infer": infer, "score": score}[args.mode](args)


if __name__ == "__main__":
    main()
