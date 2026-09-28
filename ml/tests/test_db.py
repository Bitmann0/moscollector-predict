import pytest

from mkl import db
from mkl.config import PATHS


def test_connect_applies_settings():
    con = db.connect()
    limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert "GiB" in limit and float(limit.split()[0]) >= 8.0
    threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    assert int(threads) == 8
    con.close()


@pytest.mark.skipif(not any(PATHS.interim.glob("events_year=*.parquet")),
                    reason="нужен журнал событий из данных заказчика")
def test_attach_events_exposes_expected_columns():
    con = db.connect()
    db.attach_events(con)
    cols = {r[0] for r in con.execute("DESCRIBE ev").fetchall()}
    assert {"event_id", "ch", "ts", "day", "alarm", "val_raw", "val_num",
            "sys", "stype", "obj", "picket"} <= cols
    con.close()
