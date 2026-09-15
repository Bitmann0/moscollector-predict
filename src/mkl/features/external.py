import datetime as dt
import json
import urllib.error
import urllib.request

import duckdb
import polars as pl

from ..config import PATHS

WEATHER_CACHE = PATHS.interim / "weather.parquet"
LAT, LON = 55.75, 37.62

# Нерабочие праздничные дни РФ, без переносов: перенос года к году меняется,
# а нам нужен устойчивый признак «не рабочий день по календарю».
RU_HOLIDAYS_MMDD = frozenset({
    (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
    (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4),
})


def _download_weather(start: dt.date, end: dt.date) -> pl.DataFrame:
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={LAT}&longitude={LON}&start_date={start}&end_date={end}"
        "&daily=temperature_2m_mean,temperature_2m_min,temperature_2m_max,"
        "precipitation_sum,snow_depth_max&timezone=Europe%2FMoscow"
    )
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = json.load(resp)
    d = payload["daily"]
    return pl.DataFrame({
        "day": [dt.date.fromisoformat(x) for x in d["time"]],
        "t_mean": d["temperature_2m_mean"],
        "t_min": d["temperature_2m_min"],
        "t_max": d["temperature_2m_max"],
        "precip_mm": d["precipitation_sum"],
        "snow_depth_cm": [(v or 0.0) * 100 for v in d["snow_depth_max"]],
    })


def fetch_moscow_weather(start: dt.date, end: dt.date) -> pl.DataFrame | None:
    """Погода Москвы из Open-Meteo. При недоступности сети возвращает None,
    и пайплайн продолжает работу на календарных фичах."""
    if WEATHER_CACHE.exists():
        return pl.read_parquet(WEATHER_CACHE)
    try:
        df = _download_weather(start, end)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError) as exc:
        print(f"погода недоступна ({exc}); продолжаем без неё", flush=True)
        return None
    df = df.sort("day").with_columns([
        (pl.col("t_max") - pl.col("t_min")).alias("t_range"),
        (pl.col("t_mean") * (pl.col("t_mean") < 0)).alias("frost_intensity"),
    ])
    # Подтопление вызывают не сегодняшние осадки, а накопленные: вода доходит
    # до коллектора с задержкой. Снеготаяние — переход температуры через ноль
    # при ненулевой глубине снега.
    df = df.with_columns([
        pl.col("precip_mm").rolling_sum(1).alias("precip_24h"),
        pl.col("precip_mm").rolling_sum(2, min_samples=1).alias("precip_48h"),
        pl.col("precip_mm").rolling_sum(3, min_samples=1).alias("precip_72h"),
        (pl.col("snow_depth_cm") - pl.col("snow_depth_cm").shift(1))
            .alias("snow_delta"),
    ])
    df = df.with_columns(
        pl.when((pl.col("t_max") > 0) & (pl.col("snow_depth_cm") > 0))
          .then(pl.col("t_max") * pl.col("snow_depth_cm"))
          .otherwise(0.0).alias("snowmelt")
    )
    WEATHER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(WEATHER_CACHE)
    return df


def add_calendar(con: duckdb.DuckDBPyConnection, source: str = "feat_full") -> None:
    holidays = ",".join(f"({m},{d})" for m, d in sorted(RU_HOLIDAYS_MMDD))
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_ext AS
    SELECT *,
           dayofweek(day)  AS dow,
           month(day)      AS month,
           CASE WHEN dayofweek(day) IN (0, 6) THEN 1 ELSE 0 END AS is_weekend,
           CASE WHEN (month(day), day(day)) IN ({holidays}) THEN 1 ELSE 0 END AS is_holiday,
           sin(2 * pi() * dayofyear(day) / 365.25) AS doy_sin,
           cos(2 * pi() * dayofyear(day) / 365.25) AS doy_cos
    FROM {source}
    """)


WEATHER_COLUMNS = ("t_mean", "t_min", "t_max", "precip_mm",
                   "snow_depth_cm", "t_range", "frost_intensity",
                   "precip_24h", "precip_48h", "precip_72h",
                   "snowmelt", "snow_delta")


def add_weather(con: duckdb.DuckDBPyConnection) -> bool:
    """Приклеить погоду к feat_ext.

    Если кэша нет, колонки всё равно создаются пустыми: схема фичестора обязана
    быть одинаковой на обучении и на инференсе, иначе модель не сможет принять
    вектор признаков.
    """
    if not WEATHER_CACHE.exists():
        nulls = ", ".join(f"CAST(NULL AS DOUBLE) AS {c}" for c in WEATHER_COLUMNS)
        con.execute(f"CREATE OR REPLACE TABLE feat_ext AS SELECT *, {nulls} FROM feat_ext")
        return False
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_ext AS
    SELECT e.*, w.t_mean, w.t_min, w.t_max, w.precip_mm, w.snow_depth_cm,
           w.t_range, w.frost_intensity, w.precip_24h, w.precip_48h,
           w.precip_72h, w.snowmelt, w.snow_delta
    FROM feat_ext e
    LEFT JOIN read_parquet('{WEATHER_CACHE}') w ON w.day = e.day
    """)
    return True
