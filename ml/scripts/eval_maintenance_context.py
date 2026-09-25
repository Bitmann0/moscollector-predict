"""Audit schedule overlap now; monitor A_link proxy metrics when new days arrive.

Inputs are frozen A_link scored rows (ch, day, risk, y) and their saved
operating threshold. Null y is an unknown outcome. No threshold is fitted here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import polars as pl

from mkl.maintenance import load_mapping, source_sha256
from mkl.maintenance_eval import evaluate_link_schedule


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scored", type=Path, required=True)
    parser.add_argument("--policy-metadata", type=Path, required=True,
                        help="Frozen model/policy metadata containing threshold")
    parser.add_argument("--scored-model-id", required=True,
                        help="Exact scorer identity; metadata may describe a challenger instead")
    parser.add_argument("--schedule", type=Path,
                        default=Path("data/interim/maintenance_2026.json"))
    parser.add_argument("--mapping", type=Path,
                        default=Path("resources/maintenance_mapping_candidates.json"))
    parser.add_argument("--channels", type=Path,
                        default=Path("data/interim/channels.parquet"))
    parser.add_argument("--labels-mature-through", type=dt.date.fromisoformat,
                        help="Last date on which future outcomes were evaluated")
    parser.add_argument("--reviews-parquet", type=Path,
                        help="Optional human decisions: ch, day, verdict")
    parser.add_argument("--output", type=Path,
                        default=Path("reports/maintenance_context_metrics.json"))
    args = parser.parse_args()
    scored = pl.read_parquet(args.scored)
    metadata = json.loads(args.policy_metadata.read_text(encoding="utf-8"))
    if metadata.get("threshold") is None:
        raise ValueError("policy metadata has no saved threshold")
    if metadata.get("test_start") and scored["day"].min() < dt.date.fromisoformat(metadata["test_start"]):
        raise ValueError("scored rows precede saved policy test window")
    if metadata.get("test_end") and scored["day"].max() > dt.date.fromisoformat(metadata["test_end"]):
        raise ValueError("scored rows exceed saved policy test window")
    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    mapping = load_mapping(args.mapping, schedule)
    result = evaluate_link_schedule(
        scored, pl.read_parquet(args.channels), schedule, mapping,
        threshold=float(metadata["threshold"]),
        labels_mature_through=args.labels_mature_through,
        reviews=pl.read_parquet(args.reviews_parquet) if args.reviews_parquet else None)
    result["policy_provenance"] = {
        "mode": "offline_replay_not_backend_issued_journal",
        "scored_model_id": args.scored_model_id,
        "scored_sha256": source_sha256(args.scored),
        "metadata_file": args.policy_metadata.name,
        "metadata_sha256": source_sha256(args.policy_metadata),
        "metadata_experiment": metadata.get("experiment"),
        "threshold": metadata["threshold"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
