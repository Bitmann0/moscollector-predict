"""End-to-end cases for event-time target construction and censored days."""
import datetime as dt

import duckdb

from mkl.intrusion_target import build


def test_event_time_labels_do_not_use_end_of_day_guard_or_missing_days_as_negative():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE ev (obj VARCHAR, ts TIMESTAMP, val_raw VARCHAR, alarm BOOLEAN)")
        con.executemany("INSERT INTO ev VALUES (?, ?, ?, ?)", [
            ("A", "2025-01-01 07:00:00", "Снято с охраны", False),
            ("A", "2025-01-02 12:00:00", "Обнаружено движение", True),
            ("A", "2025-01-02 17:00:00", "На охране", False),
            ("A", "2025-01-03 10:00:00", "Обнаружено движение", True),
            ("B", "2025-01-02 12:00:00", "На охране", False),
            ("B", "2025-01-02 12:00:00", "Не замкнут", True),
            ("C", "2025-01-01 17:00:00", "На охране", False),
            ("C", "2025-01-03 10:00:00", "Не замкнут", True),
        ])
        con.execute("CREATE TABLE object_days (obj VARCHAR, day DATE)")
        con.executemany("INSERT INTO object_days VALUES (?, ?)", [
            ("A", "2025-01-01"), ("A", "2025-01-02"), ("A", "2025-01-03"),
            ("B", "2025-01-01"), ("B", "2025-01-02"),
            ("C", "2025-01-02"), ("D", "2025-01-02"),
        ])
        build(con, dt.date(2025, 1, 1), dt.date(2025, 1, 2))
        rows = con.execute("""
          SELECT obj, day, y, known, next_day_object_observed,
                 unresolved_alarm_tomorrow
          FROM label_intrusion_eventtime ORDER BY obj, day
        """).fetchall()
        assert rows == [
            ("A", dt.date(2025, 1, 1), 0, True, True, False),
            ("A", dt.date(2025, 1, 2), 1, True, True, False),
            ("B", dt.date(2025, 1, 1), 0, False, True, True),
            ("B", dt.date(2025, 1, 2), 0, False, False, False),
            ("C", dt.date(2025, 1, 2), 1, True, False, False),
            ("D", dt.date(2025, 1, 2), 0, False, False, False),
        ]


def test_target_can_extend_when_next_observed_day_is_appended():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE ev (obj VARCHAR, ts TIMESTAMP, val_raw VARCHAR, alarm BOOLEAN)")
        con.execute("CREATE TABLE object_days AS SELECT 'A' AS obj, DATE '2025-01-01' AS day")
        build(con, dt.date(2025, 1, 1), dt.date(2025, 1, 1))
        assert con.execute("SELECT known FROM label_intrusion_eventtime").fetchone() == (False,)
        con.execute("INSERT INTO object_days VALUES ('A', DATE '2025-01-02')")
        build(con, dt.date(2025, 1, 1), dt.date(2025, 1, 1))
        assert con.execute("SELECT known FROM label_intrusion_eventtime").fetchone() == (True,)
