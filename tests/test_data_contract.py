from pathlib import Path

import pandas as pd
import pytest
from app.model import CHANNELS_FILE, EVENTS_FILE, DataValidationError, analyze, load_frames


@pytest.mark.parametrize(
    "raw,expected",
    [("f", False), ("t", True), ("false", False), ("TRUE", True), ("0", False), ("1", True)],
)
def test_alarm_encodings(sample_data_dir: Path, raw: str, expected: bool) -> None:
    path = sample_data_dir / EVENTS_FILE
    frame = pd.read_csv(path, dtype=str)
    frame["тревожное"] = raw
    frame.to_csv(path, index=False)
    parsed, _ = load_frames(sample_data_dir)
    assert parsed["тревожное"].eq(expected).all()


@pytest.mark.parametrize("raw", ["", "unknown", "2"])
def test_invalid_alarm_rejected(sample_data_dir: Path, raw: str) -> None:
    path = sample_data_dir / EVENTS_FILE
    frame = pd.read_csv(path, dtype=str)
    frame.loc[0, "тревожное"] = raw
    frame.to_csv(path, index=False)
    with pytest.raises(DataValidationError, match="Неизвестное значение"):
        load_frames(sample_data_dir)


def test_only_exact_duplicates_removed_and_headers_reported(sample_data_dir: Path) -> None:
    path = sample_data_dir / EVENTS_FILE
    frame = pd.read_csv(path, dtype=str)
    conflicting = frame.iloc[[0]].copy()
    conflicting["значение_датчика"] = "Неисправен"
    header = pd.DataFrame([{c: c for c in frame.columns}])
    pd.concat([frame, frame.iloc[[0]], conflicting, header]).to_csv(path, index=False)
    result, _ = load_frames(sample_data_dir)
    assert len(result) == len(frame) + 1
    assert len(result[result["ид_события"] == 1]) == 2
    assert result.attrs["quality"]["exact_duplicate_rows"] == 1
    assert result.attrs["quality"]["repeated_header_rows"] == 1


def test_conflicting_catalog_rejected(sample_data_dir: Path) -> None:
    path = sample_data_dir / CHANNELS_FILE
    frame = pd.read_csv(path, dtype=str)
    conflicting = frame.iloc[[0]].copy()
    conflicting["тип_датчика"] = "Газовый датчик"
    pd.concat([frame, conflicting]).to_csv(path, index=False)
    with pytest.raises(DataValidationError, match="Конфликт описаний"):
        load_frames(sample_data_dir)


def test_empty_input_rejected(sample_data_dir: Path) -> None:
    path = sample_data_dir / EVENTS_FILE
    pd.read_csv(path).iloc[:0].to_csv(path, index=False)
    with pytest.raises(DataValidationError, match="пусты"):
        load_frames(sample_data_dir)


def test_timezone_window_and_no_fake_future_prediction(sample_data_dir: Path) -> None:
    path = sample_data_dir / EVENTS_FILE
    frame = pd.read_csv(path, dtype=str)
    old = frame.iloc[[0]].copy()
    old["дата"] = "2025-08-01"
    pd.concat([frame, old]).to_csv(path, index=False)
    assessments, metadata = analyze(sample_data_dir, horizon_hours=168)
    assert metadata["event_count"] == len(frame)
    assert metadata["data_to"] == "2026-08-01T09:00:00+00:00"
    assert metadata["prediction_available"] is False
    assert metadata["target_prediction_horizon_hours"] == 168
    assert all(x.predicted_at is None and x.valid_until is None for x in assessments)
    assert all(x.data_as_of == metadata["data_to"] for x in assessments)


def test_same_second_states_not_counted_as_fast_polling(sample_data_dir: Path) -> None:
    path = sample_data_dir / EVENTS_FILE
    frame = pd.read_csv(path, dtype=str)
    frame["время"] = "10:00:00"
    frame.to_csv(path, index=False)
    assessments, _ = analyze(sample_data_dir)
    assert all(x.features["short_interval_rate"] == 0 for x in assessments)


@pytest.mark.parametrize("value", ["1.5", "0", "-1", "9223372036854775808"])
def test_invalid_id_rejected(sample_data_dir: Path, value: str) -> None:
    path = sample_data_dir / EVENTS_FILE
    frame = pd.read_csv(path, dtype=str)
    frame.loc[0, "ид_события"] = value
    frame.to_csv(path, index=False)
    with pytest.raises(DataValidationError):
        load_frames(sample_data_dir)
