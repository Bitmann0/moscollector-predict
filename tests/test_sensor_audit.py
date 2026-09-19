import duckdb
import pandas as pd
from ml.sensor_audit import audit


def test_sensor_audit_keeps_unmapped_channels_and_value_semantics(tmp_path):
    database = tmp_path / "data.duckdb"
    catalog = tmp_path / "catalog.csv"
    pd.DataFrame(
        {
            "ид_канала_данных": [1],
            "тип_инж_системы": ["Система"],
            "тип_датчика": ["Тип"],
        }
    ).to_csv(catalog, index=False)
    with duckdb.connect(str(database)) as con:
        con.execute("""CREATE TABLE daily_channels AS SELECT * FROM (VALUES
            ('x',1,DATE '2024-01-01',2,1,TIMESTAMPTZ '2024-01-01 00:00:00+00',TIMESTAMPTZ '2024-01-01 01:00:00+00'),
            ('x',2,DATE '2024-01-01',1,0,TIMESTAMPTZ '2024-01-01 00:00:00+00',TIMESTAMPTZ '2024-01-01 00:00:00+00')
        ) t(source_sha256,channel_id,local_date,event_count,alarm_count,first_event,last_event)""")
        con.execute("""CREATE TABLE events AS SELECT * FROM (VALUES
            (1,DATE '2024-01-01',TIMESTAMPTZ '2024-01-01 00:00:00+00',true,'Авария'),
            (1,DATE '2024-01-01',TIMESTAMPTZ '2024-01-01 00:00:00+00',false,'12,5'),
            (2,DATE '2024-01-01',TIMESTAMPTZ '2024-01-01 00:00:00+00',false,'Неизвестно'))
        t(channel_id,local_date,event_time,alarm,sensor_value)""")
    report = audit(database, catalog, tmp_path / "report.json")
    by_type = {row["sensor_type"]: row for row in report["sensor_types"]}
    assert by_type["Тип"]["events"] == 2
    assert by_type["Тип"]["distinct_values"] == 2
    assert by_type["Тип"]["numeric_event_fraction"] == 0.5
    assert by_type["__UNMAPPED__"]["observed_channels"] == 1
    assert {row["sensor_type"] for row in report["top_values"]} == {"Тип", "__UNMAPPED__"}
