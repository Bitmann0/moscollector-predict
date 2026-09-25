"""Regression cases for experimental C labels; production labels stay unchanged."""
import runpy
from pathlib import Path

import duckdb


AUDIT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/exp_intrusion_label_timing.py"))


def test_guard_is_past_only_object_specific_and_conflicts_are_unknown():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE controls (obj VARCHAR, ts TIMESTAMP, val_raw VARCHAR)")
        con.executemany("INSERT INTO controls VALUES (?, ?, ?)", [
            ("A", "2024-12-31 20:00:00", "На охране"),
            ("A", "2025-01-01 07:00:00", "Снято с охраны"),
            ("A", "2025-01-01 17:00:00", "На охране"),
            ("A", "2025-01-01 18:00:00", "На охране"),
            ("A", "2025-01-01 18:00:00", "Снято с охраны"),
            ("A", "2025-01-01 20:00:00", "На охране"),
            ("A", "2025-01-01 20:00:00", "На охране"),
        ])
        con.execute("CREATE TABLE intrusion (id INTEGER, obj VARCHAR, ts TIMESTAMP)")
        con.executemany("INSERT INTO intrusion VALUES (?, ?, ?)", [
            (1, "A", "2024-12-31 19:00:00"),  # no future backfill
            (2, "A", "2025-01-01 06:00:00"),  # carry across midnight/year
            (3, "A", "2025-01-01 12:00:00"),  # evening arming is irrelevant
            (4, "A", "2025-01-01 17:00:00"),  # same second, order unknown
            (5, "A", "2025-01-01 17:00:01"),
            (6, "A", "2025-01-01 19:00:00"),  # contradictory controls
            (7, "A", "2025-01-01 20:00:01"),  # identical duplicates OK
            (8, "B", "2025-01-01 21:00:00"),  # no cross-object borrowing
        ])
        AUDIT["build_guard_timeline"](con, "controls")
        AUDIT["attach_guard_state"](con)
        assert con.execute("SELECT id, armed_at_event FROM at_time ORDER BY id").fetchall() == [
            (1, None), (2, 1), (3, 0), (4, None),
            (5, 1), (6, None), (7, 1), (8, None),
        ]
