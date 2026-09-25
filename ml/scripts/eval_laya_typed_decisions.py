"""Evaluate Laya's typed-decisions checkpoint on saved ML candidate states.

This is deliberately an offline challenger.  It never changes a serving head and
keeps the model output separate from the dispatcher policy until a temporal test
has passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

MODEL_REPO = "convaiinnovations/laya"
MODEL_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"

# The id set intentionally matches Laya's documented agent_trace_observability
# typed workflow.  The labels are domain-specific, while the task remains a
# human review decision rather than an automatic work order.
QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "What should a dispatcher do with this telemetry state?",
        "criteria": {
            "manual_review": "schedule a planned manual check of the channel or equipment",
            "monitor": "continue routine monitoring and do not create a work order",
            "urgent_dispatch": "escalate for an urgent field response",
        },
    },
    "needs_review": {
        "type": "noul",
        "instructions": "Does this telemetry state deserve a manual dispatcher review?",
        "criteria": {
            "false": "No: routine monitoring is enough.",
            "true": "Yes: a dispatcher should inspect it.",
        },
        "labels": {"false": "B", "true": "A"},
    },
    "outcome": {
        "type": "choice",
        "instructions": "What is the likely near-term telemetry outcome?",
        "criteria": {
            "new_signal": "a recorded malfunction, alarm, or loss of telemetry is likely in the forecast window",
            "no_signal": "no recorded malfunction, alarm, or loss of telemetry is likely in the forecast window",
        },
    },
    "risk": {
        "type": "score",
        "instructions": "How risky is this telemetry state for a recorded future signal?",
        "criteria": [
            "low: ordinary history and no actionable change",
            "medium: some anomaly but weak evidence of a future signal",
            "high: repeated or worsening evidence that merits a manual check",
        ],
    },
    "urgency": {
        "type": "score",
        "instructions": "How soon should a dispatcher review this state?",
        "criteria": [
            "routine: review during normal planning",
            "soon: review within the next few days",
            "urgent: review as soon as possible",
        ],
    },
}
QUESTION_HASH = hashlib.sha256(
    json.dumps(QUESTIONS, ensure_ascii=False, sort_keys=True).encode()
).hexdigest()[:16]


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _answer(result: dict, question: str) -> dict:
    """Return the typed answer without assuming a single package output shape."""
    value = result.get("answers", {}).get(question, {})
    return value if isinstance(value, dict) else {"value": value}


def infer(args: argparse.Namespace) -> None:
    import importlib.metadata
    import laya
    import torch

    if importlib.metadata.version("laya") != "0.3.20":
        raise ValueError("requires laya==0.3.20")
    torch.set_num_threads(args.threads)
    rows = _rows(args.candidates)
    if args.max_rows:
        rows = rows[: args.max_rows]
    existing = {r["id"] for r in _rows(args.scores)}
    pending = [r for r in rows if r["id"] not in existing]
    # A local typed-decisions directory is preferred.  Passing a HF repo plus
    # subfolder also works, but keeping the path explicit makes the checkpoint
    # version auditable in CI and in the report.
    agent = laya.load(str(args.model_dir), device="cpu")
    args.scores.parent.mkdir(parents=True, exist_ok=True)
    with args.scores.open("a", encoding="utf-8") as out:
        for offset in range(0, len(pending), args.batch_size):
            batch = pending[offset : offset + args.batch_size]
            started = time.perf_counter()
            results = agent.predict_batch(
                [r["state"] for r in batch], QUESTIONS, batch_size=args.batch_size
            )
            elapsed = time.perf_counter() - started
            for row, result in zip(batch, results, strict=True):
                answers = {qid: _answer(result, qid) for qid in QUESTIONS}
                out.write(json.dumps({
                    "id": row["id"],
                    "answers": answers,
                    "model_revision": MODEL_REVISION,
                    "question_hash": QUESTION_HASH,
                    "batch_seconds": elapsed,
                }, ensure_ascii=False, allow_nan=False) + "\n")
            out.flush()
            print(f"scored {min(len(rows), offset + len(batch))}/{len(rows)}", flush=True)


def _choice(answer: dict) -> str | None:
    value = answer.get("choice", answer.get("value"))
    return str(value) if value is not None else None


def score(args: argparse.Namespace) -> None:
    candidates = _rows(args.candidates)
    scores = _rows(args.scores)
    if len(scores) != len(candidates) or {r["id"] for r in scores} != {r["id"] for r in candidates}:
        raise ValueError("missing or duplicate typed Laya scores")
    if any(r.get("model_revision") != MODEL_REVISION or r.get("question_hash") != QUESTION_HASH for r in scores):
        raise ValueError("mixed typed checkpoint or question schema")
    by_id = {r["id"]: r for r in scores}
    rows = []
    for candidate in candidates:
        answer = by_id[candidate["id"]]["answers"]
        rows.append({
            "id": candidate["id"],
            "y": candidate.get("y"),
            "action": _choice(answer["action"]),
            "outcome": _choice(answer["outcome"]),
            "needs_review": answer["needs_review"].get("noul"),
            "risk": answer["risk"].get("score"),
            "urgency": answer["urgency"].get("score"),
        })
    known = [r for r in rows if r["y"] in (0, 1)]
    def positive(pred):
        chosen = [r for r in known if pred(r)]
        hits = sum(r["y"] == 1 for r in chosen)
        return {"alerts": len(chosen), "hits": hits,
                "precision": hits / len(chosen) if chosen else None}
    base_rate = sum(r["y"] == 1 for r in known) / len(known) if known else None
    summary = {
        "candidate_count": len(rows),
        "known_count": len(known),
        "unknown_count": len(rows) - len(known),
        "needs_review_top_half": positive(lambda r: float(r["needs_review"] or 0) >= 0.5),
        "action_manual_or_urgent": positive(lambda r: r["action"] in {"manual_review", "urgent_dispatch"}),
        "outcome_new_signal": positive(lambda r: r["outcome"] == "new_signal"),
        "action_counts": {},
        "outcome_counts": {},
        "risk_band_counts": {},
        "urgency_band_counts": {},
        "shortlist_positive_rate": base_rate,
        "batch_seconds": sum(float(r.get("batch_seconds", 0)) for r in scores) / max(args.batch_size, 1),
        "model_revision": MODEL_REVISION,
        "question_hash": QUESTION_HASH,
    }
    for key in ("action", "outcome"):
        counts: dict[str, int] = {}
        for row in rows:
            value = row[key]
            counts[str(value)] = counts.get(str(value), 0) + 1
        summary[f"{key}_counts"] = counts
    for key, dest in (("risk", "risk_band_counts"), ("urgency", "urgency_band_counts")):
        counts: dict[str, int] = {}
        for row in rows:
            value = row[key]
            band = str(round(float(value))) if value is not None else "unknown"
            counts[band] = counts.get(band, 0) + 1
        summary[dest] = counts
    for name in ("needs_review_top_half", "action_manual_or_urgent", "outcome_new_signal"):
        precision = summary[name]["precision"]
        summary[name]["lift_vs_shortlist"] = (
            precision / base_rate if precision is not None and base_rate else None
        )
    _write(args.report, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("infer", "score"))
    parser.add_argument("--candidates", type=Path, default=Path("data/tmp/laya_d_candidates.jsonl"))
    parser.add_argument("--scores", type=Path, default=Path("data/tmp/laya_typed_d_scores.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("reports/laya_typed_d_decisions.json"))
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-rows", type=int, default=0)
    args = parser.parse_args()
    {"infer": infer, "score": score}[args.mode](args)


if __name__ == "__main__":
    main()
