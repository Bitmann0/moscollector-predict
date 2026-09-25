"""Boundary cases for six-hour target and real-time alert accounting."""
import datetime as dt
import runpy
import sys
from pathlib import Path

import duckdb
import polars as pl


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
EXPERIMENT = runpy.run_path(str(SCRIPTS / "exp_intrusion_six_hour.py"))


def test_alarm_at_cutoff_is_future_label_only_and_next_window_excludes_it():
    with duckdb.connect(":memory:") as con:
        con.execute("""
          CREATE TABLE hourly AS
          SELECT 'A' AS obj, hour,
            CASE WHEN hour=TIMESTAMP '2025-01-01 06:00:00' THEN 1 ELSE 0 END AS n_events,
            0 AS n_alarms, 0 AS n_intrusion, 0 AS n_arm, 0 AS n_disarm,
            CASE WHEN hour=TIMESTAMP '2025-01-01 06:00:00' THEN 1 ELSE 0 END AS known_positive,
            0 AS unresolved
          FROM generate_series(TIMESTAMP '2025-01-01 00:00:00',
               TIMESTAMP '2025-01-02 00:00:00', INTERVAL 1 HOUR) t(hour)
        """)
        con.execute(EXPERIMENT["ROLL_SQL"])
        rows = con.execute("""
          SELECT "asof", known_positive_24h, future_known_positive
          FROM roll WHERE "asof" IN (TIMESTAMP '2025-01-01 06:00:00',
                                   TIMESTAMP '2025-01-01 12:00:00') ORDER BY "asof"
        """).fetchall()
        assert rows == [(dt.datetime(2025, 1, 1, 6), 0, 1),
                        (dt.datetime(2025, 1, 1, 12), 1, 0)]


def test_unknown_future_is_counted_in_dispatch_denominator():
    frame = pl.DataFrame({"asof": [dt.datetime(2025, 1, 1, 0)] * 2,
                          "y": [0, 1], "known": [False, True],
                          "quiet": [True, True]})
    import numpy as np
    out = EXPERIMENT["evaluate"](frame, np.array([0.9, 0.8]), "all")
    assert out["alerts"] == 1 and out["unknown_alerts"] == 1
    assert out["hits"] == 0 and out["precision_lower_bound"] == 0
