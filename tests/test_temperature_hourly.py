import duckdb
import pandas as pd
from ml.temperature_hourly import prepare


def test_temperature_features_use_only_past_and_aggregate_same_second(tmp_path):
    database = tmp_path / "history.duckdb"
    catalog = tmp_path / "catalog.csv"
    pd.DataFrame({"ид_канала_данных": [1], "тип_датчика": ["Датчик температуры"]}).to_csv(
        catalog, index=False
    )
    with duckdb.connect(str(database)) as con:
        con.execute(
            "CREATE TABLE ingest_sources AS SELECT 'sha' sha256, "
            "'ext-journal-2024.csv' filename,'Europe/Moscow' time_zone"
        )
        con.execute("""CREATE TABLE events AS SELECT * FROM (VALUES
            (1,TIMESTAMPTZ '2024-01-01 18:00:00+00',false,'20'),
            (1,TIMESTAMPTZ '2024-01-01 20:00:00+00',false,'18'),
            (1,TIMESTAMPTZ '2024-01-01 20:00:00+00',false,'22'),
        (1,TIMESTAMPTZ '2024-01-02 20:00:00+00',false,'41'))
        t(channel_id,event_time,alarm,sensor_value)""")
    manifest = prepare(database, catalog, tmp_path / "prepared")
    with duckdb.connect() as con:
        features = con.execute(
            "SELECT * FROM read_parquet(?) ORDER BY as_of",
            [str(tmp_path / "prepared" / "temperature_episode_features.parquet")],
        ).df()
    assert manifest["feature_rows"] == 1
    row = features.iloc[0]
    assert row.value_min_24h == 18
    assert row.value_max_24h == 22
    assert row.future_bad_seconds == 1
    assert row.future_numeric_records == 1
