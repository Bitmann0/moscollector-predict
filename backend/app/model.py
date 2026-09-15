from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

EVENTS_FILE = "журнал_событий_пример.csv"
CHANNELS_FILE = "справочник_каналов_датчиков.csv"

ANALOG_SENSOR_TYPES = {"Датчик температуры", "Газовый датчик"}


class DataValidationError(ValueError):
    """Входная выгрузка не соответствует ожидаемому контракту."""


@dataclass(frozen=True)
class Forecast:
    channel_id: int
    sensor_name: str
    sensor_type: str
    system_type: str
    location: str
    risk_score: float
    risk_level: str
    horizon_hours: None
    predicted_at: None
    valid_until: None
    assessed_at: str
    data_as_of: str
    last_observed_at: str
    score_kind: str
    recommendation: str
    factors: list[str]
    features: dict[str, float | int]
    map_x: float
    map_y: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate_columns(frame: pd.DataFrame, required: set[str], filename: str) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise DataValidationError(f"{filename}: отсутствуют столбцы {sorted(missing)}")


def load_frames(
    data_dir: Path, time_zone: str = "Europe/Moscow"
) -> tuple[pd.DataFrame, pd.DataFrame]:
    events_path = data_dir / EVENTS_FILE
    channels_path = data_dir / CHANNELS_FILE
    missing = [str(path) for path in (events_path, channels_path) if not path.exists()]
    if missing:
        raise DataValidationError("Не найдены входные файлы: " + ", ".join(missing))

    try:
        events = pd.read_csv(events_path, dtype=str, keep_default_na=False)
        channels = pd.read_csv(channels_path, dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError, OSError) as error:
        raise DataValidationError(f"Не удалось прочитать CSV: {error}") from error
    _validate_columns(
        events,
        {"ид_события", "ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"},
        EVENTS_FILE,
    )
    _validate_columns(
        channels,
        {
            "ид_канала_данных",
            "тип_инж_системы",
            "тип_датчика",
            "тег_инженерной_системы",
            "название_датчика",
        },
        CHANNELS_FILE,
    )
    raw_count = len(events)
    repeated_header = events.eq(pd.Series({c: c for c in events.columns})).all(axis=1)
    events = events.loc[~repeated_header].copy()
    # Only identical source rows are duplicates; event IDs are not globally unique.
    before_dedup = len(events)
    events = events.drop_duplicates().copy()
    channels = channels.drop_duplicates().copy()
    if events.empty or channels.empty:
        raise DataValidationError("Выгрузка событий или справочник пусты")
    for frame, fields in (
        (events, ("ид_события", "ид_канала_данных")),
        (channels, ("ид_канала_данных",)),
    ):
        for field in fields:
            if not frame[field].str.fullmatch(r"[0-9]+").all():
                raise DataValidationError(f"Некорректный целочисленный идентификатор: {field}")
            values = frame[field].map(int)
            if ((values <= 0) | (values > np.iinfo(np.int64).max)).any():
                raise DataValidationError(f"Идентификатор вне диапазона BIGINT: {field}")
            frame[field] = values.astype("int64")
    if channels["ид_канала_данных"].duplicated().any():
        raise DataValidationError("Конфликт описаний одного ID в справочнике каналов")
    events["timestamp"] = pd.to_datetime(
        events["дата"].astype(str) + " " + events["время"].astype(str), errors="coerce"
    )
    if events["timestamp"].isna().any():
        raise DataValidationError(f"{EVENTS_FILE}: обнаружены некорректные дата или время")
    try:
        events["timestamp"] = events["timestamp"].dt.tz_localize(time_zone).dt.tz_convert("UTC")
    except (ValueError, KeyError) as error:
        raise DataValidationError(f"Не удалось определить часовой пояс данных: {error}") from error
    alarm = events["тревожное"].str.strip().str.lower()
    allowed = {"t": True, "true": True, "1": True, "f": False, "false": False, "0": False}
    if not alarm.isin(allowed).all():
        raise DataValidationError(
            "Неизвестное значение 'тревожное'; допустимы t/f, true/false, 1/0"
        )
    events["тревожное"] = alarm.map(allowed).astype(bool)
    events["numeric_value"] = pd.to_numeric(
        events["значение_датчика"].astype(str).str.replace(",", ".", regex=False),
        errors="coerce",
    )
    events.attrs["quality"] = {
        "raw_event_count": raw_count,
        "repeated_header_rows": int(repeated_header.sum()),
        "exact_duplicate_rows": before_dedup - len(events),
        "unknown_channel_rows": int(
            (~events["ид_канала_данных"].isin(channels["ид_канала_данных"])).sum()
        ),
    }
    return events, channels


