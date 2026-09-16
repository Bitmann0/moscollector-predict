import csv
from pathlib import Path

import duckdb
import pytest
from app.ingest import COLUMNS, ingest


def write_source(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows([COLUMNS, *rows])


def test_ingest_preserves_conflicts_quarantines_errors_and_resumes(tmp_path: Path):
    source = tmp_path / "annual.csv"
    database = tmp_path / "history.duckdb"
    row = [1, 10, "2020-01-01", "10:00:00", "f", "Норма"]
    other = [1, 10, "2020-01-01", "10:00:00", "t", "Тревога"]
    write_source(source, [row, row, other, COLUMNS, [2, 10, "bad", "10:00:00", "unknown", "bad"]])
    report = ingest(source, database)
    assert report["raw_rows"] == 5
    assert report["accepted_rows"] == 2
    assert report["exact_duplicate_rows"] == 1
    assert report["embedded_headers"] == 1
    assert report["rejected_source_rows"] == 1
    assert report["ambiguous_event_ids"] == 1
    assert ingest(source, database)["skipped"]
    with pytest.raises(ValueError, match="timezone"):
        ingest(source, database, "UTC")
    with duckdb.connect(str(database)) as con:
        assert con.execute("SELECT count(*), sum(alarm::INT) FROM events").fetchone() == (2, 1)
        assert con.execute("SELECT event_count,alarm_count FROM daily_channels").fetchone() == (
            2,
            1,
        )
        assert (
            con.execute("SELECT hour(min(event_time) AT TIME ZONE 'UTC') FROM events").fetchone()[0]
            == 7
        )
        assert con.execute("SELECT count(*) FROM ingest_sources").fetchone()[0] == 1


def test_bad_schema_rolls_back_without_losing_previous_source(tmp_path: Path):
    database = tmp_path / "history.duckdb"
    good = tmp_path / "good.csv"
    write_source(good, [[1, 10, "2020-01-01", "10:00:00", "f", "Норма"]])
    ingest(good, database)
    bad = tmp_path / "bad.csv"
    bad.write_text("wrong,column\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns"):
        ingest(bad, database)
    with duckdb.connect(str(database)) as con:
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM ingest_sources").fetchone()[0] == 1


def test_reused_ids_across_sources_are_not_dropped(tmp_path: Path):
    database = tmp_path / "history.duckdb"
    for year in [2020, 2021]:
        path = tmp_path / f"{year}.csv"
        write_source(path, [[1, 10, f"{year}-01-01", "10:00:00", "f", "Норма"]])
        ingest(path, database)
    with duckdb.connect(str(database)) as con:
        assert con.execute(
            "SELECT count(*),count(DISTINCT source_sha256) FROM events"
        ).fetchone() == (2, 2)
