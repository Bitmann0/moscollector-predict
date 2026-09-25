"""Current candidate eligibility and unknown outcomes in the daily queue."""
import runpy
import sys
from pathlib import Path

import numpy as np
import polars as pl


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SUITE = runpy.run_path(str(SCRIPTS / "exp_intrusion_precise_suite.py"))
QUEUE = runpy.run_path(str(SCRIPTS / "exp_intrusion_operational_queue.py"))


def test_candidate_decision_depends_only_on_current_guard():
    data = pl.DataFrame({"obj": ["A", "B", "C", "D", "E"],
                         "obj_armed": [1, 1, 0, None, 1],
                         "days_since_arm_event": [0, 8, 0, 0, 7],
                         "eligible": [False, True, True, True, False],
                         "exact_y": [1, 0, 1, 0, 0]})
    out = SUITE["current_candidates"](data)
    assert out["obj"].to_list() == ["A", "E"]
    changed_future = data.with_columns(pl.col("eligible").not_(),
                                        (1-pl.col("exact_y")).alias("exact_y"))
    assert SUITE["current_candidates"](changed_future)["obj"].to_list() == ["A", "E"]


def test_unknown_alert_stays_in_dispatch_denominator():
    frame = pl.DataFrame({"day": [__import__("datetime").date(2025, 1, 1)]*2,
                          "y": [0, 1], "known": [False, True],
                          "quiet": [True, True]})
    result = QUEUE["evaluate"](frame, np.array([0.9, 0.8]), quiet=False, budget=1)
    assert result["alerts"] == 1
    assert result["unknown_outcome_alerts"] == 1
    assert result["recorded_hits"] == 0
    assert result["recorded_hits_per_alert"] == 0