def _risk_level(score: float) -> str:
    if score >= 0.75:
        return "critical"
    if score >= 0.55:
        return "high"
    if score >= 0.35:
        return "medium"
    return "low"


def _recommendation(level: str) -> str:
    return {
        "critical": "Проверить события канала и режим опроса; необходимость ремонта определяет специалист.",
        "high": "Сопоставить события с режимом оборудования и плановыми работами.",
        "medium": "Проверить соединения при ближайшем обходе и наблюдать динамику.",
        "low": "В данном окне мало оснований для приоритетной проверки; исправность не подтверждена.",
    }[level]


def _location_from_tag(tag: str) -> str:
    clean = str(tag).strip().strip(".")
    return f"Тег {clean}" if clean else "Привязка к объекту не определена"


def build_forecasts(
    events: pd.DataFrame,
    channels: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> list[Forecast]:
    """Строит объяснимое ранжирование риска деградации для каждого активного канала."""
    if events.empty:
        return []
    ordered = events.sort_values(["ид_канала_данных", "timestamp"], kind="stable").copy()
    # Several states in one second are not evidence of rapid consecutive polling.
    timeline = ordered.drop_duplicates(["ид_канала_данных", "timestamp"]).copy()
    timeline["interval_seconds"] = (
        timeline.groupby("ид_канала_данных")["timestamp"].diff().dt.total_seconds()
    )
    timeline["short_interval"] = timeline["interval_seconds"].between(0, 60, inclusive="right")
    interval_rates = timeline.groupby("ид_канала_данных")["short_interval"].mean()

    grouped = ordered.groupby("ид_канала_данных", sort=False)
    features = grouped.agg(
        event_count=("ид_события", "size"),
        alarm_count=("тревожное", "sum"),
        alarm_rate=("тревожное", "mean"),
        last_observed_at=("timestamp", "max"),
        unique_values=("значение_датчика", "nunique"),
        numeric_coverage=("numeric_value", lambda values: float(values.notna().mean())),
    ).reset_index()
    features["short_interval_rate"] = features["ид_канала_данных"].map(interval_rates)
    features["dominant_value_rate"] = (
        grouped["значение_датчика"]
        .apply(lambda values: float(values.value_counts(normalize=True).iloc[0]))
        .to_numpy()
    )

    positive_counts = features["event_count"].clip(lower=1)
    count_scale = max(float(positive_counts.quantile(0.95)), 2.0)
    features["event_pressure"] = 1.0 - np.exp(-features["event_count"] / count_scale)
    features = features.merge(channels, on="ид_канала_данных", how="left")
    features["тип_датчика"] = features["тип_датчика"].fillna("Неизвестный датчик")
    features["название_датчика"] = features["название_датчика"].fillna(
        "Канал " + features["ид_канала_данных"].astype(str)
    )
    features["тип_инж_системы"] = features["тип_инж_системы"].fillna("Не определена")
    features["тег_инженерной_системы"] = features["тег_инженерной_системы"].fillna("")

    analog = features["тип_датчика"].isin(ANALOG_SENSOR_TYPES).astype(float)
    # Коэффициенты baseline зафиксированы и интерпретируемы. После разметки этот блок
    # заменяется калиброванной supervised-моделью с тем же выходным контрактом.
    raw_score = (
        -3.0
        + 2.4 * features["short_interval_rate"]
        + 2.0 * np.minimum(features["alarm_count"] / 3.0, 1.0)
        + 1.8 * features["event_pressure"]
        + 1.1 * analog * features["dominant_value_rate"]
    )
    features["risk_score"] = 1.0 / (1.0 + np.exp(-raw_score))

    assessed_at = (now or datetime.now(UTC)).replace(microsecond=0)
    data_as_of = ordered["timestamp"].max().isoformat()
    forecasts: list[Forecast] = []
    for row in features.itertuples(index=False):
        score = round(float(row.risk_score), 4)
        level = _risk_level(score)
        factors: list[str] = []
        if row.short_interval_rate >= 0.15:
            factors.append(f"{row.short_interval_rate:.0%} событий повторились быстрее минуты")
        if row.alarm_count:
            factors.append(f"Зафиксировано тревог: {int(row.alarm_count)}")
        if row.event_pressure >= 0.65:
            factors.append(f"Высокая активность канала: {int(row.event_count)} событий")
        if row.тип_датчика in ANALOG_SENSOR_TYPES and row.dominant_value_rate >= 0.7:
            factors.append(f"Одно значение занимает {row.dominant_value_rate:.0%} наблюдений")
        if not factors:
            factors.append("Аномальные паттерны не выявлены")
        channel_id = int(row.ид_канала_данных)
        forecasts.append(
            Forecast(
                channel_id=channel_id,
                sensor_name=str(row.название_датчика),
                sensor_type=str(row.тип_датчика),
                system_type=str(row.тип_инж_системы),
                location=_location_from_tag(row.тег_инженерной_системы),
                risk_score=score,
                risk_level=level,
                horizon_hours=None,
                predicted_at=None,
                valid_until=None,
                assessed_at=assessed_at.isoformat(),
                data_as_of=data_as_of,
                last_observed_at=row.last_observed_at.isoformat(),
                score_kind="heuristic_index",
                recommendation=_recommendation(level),
                factors=factors,
                features={
                    "event_count": int(row.event_count),
                    "alarm_count": int(row.alarm_count),
                    "alarm_rate": round(float(row.alarm_rate), 4),
                    "short_interval_rate": round(float(row.short_interval_rate), 4),
                    "dominant_value_rate": round(float(row.dominant_value_rate), 4),
                    "unique_values": int(row.unique_values),
                },
                map_x=round(6 + ((channel_id * 37) % 8800) / 100, 2),
                map_y=round(8 + ((channel_id * 61) % 7600) / 100, 2),
            )
        )
    return sorted(forecasts, key=lambda item: item.risk_score, reverse=True)


def analyze(
    data_dir: Path,
    horizon_hours: int = 24,
    *,
    time_zone: str = "Europe/Moscow",
    window_hours: int = 24,
    now: datetime | None = None,
) -> tuple[list[Forecast], dict[str, Any]]:
    events, channels = load_frames(data_dir, time_zone)
    quality = events.attrs["quality"]
    if window_hours <= 0:
        raise DataValidationError("Окно анализа должно быть положительным")
    data_to = events["timestamp"].max()
    window_start = data_to - pd.Timedelta(hours=window_hours)
    events = events.loc[events["timestamp"] > window_start].copy()
    forecasts = build_forecasts(events, channels, now=now)
    metadata = {
        "event_count": len(events),
        "channel_count": int(events["ид_канала_данных"].nunique()),
        "catalog_channel_count": len(channels),
        "data_from": events["timestamp"].min().isoformat(),
        "data_to": events["timestamp"].max().isoformat(),
        "model_name": "sensor-failure-baseline",
        "model_version": "0.2.0",
        "model_status": "baseline_unvalidated",
        "score_kind": "heuristic_index",
        "prediction_available": False,
        "target_prediction_horizon_hours": horizon_hours,
        "analysis_window_hours": window_hours,
        "window_start": window_start.isoformat(),
        "source_time_zone": time_zone,
        "quality": quality,
    }
    return forecasts, metadata
