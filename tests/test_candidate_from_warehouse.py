import duckdb
import pandas as pd
from ml.candidate_from_warehouse import prepare


def test_candidate_adapter_builds_temperature_target(tmp_path):
    database = tmp_path / "history.duckdb"
    catalog = tmp_path / "catalog.csv"
    pd.DataFrame({"ид_канала_данных": [1], "тип_датчика": ["Датчик температуры"]}).to_csv(
        catalog, index=False
    )
    with duckdb.connect(str(database)) as con:
        con.execute(
            "CREATE TABLE ingest_sources AS SELECT 'sha' sha256, "
            "'ext-journal-2024.csv' filename,'Europe/Moscow' time_zone,'{}' report"
        )
        con.execute("""CREATE TABLE events AS SELECT * FROM (VALUES
            ('sha',1,TIMESTAMPTZ '2024-01-01 21:00:00+00',DATE '2024-01-02',false,'2.5'),
            ('sha',1,TIMESTAMPTZ '2024-01-01 22:00:00+00',DATE '2024-01-02',false,'20'))
        t(source_sha256,channel_id,event_time,local_date,alarm,sensor_value)""")
    output = tmp_path / "prepared"
    manifest = prepare(database, catalog, output, "temperature_outside_3_40", [2024])
    daily = pd.read_csv(output / "pump_daily.csv")
    assert manifest["channels"] == 1
    assert daily.iloc[0].faults == 1
    assert daily.iloc[0].events == 2
