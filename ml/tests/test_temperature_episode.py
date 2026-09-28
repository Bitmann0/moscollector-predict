"""Почасовые температурные признаки строятся только по прошлому и по содержанию записей."""
import duckdb
import pandas as pd
import pytest

from conftest import insert_event
from mkl.temperature_episode import (
    STYPE, WINDOW_HOURS, build_features, channel_bucket, make_samples, target_audit,
)


def _temp(con, event_id, ch, ts, val, alarm=False, stype=STYPE):
    insert_event(con, event_id, ch, ts, val, alarm=alarm, stype=stype)


def _features(con, tmp_path):
    out = tmp_path / "features.parquet"
    manifest = build_features(con, out)
    frame = duckdb.connect().execute(
        "SELECT * FROM read_parquet(?) ORDER BY channel, as_of", [str(out)]).df()
    return manifest, frame


def test_features_use_only_past_and_aggregate_same_second(ev_con, tmp_path):
    # Перенос теста PR: там моменты были в UTC (18:00+00, 20:00+00), в ev — местное время.
    _temp(ev_con, 1, 1, "2024-01-01 21:00:00", "20")
    _temp(ev_con, 2, 1, "2024-01-01 23:00:00", "18")
    _temp(ev_con, 3, 1, "2024-01-01 23:00:00", "22")
    _temp(ev_con, 4, 1, "2024-01-02 23:00:00", "41")
    manifest, frame = _features(ev_con, tmp_path)
    assert manifest["feature_rows"] == 1
    row = frame.iloc[0]
    assert str(row.as_of)[:10] == "2024-01-02"
    assert row.value_min_24h == 18
    assert row.value_max_24h == 22
    # Секунда 23:00 с двумя значениями — одна секунда наблюдения, две записи.
    assert row.observed_seconds_24h == 2
    assert row.events_24h == 3
    assert row.bad_seconds_24h == 0
    assert row.future_bad_seconds == 1
    assert row.future_numeric_records == 1


def test_record_at_prediction_midnight_belongs_to_label_not_features(ev_con, tmp_path):
    # Окно признаков полуоткрытое [as_of - h, as_of): запись ровно в полночь
    # as_of уже относится к метке. С «<=» она попала бы и в признаки, и в метку.
    _temp(ev_con, 1, 1, "2024-01-01 12:00:00", "20")
    _temp(ev_con, 2, 1, "2024-01-02 00:00:00", "41")
    _, frame = _features(ev_con, tmp_path)
    row = frame.iloc[0]
    assert str(row.as_of) == "2024-01-02 00:00:00"
    assert row.events_6h == 0
    assert row.bad_seconds_24h == 0
    assert row.value_max_24h == 20
    assert row.future_bad_seconds == 1


def test_excluded_migration_period_is_dropped_before_features(ev_con, tmp_path):
    # Даты EXCLUDED_PERIODS включительные: 30 июня выпадает целиком, до 23:59.
    _temp(ev_con, 1, 1, "2021-05-10 12:00:00", "20")
    _temp(ev_con, 2, 1, "2021-06-30 23:00:00", "20")
    _temp(ev_con, 3, 1, "2021-07-01 12:00:00", "20")
    _temp(ev_con, 4, 1, "2021-07-02 12:00:00", "21")
    manifest, _ = _features(ev_con, tmp_path)
    assert manifest["raw_rows"] == 2
    assert manifest["as_of_min"].startswith("2021-07-02")


def test_decimal_comma_is_parsed(ev_con, tmp_path):
    _temp(ev_con, 1, 1, "2024-01-01 12:00:00", "20,5")
    _temp(ev_con, 2, 1, "2024-01-02 12:00:00", "21")
    manifest, frame = _features(ev_con, tmp_path)
    assert manifest["valid_numeric_rows"] == 2
    assert frame.iloc[0].value_mean_24h == pytest.approx(20.5)


def test_overflow_value_is_neither_numeric_nor_bad(ev_con, tmp_path):
    # 999 — аппаратное переполнение (config.VALUE_LIMITS), а не жара.
    _temp(ev_con, 1, 1, "2024-01-01 12:00:00", "20")
    _temp(ev_con, 2, 1, "2024-01-02 12:00:00", "999")
    manifest, frame = _features(ev_con, tmp_path)
    assert manifest["parsed_rows"] == 2
    assert manifest["valid_numeric_rows"] == 1
    row = frame.iloc[0]
    assert row.future_observed_seconds == 1
    assert row.future_numeric_records == 0
    assert row.future_bad_seconds == 0


def test_other_sensor_types_are_ignored(ev_con, tmp_path):
    _temp(ev_con, 1, 1, "2024-01-01 12:00:00", "20")
    _temp(ev_con, 2, 1, "2024-01-02 12:00:00", "21")
    _temp(ev_con, 3, 2, "2024-01-01 12:00:00", "1", stype="Газовый датчик")
    _temp(ev_con, 4, 2, "2024-01-02 12:00:00", "1", stype="Газовый датчик")
    manifest, frame = _features(ev_con, tmp_path)
    assert manifest["channels"] == 1
    assert set(frame.channel) == {1}


