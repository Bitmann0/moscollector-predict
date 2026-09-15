import pandas as pd
import pytest

pytest.importorskip("duckdb")
pytest.importorskip("py7zr")

from ml.prepare import prepare


def test_daily_aggregation_keeps_simultaneous_states(tmp_path):
    catalog = tmp_path / "channels.csv"
    pd.DataFrame({"ид_канала_данных": [10], "тип_датчика": ["Состояние насоса"]}).to_csv(
        catalog, index=False)
    events = tmp_path / "events.csv"
    # One duplicate plus simultaneous fault/normal; no inferred latest state.
    pd.DataFrame({"ид_канала_данных": [10, 10, 10], "дата": ["2024-01-01"] * 3,
                  "время": ["10:00:00"] * 3, "тревожное": ["t", "t", "f"],
                  "значение_датчика": ["Неисправен", "Неисправен", "Норма"]}).to_csv(
        events, index=False)
    prepare([events], catalog, tmp_path / "out")
    daily = pd.read_csv(tmp_path / "out/pump_daily.csv").iloc[0]
    assert daily.events == 2
    assert daily.faults == 1
    assert daily.alarms == 1
    assert daily.ambiguous_seconds == 1
