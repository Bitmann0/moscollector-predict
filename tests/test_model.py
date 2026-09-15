from pathlib import Path

import pandas as pd
import pytest
from app.model import DataValidationError, build_forecasts, load_frames


def channels() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ид_канала_данных": 10,
                "тип_инж_системы": "Температурная подсистема",
                "тип_датчика": "Датчик температуры",
                "тег_инженерной_системы": "15-1.2",
                "название_датчика": "Температура ВШ",
            },
            {
                "ид_канала_данных": 20,
                "тип_инж_системы": "Охранная подсистема",
                "тип_датчика": "КД Дверь",
                "тег_инженерной_системы": "15-1.3",
                "название_датчика": "Дверь ПК1",
            },
        ]
    )


def events() -> pd.DataFrame:
    rows = []
    for index in range(12):
        rows.append(
            {
                "ид_события": index,
                "ид_канала_данных": 10,
                "timestamp": pd.Timestamp("2026-08-01 10:00") + pd.Timedelta(index * 10, unit="s"),
                "тревожное": index < 4,
                "значение_датчика": "42",
                "numeric_value": 42.0,
            }
        )
    for index in range(3):
        rows.append(
            {
                "ид_события": 100 + index,
                "ид_канала_данных": 20,
                "timestamp": pd.Timestamp("2026-08-01 10:00") + pd.Timedelta(index, unit="h"),
                "тревожное": False,
                "значение_датчика": str(index % 2),
                "numeric_value": float(index % 2),
            }
        )
    return pd.DataFrame(rows)


def test_noisy_alarm_channel_ranked_above_quiet_channel() -> None:
    forecasts = build_forecasts(events(), channels())
    assert forecasts[0].channel_id == 10
    assert forecasts[0].risk_score > forecasts[1].risk_score
    assert forecasts[0].horizon_hours is None
    assert forecasts[0].score_kind == "heuristic_index"
    assert forecasts[0].predicted_at is None
    assert forecasts[0].factors


def test_missing_files_have_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(DataValidationError, match="Не найдены входные файлы"):
        load_frames(tmp_path)