def test_rows_differing_only_in_event_id_are_one_record(ev_con, tmp_path):
    _temp(ev_con, 1, 1, "2024-01-01 12:00:00", "20")
    _temp(ev_con, 2, 1, "2024-01-01 12:00:00", "20")
    _temp(ev_con, 3, 1, "2024-01-02 12:00:00", "21")
    manifest, frame = _features(ev_con, tmp_path)
    assert manifest["raw_rows"] == 2
    assert frame.iloc[0].events_24h == 1


def test_manifest_hashes_only_given_sources(ev_con, tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "ext-journal-2024.csv").write_bytes(b"abc")
    (raw / "notes.csv").write_bytes(b"x")
    _temp(ev_con, 1, 1, "2024-01-01 12:00:00", "20")
    _temp(ev_con, 2, 1, "2024-01-02 12:00:00", "21")
    manifest = build_features(ev_con, tmp_path / "f.parquet", raw_dir=raw,
                              catalog_path=tmp_path / "missing.csv")
    assert [s["filename"] for s in manifest["sources"]] == ["ext-journal-2024.csv"]
    assert manifest["sources"][0]["sha256"] == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    assert manifest["catalog_sha256"] is None


def test_target_audit_counts_onsets_and_their_values(ev_con):
    # Канал 1: начало из ровно 0 °C, повтор через 10 ч (не начало), новое начало
    # через 4 суток из 45 °C. Канал 2: начало из 1,5 °C рядом с нормальным
    # значением в той же секунде.
    rows = [
        (1, "2024-01-01 10:00:00", "0", False),
        (1, "2024-01-01 20:00:00", "1", True),
        (1, "2024-01-05 20:00:00", "45", False),
        (1, "2024-01-06 10:00:00", "20", False),
        (2, "2024-01-02 10:00:00", "1,5", False),
        (2, "2024-01-02 10:00:00", "20", False),
        (2, "2024-01-02 11:00:00", "999", False),
    ]
    for i, (ch, ts, val, alarm) in enumerate(rows):
        _temp(ev_con, i, ch, ts, val, alarm=alarm)
    audit = target_audit(ev_con)
    assert audit["valid_out_of_range_records"] == 4
    assert audit["channels_with_out_of_range_records"] == 2
    assert audit["alarm_true_records"] == 1
    assert audit["bad_seconds"] == 4
    assert audit["mixed_bad_and_in_range_seconds"] == 1
    assert audit["raw_onsets_separated_by_72h"] == 3
    assert audit["onsets_exact_0_1_2_only"] == 1
    assert audit["other_low_onsets"] == 1
    assert audit["high_onsets"] == 1
    assert "eligible_72h_positive_channels" not in audit


def _frame():
    rows = []
    for day, future, past_bad, future_count in (
        ("2024-01-01", 1, 0, 10),
        ("2024-01-02", 0, 1, 10),
        ("2024-01-03", 0, 0, 0),
    ):
        row = {
            "channel": 7,
            "as_of": day,
            "future_bad_seconds": future,
            "future_numeric_records": future_count,
            "value_q25_720h": 18,
            "value_median_720h": 20,
            "value_q75_720h": 22,
        }
        for hours in WINDOW_HOURS:
            row.update({
                f"observed_seconds_{hours}h": 10,
                f"events_{hours}h": 10,
                f"numeric_records_{hours}h": 10 if hours != 168 else 70,
                f"bad_seconds_{hours}h": past_bad,
                f"value_min_{hours}h": 18,
                f"value_max_{hours}h": 22,
                f"value_mean_{hours}h": 20,
                f"value_std_{hours}h": 1,
                f"value_first_{hours}h": 19,
                f"value_last_{hours}h": 21,
                f"value_slope_per_hour_{hours}h": 0.1,
                f"distinct_values_{hours}h": 3,
                f"hours_since_value_{hours}h": 1,
            })
        rows.append(row)
    return pd.DataFrame(rows)


def test_episode_samples_exclude_existing_bad_and_unknown_future():
    samples, quality = make_samples(_frame(), clean_hours=24)
    assert len(samples) == 1
    assert samples.iloc[0].target == 1
    assert quality["excluded_unknown_or_low_future_cadence"] == 1


def test_samples_drop_prediction_days_inside_migration_period():
    frame = _frame()
    frame["as_of"] = ["2021-06-30", "2021-07-01", "2021-07-02"]
    frame["bad_seconds_24h"] = 0
    frame["future_numeric_records"] = 10
    samples, _ = make_samples(frame, clean_hours=24)
    assert [str(d)[:10] for d in samples.as_of] == ["2021-07-01", "2021-07-02"]


def test_target_audit_eligible_counts_come_from_samples(ev_con):
    _temp(ev_con, 1, 1, "2024-01-01 10:00:00", "20")
    samples = pd.DataFrame({"channel": [1, 1, 2, 3], "target": [1, 1, 1, 0]})
    audit = target_audit(ev_con, samples)
    assert audit["eligible_72h_positive_channel_days"] == 3
    assert audit["eligible_72h_positive_channels"] == 2
    assert audit["eligible_72h_top_10_channel_share"] == 1.0


def test_channel_bucket_is_deterministic():
    assert channel_bucket(123) == channel_bucket(123)
    assert 0 <= channel_bucket(123) < 5
