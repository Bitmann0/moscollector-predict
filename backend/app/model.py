from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

EVENTS_FILE = "журнал_событий_пример.csv"
CHANNELS_FILE = "справочник_каналов_датчиков.csv"

ANALOG_SENSOR_TYPES = {"Датчик температуры", "Тепловой датчик", "Газовый датчик"}


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
    horizon_hours: int
    predicted_at: str
    valid_until: str
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


def load_frames(data_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    events_path = data_dir / EVENTS_FILE
    channels_path = data_dir / CHANNELS_FILE
    missing = [str(path) for path in (events_path, channels_path) if not path.exists()]
    if missing:
        raise DataValidationError("Не найдены входные файлы: " + ", ".join(missing))

    events = pd.read_csv(events_path)
    channels = pd.read_csv(channels_path)
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
    events["timestamp"] = pd.to_datetime(
        events["дата"].astype(str) + " " + events["время"].astype(str), errors="coerce"
    )
    if events["timestamp"].isna().any():
        raise DataValidationError(f"{EVENTS_FILE}: обнаружены некорректные дата или время")
    if events.empty:
        raise DataValidationError(f"{EVENTS_FILE}: журнал пуст")
    alarm = events["тревожное"].astype(str).str.strip().str.lower().map(
        {"true": True, "false": False, "t": True, "f": False, "1": True, "0": False}
    )
    if alarm.isna().any():
        raise DataValidationError(f"{EVENTS_FILE}: некорректный флаг тревоги")
    events["тревожное"] = alarm.astype(bool)
    events["numeric_value"] = pd.to_numeric(
        events["значение_датчика"].astype(str).str.replace(",", ".", regex=False),
        errors="coerce",
    )
    return events, channels.drop_duplicates("ид_канала_данных", keep="last")


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
        "critical": "Проверить канал в течение 4 часов и подготовить замену датчика.",
        "high": "Назначить внеплановую диагностику в ближайшую смену.",
        "medium": "Проверить соединения при ближайшем обходе и наблюдать динамику.",
        "low": "Продолжить штатный мониторинг.",
    }[level]


def _location_from_tag(tag: str) -> str:
    clean = str(tag).strip().strip(".")
    return f"Участок {clean}" if clean else "Участок не определён"


def build_forecasts(
    events: pd.DataFrame,
    channels: pd.DataFrame,
    horizon_hours: int = 24,
) -> list[Forecast]:
    """Строит объяснимое ранжирование риска деградации для каждого активного канала."""
    ordered = events.sort_values(["ид_канала_данных", "timestamp"]).copy()
    ordered["interval_seconds"] = (
        ordered.groupby("ид_канала_данных")["timestamp"].diff().dt.total_seconds()
    )
    ordered["short_interval"] = ordered["interval_seconds"].le(60).fillna(False)

    grouped = ordered.groupby("ид_канала_данных", sort=False)
    features = grouped.agg(
        event_count=("ид_события", "size"),
        alarm_count=("тревожное", "sum"),
        alarm_rate=("тревожное", "mean"),
        short_interval_rate=("short_interval", "mean"),
        unique_values=("значение_датчика", "nunique"),
        numeric_coverage=("numeric_value", lambda values: float(values.notna().mean())),
    ).reset_index()
    features["dominant_value_rate"] = grouped["значение_датчика"].apply(
        lambda values: float(values.value_counts(normalize=True).iloc[0])
    ).to_numpy()

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

    # Исторический replay отсчитывает горизонт от конца доступных наблюдений.
    # Часовой пояс выгрузки предполагается Europe/Moscow до подтверждения заказчиком.
    predicted_at = events["timestamp"].max().tz_localize("Europe/Moscow").to_pydatetime()
    valid_until = predicted_at + timedelta(hours=horizon_hours)
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
                horizon_hours=horizon_hours,
                predicted_at=predicted_at.isoformat(),
                valid_until=valid_until.isoformat(),
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


def analyze(data_dir: Path, horizon_hours: int = 24) -> tuple[list[Forecast], dict[str, Any]]:
    events, channels = load_frames(data_dir)
    forecasts = build_forecasts(events, channels, horizon_hours)
    metadata = {
        "event_count": len(events),
        "channel_count": int(events["ид_канала_данных"].nunique()),
        "catalog_channel_count": len(channels),
        "data_from": events["timestamp"].min().isoformat(),
        "data_to": events["timestamp"].max().isoformat(),
        "model_name": "sensor-failure-baseline",
        "model_version": "0.1.0",
        "model_status": "baseline_unvalidated",
    }
    return forecasts, metadata
