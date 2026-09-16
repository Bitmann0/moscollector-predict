from datetime import date

import duckdb
import pytest
from app.features import FEATURE_SQL, build_features


def test_features_do_not_change_when_future_events_change():
    with duckdb.connect() as con:
        con.execute(
            "CREATE TABLE daily_channels(channel_id INT,local_date DATE,event_count INT,alarm_count INT)"
        )
        con.execute(
            "INSERT INTO daily_channels VALUES (10,'2020-01-01',2,0),(10,'2020-01-03',4,1),(10,'2020-01-04',100,90)"
        )
        before = con.execute(FEATURE_SQL).fetchdf().sort_values("local_date")
        con.execute("UPDATE daily_channels SET event_count=999999 WHERE local_date='2020-01-04'")
        after = con.execute(FEATURE_SQL).fetchdf().sort_values("local_date")
        assert before.iloc[:2].equals(after.iloc[:2])
        assert before.iloc[1]["observed_days_previous_30d"] == 1
        assert before.iloc[1]["events_previous_30d"] == 2
        assert before.iloc[1]["days_since_previous"] == 2


def test_export_rejects_overlap_then_preserves_missing_days(tmp_path):
    database = tmp_path / "features.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE ingest_sources(sha256 VARCHAR,time_zone VARCHAR)")
        con.execute("INSERT INTO ingest_sources VALUES ('test','Europe/Moscow')")
        con.execute(
            "CREATE TABLE daily_channels(channel_id INT,local_date DATE,event_count INT,alarm_count INT)"
        )
        con.execute("INSERT INTO daily_channels VALUES (10,'2020-01-01',2,0),(10,'2020-01-01',2,0)")
    with pytest.raises(ValueError, match="Overlapping"):
        build_features(database, tmp_path / "features.parquet")
    with duckdb.connect(str(database)) as con:
        con.execute("DELETE FROM daily_channels")
        con.execute("INSERT INTO daily_channels VALUES (10,'2020-01-01',2,0),(10,'2020-01-03',4,1)")
    report = build_features(database, tmp_path / "features.parquet")
    assert report["rows"] == 2 and not report["target_available"]
    catalog = tmp_path / "catalog.csv"
    catalog.write_text("ид_канала_данных,тип_датчика\n10,Состояние насоса\n", encoding="utf-8")
    report = build_features(database, tmp_path / "features.parquet", catalog)
    assert report["catalog_sha256"] and report["unknown_type_rows"] == 0
    catalog.write_text("ид_канала_данных,тип_датчика\n10,A\n10,B\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Conflicting"):
        build_features(database, tmp_path / "features.parquet", catalog)
    with duckdb.connect() as con:
        result = con.execute(
            "SELECT local_date, feature_available_at AT TIME ZONE 'UTC' FROM read_parquet(?) ORDER BY local_date",
            [str(tmp_path / "features.parquet")],
        ).fetchall()
        assert result[0][0] == date(2020, 1, 1)
        assert result[0][1].hour == 21
