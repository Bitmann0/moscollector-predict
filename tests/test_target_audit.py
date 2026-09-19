import duckdb
import pandas as pd
from ml.target_audit import audit


def test_target_audit_requires_observed_previous_day_for_episode_start(tmp_path):
    database = tmp_path / "data.duckdb"
    catalog = tmp_path / "catalog.csv"
    pd.DataFrame({"ид_канала_данных": [1], "тип_датчика": ["Состояние насоса"]}).to_csv(
        catalog, index=False
    )
    with duckdb.connect(str(database)) as con:
        con.execute("""CREATE TABLE daily_channels AS SELECT * FROM (VALUES
            (1,DATE '2024-01-01'),(1,DATE '2024-01-02'),(1,DATE '2024-01-03'),
            (1,DATE '2024-01-05')) t(channel_id,local_date)""")
        con.execute("""CREATE TABLE events AS SELECT * FROM (VALUES
            (1,DATE '2024-01-02',false,'Неисправен'),
            (1,DATE '2024-01-03',false,'Неисправен'),
            (1,DATE '2024-01-05',false,'Неисправен'))
            t(channel_id,local_date,alarm,sensor_value)""")
    result = audit(database, catalog, tmp_path / "report.json")
    row = next(r for r in result["candidates"] if r["candidate"] == "pump_fault_signal")
    assert row["observed_channel_days"] == 4
    assert row["target_channel_days"] == 3
    assert row["repeated_next_day"] == 1
    assert row["starts_after_observed_non_target_day"] == 1
