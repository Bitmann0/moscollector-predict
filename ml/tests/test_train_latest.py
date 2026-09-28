import datetime as dt

from mkl import cv, pipeline


def test_live_windows_move_with_new_data_and_keep_embargo():
    first = cv.live_windows(dt.date(2026, 6, 29), embargo_days=31)
    later = cv.live_windows(dt.date(2027, 6, 29), embargo_days=31)
    assert first["threshold_end"] == dt.date(2026, 6, 29)
    assert later["threshold_end"] == dt.date(2027, 6, 29)
    assert (first["calibration_start"] - first["training_end"]).days == 32
    assert (first["threshold_start"] - first["calibration_end"]).days == 1
    assert (first["threshold_end"] - first["threshold_start"]).days == 29


def test_regular_pipeline_trains_latest_and_benchmark_is_separate():
    stage = pipeline.by_name()["train"]
    assert stage.command[-1].endswith("train_latest.py")
    assert len(stage.outputs) >= 1
    assert all(path.suffix == ".pkl" for path in stage.outputs)
    assert {path.stem for path in stage.outputs} == {"D", "A_link", "B", "E"}
