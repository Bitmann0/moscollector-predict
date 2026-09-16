import json

import duckdb
import pandas as pd
import pytest
from ml.from_warehouse import prepare


def test_warehouse_adapter_deduplicates_states_and_uses_local_day(tmp_path):
    database = tmp_path / "history.duckdb"
    catalog = tmp_path / "catalog.csv"
    pd.DataFrame(
        {"ид_канала_данных": [1, 2], "тип_датчика": ["Состояние насоса", "Другой"]}
    ).to_csv(catalog, index=False)
    with duckdb.connect(str(database)) as con:
        con.execute(
            "CREATE TABLE ingest_sources AS SELECT 'sha' sha256, "
            "'ext-journal-2024.csv' filename,'Europe/Moscow' time_zone,'{}' report"
        )
        con.execute("""CREATE TABLE events AS SELECT * FROM (VALUES
            ('sha',1,TIMESTAMPTZ '2024-01-01 21:00:00+00',DATE '2024-01-02',false,'Неисправен'),
            ('sha',1,TIMESTAMPTZ '2024-01-01 21:00:00+00',DATE '2024-01-02',false,'Неисправен'),
            ('sha',1,TIMESTAMPTZ '2024-01-01 21:00:00+00',DATE '2024-01-02',true,'Норма'),
            ('sha',2,TIMESTAMPTZ '2024-01-01 22:00:00+00',DATE '2024-01-02',true,'Норма')
        ) t(source_sha256,channel_id,event_time,local_date,alarm,sensor_value)""")
    output = tmp_path / "prepared"
    report = prepare(database, catalog, output, [2024])
    daily = pd.read_csv(output / "pump_daily.csv")
    assert len(daily) == 1
    assert daily.iloc[0]["day"] == "2024-01-02"
    assert daily.iloc[0]["events"] == 2
    assert daily.iloc[0]["alarms"] == 1
    assert daily.iloc[0]["faults"] == 1
    assert daily.iloc[0]["ambiguous_seconds"] == 1
    coverage = pd.read_csv(output / "coverage.csv")
    assert coverage.iloc[0]["observed_hours"] == 2
    assert report["label_audit"]["mixed_fault_seconds"] == 1
    assert report["label_audit"]["companion_states"] == [{"sensor_value": "Норма", "seconds": 1}]
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == report
    assert b"\r\n" not in (output / "pump_daily.csv").read_bytes()
    with pytest.raises(ValueError, match="Required annual sources"):
        prepare(database, catalog, output, [2025])
    with pytest.raises(ValueError, match="At least one year"):
        prepare(database, catalog, output, [])
