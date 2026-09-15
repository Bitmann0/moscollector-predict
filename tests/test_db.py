from mkl import db


def test_connect_applies_settings():
    con = db.connect()
    limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert "GiB" in limit and float(limit.split()[0]) >= 8.0
    threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    assert int(threads) == 8
    con.close()


def test_attach_events_exposes_expected_columns():
    con = db.connect()
    db.attach_events(con)
    cols = {r[0] for r in con.execute("DESCRIBE ev").fetchall()}
    assert {"event_id", "ch", "ts", "day", "alarm", "val_raw", "val_num",
            "sys", "stype", "obj", "picket"} <= cols
    con.close()
