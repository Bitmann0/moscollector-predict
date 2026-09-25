"""Temporal audit of narrow guarded-alarm decisions, including deferral.

The negative outcome is deliberately conservative: all seven future object
days must exist and none may carry an unresolved alarm. This is still only
evidence about *recorded* alarms, never proof of physical safety.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
from collections import defaultdict

import polars as pl

from mkl import guard_queue, serve, store, train
from mkl.config import PATHS


START = dt.date(2023, 2, 1)
END = dt.date(2026, 6, 22)
TEST_START = dt.date(2025, 1, 6)


def aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    hits = sum(row["hit"] for row in rows)
    quiet = sum(row["known_quiet"] for row in rows)
    unknown = n - hits - quiet
    return {"selected": n, "recorded_hits": hits, "known_quiet": quiet,
            "unknown": unknown,
            "unique_objects": len({row["obj"] for row in rows}),
            "hit_fraction": hits / n if n else None,
            "conservative_quiet_fraction": quiet / n if n else None,
            "known_only_quiet_fraction": quiet / (quiet + hits) if quiet + hits else None}


def main() -> None:
    base = store.read_slice("object", START, END, columns=[
        "obj", "day", "obj_armed", "days_since_arm_event"])
    event = pl.read_parquet(guard_queue.EVENT_DAYS)
    frame = guard_queue.add_alarm_history(
        guard_queue.current_candidates(base).filter(pl.col("day").dt.weekday() == 1),
        event).sort(["day", "obj"])
    positives: dict[str, list[dt.date]] = defaultdict(list)
    unresolved = set()
    for obj, day, pos, unk in event.select("obj", "day", "positive", "unresolved").iter_rows():
        obj = str(obj)
        if pos:
            positives[obj].append(day)
        if unk:
            unresolved.add((obj, day))
    for dates in positives.values():
        dates.sort()
    observed = {(str(obj), day) for obj, day in pl.read_parquet(
        PATHS.features / "object.parquet", columns=["obj", "day"]).iter_rows()}
    rows = []
    for obj, day, n7, n30, age in frame.select(
            "obj", "day", "exact_count_7", "exact_count_30", "exact_age").iter_rows():
        obj = str(obj)
        dates = positives[obj]
        hit = bisect.bisect_right(dates, day + dt.timedelta(days=8)) > bisect.bisect_left(
            dates, day + dt.timedelta(days=2))
        complete = all((obj, day + dt.timedelta(days=i)) in observed and
                       (obj, day + dt.timedelta(days=i)) not in unresolved
                       for i in range(2, 9))
        rows.append({"obj": obj, "day": day, "n7": n7, "n30": n30,
                     "age": age, "hit": hit, "known_quiet": not hit and complete})
    periods = {"fit_2023_2024": [r for r in rows if r["day"] < TEST_START],
               "test_2025_2026": [r for r in rows if r["day"] >= TEST_START]}
    result = {"target": "recorded guarded SMVU alarm D+2..D+8",
              "observation": "quiet requires seven future object rows and no unresolved event days; object presence does not prove complete guard telemetry",
              "policies": {}}
    for name, group in periods.items():
        total = aggregate(group)
        policies = {}
        for policy, select in {
            "no_alarm_30d": lambda r: r["n30"] == 0,
            "no_alarm_7d": lambda r: r["n7"] == 0,
            "at_most_one_alarm_7d": lambda r: r["n7"] <= 1,
            "repeat_3d": lambda r: r["n7"] >= 3,
            "repeat_4d": lambda r: r["n7"] >= 4,
            "middle_history": lambda r: r["n30"] > 0 and r["n7"] < 4,
        }.items():
            selected = aggregate([r for r in group if select(r)])
            selected["candidate_coverage"] = selected["selected"] / total["selected"]
            selected["positive_recall"] = (selected["recorded_hits"] / total["recorded_hits"]
                                           if total["recorded_hits"] else None)
            policies[policy] = selected
        result["policies"][name] = {"all_candidates": total, "strata": policies}
    # A genuinely separate specialist for objects without a recorded guarded
    # alarm in 30 days. Fit only on decided 2023-24 outcomes; reserve 2025-26.
    cfg = serve.load_heads()["C"]
    drop = cfg.get("drop_feature_prefixes") or []
    full = store.read_slice("object", START, END)
    full = full.select([c for c in full.columns
                        if not any(c.startswith(prefix) for prefix in drop)])
    all_full = guard_queue.add_alarm_history(guard_queue.current_candidates(full), event)
    all_full = all_full.filter(pl.col("day").dt.weekday() == 1).sort(["day", "obj"])
    full = all_full.filter(pl.col("exact_count_30") == 0)
    by_key = {(r["obj"], r["day"]): r for r in rows}
    outcomes = [by_key[(str(obj), day)] for obj, day in full.select("obj", "day").iter_rows()]
    full = full.with_columns(pl.Series("y", [int(r["hit"]) for r in outcomes]),
                             pl.Series("quiet", [int(r["known_quiet"]) for r in outcomes]),
                             pl.Series("decided", [r["hit"] or r["known_quiet"]
                                                   for r in outcomes]))
    features = train.feature_columns(full.drop("decided", "quiet"))
    fit = full.filter((pl.col("day") < TEST_START) & pl.col("decided"))
    test = full.filter(pl.col("day") >= TEST_START)
    yf = fit["y"].to_numpy()
    model = train._build_model("lgbm", {"n_estimators": 100, "num_leaves": 7,
                                         "min_child_samples": 40, "n_jobs": 4},
                               (len(yf)-yf.sum())/yf.sum())
    model.fit(train._matrix(fit, features), yf)
    quiet_fit = full.filter(pl.col("day") < TEST_START)
    yq = quiet_fit["quiet"].to_numpy()
    quiet_model = train._build_model("lgbm", {"n_estimators": 100, "num_leaves": 7,
                                               "min_child_samples": 40, "n_jobs": 4},
                                     (len(yq)-yq.sum())/yq.sum())
    quiet_model.fit(train._matrix(quiet_fit, features), yq)
    test = test.with_columns(pl.Series(
        "ml_score", model.predict_proba(train._matrix(test, features))[:, 1]),
        pl.Series("quiet_score", quiet_model.predict_proba(train._matrix(test, features))[:, 1]))
    model_results = {"fit_decided": fit.height, "fit_positive": int(yf.sum()),
                     "test_all": test.height, "test_decided": int(test["decided"].sum()),
                     "features": features, "top_per_monday": {}}
    for budget in (1, 4):
        for method, score in (("rule_recent_prior", "exact_age"),
                              ("risk_model", "ml_score"),
                              ("quiet_model", "quiet_score")):
            # The rule favors recent alarms before the 30-day quiet gap.
            chosen = pl.concat([
                day.sort([score, "obj"], descending=[method != "rule_recent_prior", False]).head(budget)
                for day in test.partition_by("day", maintain_order=True)])
            selected = [by_key[(str(obj), day)]
                        for obj, day in chosen.select("obj", "day").iter_rows()]
            stats = aggregate(selected)
            stats["candidate_coverage"] = stats["selected"] / test.height
            stats["periods"] = {
                "2025": aggregate([r for r in selected if r["day"].year == 2025]),
                "2026_H1": aggregate([r for r in selected if r["day"].year == 2026]),
            }
            model_results["top_per_monday"][f"{method}_{budget}"] = stats
    for method, score in (("rule_recent_prior", "exact_age"),
                          ("quiet_model", "quiet_score")):
        last_selected = {}
        selected = []
        for day in test.partition_by("day", maintain_order=True):
            chosen = 0
            for row in day.sort([score, "obj"],
                                descending=[method != "rule_recent_prior", False]).to_dicts():
                obj = str(row["obj"])
                date = row["day"]
                if obj in last_selected and (date - last_selected[obj]).days <= 28:
                    continue
                selected.append(by_key[(obj, date)])
                last_selected[obj] = date
                chosen += 1
                if chosen == 4:
                    break
        stats = aggregate(selected)
        stats["candidate_coverage"] = stats["selected"] / test.height
        stats["periods"] = {
            "2025": aggregate([r for r in selected if r["day"].year == 2025]),
            "2026_H1": aggregate([r for r in selected if r["day"].year == 2026]),
        }
        model_results["top_per_monday"][f"{method}_4_cd28"] = stats
    result["quiet_30d_specialist"] = model_results
    middle = all_full.filter((pl.col("exact_count_30") > 0) &
                             (pl.col("exact_count_7") < 4))
    middle = middle.with_columns(pl.Series(
        "y", [int(by_key[(str(obj), day)]["hit"])
              for obj, day in middle.select("obj", "day").iter_rows()]))
    middle_fit = middle.filter(pl.col("day") < TEST_START)
    middle_test = middle.filter(pl.col("day") >= TEST_START)
    ym = middle_fit["y"].to_numpy()
    middle_features = train.feature_columns(middle)
    middle_model = train._build_model("lgbm", {"n_estimators": 100,
                                                "num_leaves": 7,
                                                "min_child_samples": 40,
                                                "n_jobs": 4},
                                      (len(ym)-ym.sum())/ym.sum())
    middle_model.fit(train._matrix(middle_fit, middle_features), ym)
    middle_test = guard_queue.priority_score(middle_test).with_columns(pl.Series(
        "ml_score", middle_model.predict_proba(
            train._matrix(middle_test, middle_features))[:, 1]))
    middle_result = {"fit_rows": middle_fit.height, "fit_hits": int(ym.sum()),
                     "test_rows": middle_test.height, "test_hits": int(middle_test["y"].sum()),
                     "top_per_monday": {}}
    for method, score in (("rule", "priority_score"), ("model", "ml_score")):
        chosen = pl.concat([
            day.sort([score, "obj"], descending=[True, False]).head(4)
            for day in middle_test.partition_by("day", maintain_order=True)])
        selected = [by_key[(str(obj), day)]
                    for obj, day in chosen.select("obj", "day").iter_rows()]
        middle_result["top_per_monday"][method] = aggregate(selected)
        last_selected = {}
        selected = []
        for day in middle_test.partition_by("day", maintain_order=True):
            chosen = 0
            for row in day.sort([score, "obj"], descending=[True, False]).to_dicts():
                obj = str(row["obj"])
                date = row["day"]
                if obj in last_selected and (date - last_selected[obj]).days <= 28:
                    continue
                selected.append(by_key[(obj, date)])
                last_selected[obj] = date
                chosen += 1
                if chosen == 4:
                    break
        middle_result["top_per_monday"][f"{method}_cd28"] = aggregate(selected)
    result["middle_history_specialist"] = middle_result
    path = PATHS.reports / "guard_specialist_frontier.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
