import datetime as dt
import json
import urllib.error
import urllib.request

import duckdb
import numpy as np
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
        cached = pl.read_parquet(WEATHER_CACHE)
        # Кэш годится, только если в нём есть всё, что обещает WEATHER_COLUMNS.
        # Без этой проверки добавление производного признака молча не доезжало
        # до фичестора: функция возвращала старый файл, а сборка падала на
        # отсутствующей колонке через десять минут работы.
        missing = [c for c in WEATHER_COLUMNS if c not in cached.columns]
        if not missing:
            return cached
        print(f"кэш погоды устарел (нет {', '.join(missing[:4])}) — пересчитываю",
              flush=True)
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
    # Индекс предшествующего увлажнения: API_t = k * API_{t-1} + P_t.
    # Стандартный прокси насыщения грунта. Трёхсуточной суммы для подтопления
    # мало — грунт помнит осадки неделями, и именно память, а не суточный
    # максимум, определяет, куда денется следующий дождь. Три коэффициента
    # затухания: k=0.85 держит память около недели, k=0.95 — около месяца.
    p_arr = df["precip_mm"].fill_null(0.0).to_numpy()
    for k in (0.85, 0.90, 0.95):
        api = np.empty(len(p_arr))
        acc = 0.0
        for i, v in enumerate(p_arr):
            acc = k * acc + v
            api[i] = acc
        df = df.with_columns(pl.Series(f"api_{int(k * 100)}", api))
    df = df.with_columns([
        pl.col("precip_mm").rolling_sum(7, min_samples=1).alias("precip_7d"),
        pl.col("precip_mm").rolling_sum(14, min_samples=1).alias("precip_14d"),
        pl.col("precip_mm").rolling_sum(30, min_samples=1).alias("precip_30d"),
        pl.col("snowmelt").rolling_sum(7, min_samples=1).alias("snowmelt_7d"),
        pl.col("snowmelt").rolling_sum(30, min_samples=1).alias("snowmelt_30d"),
    ])
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
                   "snowmelt", "snow_delta",
                   "api_85", "api_90", "api_95",
                   "precip_7d", "precip_14d", "precip_30d",
                   "snowmelt_7d", "snowmelt_30d")


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
    cols = ", ".join(f"w.{c}" for c in WEATHER_COLUMNS)
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_ext AS
    SELECT e.*, {cols}
    FROM feat_ext e
    LEFT JOIN read_parquet('{WEATHER_CACHE}') w ON w.day = e.day
    """)
    return True
