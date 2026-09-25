import datetime as dt

import duckdb
import polars as pl
import pytest

from mkl.features import external


def test_calendar_features_are_cyclic():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE feat_full (ch BIGINT, day DATE)")
    con.execute("INSERT INTO feat_full VALUES (1, DATE '2025-01-01'), (1, DATE '2025-07-01')")
    external.add_calendar(con)
    rows = con.execute(
        "SELECT day, dow, month, doy_sin, doy_cos, is_holiday FROM feat_ext ORDER BY day"
    ).fetchall()
    assert rows[0][2] == 1 and rows[1][2] == 7
    assert rows[0][5] == 1, "1 января — праздник"
    assert rows[1][5] == 0
    for r in rows:
        assert -1.0 <= r[3] <= 1.0 and -1.0 <= r[4] <= 1.0
    con.close()


def test_weather_columns_appear_even_without_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(external, "WEATHER_CACHE", tmp_path / "missing.parquet")
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE feat_full (ch BIGINT, day DATE)")
    con.execute("INSERT INTO feat_full VALUES (1, DATE '2025-01-01')")
    external.add_calendar(con)
    assert external.add_weather(con) is False
    cols = {r[0] for r in con.execute("DESCRIBE feat_ext").fetchall()}
    assert set(external.WEATHER_COLUMNS) <= cols
    con.close()


def test_weather_cache_is_reused(tmp_path, monkeypatch):
    cache = tmp_path / "weather.parquet"
    calls = []

    def fake_download(start, end):
        calls.append((start, end))
        return pl.DataFrame({
            "day": [dt.date(2025, 1, 1)], "t_mean": [1.0], "t_min": [0.0],
            "t_max": [2.0], "precip_mm": [0.0], "snow_depth_cm": [0.0],
        })

    monkeypatch.setattr(external, "_download_weather", fake_download)
    monkeypatch.setattr(external, "WEATHER_CACHE", cache)
    external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 1))
    external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 1))
    assert len(calls) == 1, "второй вызов обязан читать кэш"


def test_network_failure_degrades_gracefully(tmp_path, monkeypatch):
    def boom(start, end):
        raise OSError("нет сети")

    monkeypatch.setattr(external, "_download_weather", boom)
    monkeypatch.setattr(external, "WEATHER_CACHE", tmp_path / "missing.parquet")
    assert external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 2)) is None


def test_stale_weather_cache_is_recomputed_not_returned(tmp_path, monkeypatch):
    """Кэш годится, только если в нём есть всё, что обещает WEATHER_COLUMNS.

    Без этой проверки добавление производного признака молча не доезжало до
    фичестора: функция возвращала старый файл, а сборка падала на отсутствующей
    колонке через десять минут работы.
    """
    import datetime as dt
    import polars as pl
    from mkl.features import external

    stale = tmp_path / "weather.parquet"
    pl.DataFrame({"day": [dt.date(2025, 1, 1)], "t_mean": [0.0]}).write_parquet(stale)
    monkeypatch.setattr(external, "WEATHER_CACHE", stale)
    called = {"n": 0}

    def _fail(*a, **k):
        called["n"] += 1
        raise OSError("сеть недоступна")

    monkeypatch.setattr(external, "_download_weather", _fail)
    got = external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 2))
    assert called["n"] == 1, "устаревший кэш обязан вызвать пересчёт"
    assert got is None, "сеть недоступна — честно возвращаем None"


def test_complete_cache_is_reused_without_network(tmp_path, monkeypatch):
    import datetime as dt
    import polars as pl
    from mkl.features import external

    full = tmp_path / "weather.parquet"
    pl.DataFrame({"day": [dt.date(2025, 1, 1)],
                  **{c: [0.0] for c in external.WEATHER_COLUMNS}}).write_parquet(full)
    monkeypatch.setattr(external, "WEATHER_CACHE", full)
    monkeypatch.setattr(external, "_download_weather",
                        lambda *a, **k: pytest.fail("сеть трогать не должны"))
    assert external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 1)) is not None


def test_weather_cache_refreshes_when_new_dates_arrive(tmp_path, monkeypatch):
    cache = tmp_path / "weather.parquet"
    pl.DataFrame({"day": [dt.date(2025, 1, 1)],
                  **{c: [0.0] for c in external.WEATHER_COLUMNS}}).write_parquet(cache)
    monkeypatch.setattr(external, "WEATHER_CACHE", cache)
    called = []

    def download(start, end):
        called.append((start, end))
        return pl.DataFrame({
            "day": [dt.date(2025, 1, 1), dt.date(2025, 1, 2)],
            "t_mean": [0.0, 0.0], "t_min": [0.0, 0.0],
            "t_max": [0.0, 0.0], "precip_mm": [0.0, 0.0],
            "snow_depth_cm": [0.0, 0.0],
        })

    monkeypatch.setattr(external, "_download_weather", download)
    got = external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 2))
    assert called == [(dt.date(2025, 1, 1), dt.date(2025, 1, 2))]
    assert got["day"].max() == dt.date(2025, 1, 2)
