"""Экспоненциально затухающая интенсивность событий.

Прямоугольное окно 7/30 взвешивает вчерашний всплеск и всплеск шестидневной
давности одинаково, а на седьмые сутки обнуляет его разом. Затухающее окно
ближе к тому, как ведёт себя самовозбуждающийся процесс: вклад события убывает
плавно, и «вчера» весит больше, чем «неделю назад».

Считается отдельным проходом, а не оконной функцией: рекуррента
ewma_t = x_t + r^gap * ewma_{t-1} в SQL без рекурсии не выражается, а попытка
записать её через накопленную сумму с обратными весами переполняет double уже
на паре сотен суток.

Затухание отсчитывается по СУТКАМ, а не по строкам панели. Строки есть только у
тех суток, когда канал отчитывался, и порядковое затухание считало бы месячный
простой одним шагом.
"""
import duckdb
import numpy as np
import polars as pl

DECAY_COLS = ("n_events", "n_alarms", "n_bad")
HALF_LIVES = (1, 7, 30)


def _ewma(ch: np.ndarray, day: np.ndarray, x: np.ndarray,
          half_life: float) -> np.ndarray:
    """ewma_t = x_t + r^gap * ewma_{t-1}, по каналам, с разрывами в сутках."""
    r = 0.5 ** (1.0 / half_life)
    out = np.empty(len(x), dtype=np.float64)
    acc = 0.0
    prev_ch = None
    prev_day = 0
    for i in range(len(x)):
        c = ch[i]
        if c != prev_ch:
            acc = 0.0
            prev_ch = c
        else:
            acc *= r ** max(int(day[i] - prev_day), 0)
        acc += x[i]
        out[i] = acc
        prev_day = day[i]
    return out


def add_decayed_intensity(con: duckdb.DuckDBPyConnection,
                          source: str = "daily_channel") -> None:
    cols = ", ".join(DECAY_COLS)
    df = con.execute(
        f"SELECT ch, day, {cols} FROM {source} ORDER BY ch, day").pl()
    ch = df["ch"].to_numpy()
    day = df["day"].cast(pl.Int32).to_numpy()          # сутки от эпохи
    series = {}
    for c in DECAY_COLS:
        x = df[c].fill_null(0).cast(pl.Float64).to_numpy()
        for hl in HALF_LIVES:
            series[f"ewma_{c}_hl{hl}"] = _ewma(ch, day, x, hl)
    out = df.select(["ch", "day"]).with_columns(
        [pl.Series(k, v) for k, v in series.items()])
    # Отношения масштабов: во сколько раз свежая интенсивность выше фоновой.
    # Сам по себе уровень зависит от того, насколько канал разговорчив,
    # а отношение — нет.
    for c in DECAY_COLS:
        out = out.with_columns(
            (pl.col(f"ewma_{c}_hl1") / (pl.col(f"ewma_{c}_hl7") + 1e-9))
              .alias(f"ewma_{c}_r1_7"),
            (pl.col(f"ewma_{c}_hl7") / (pl.col(f"ewma_{c}_hl30") + 1e-9))
              .alias(f"ewma_{c}_r7_30"),
        )
    con.register("_ewma_df", out)
    con.execute("CREATE OR REPLACE TABLE feat_decay AS SELECT * FROM _ewma_df")
    con.unregister("_ewma_df")


DECAY_FEATURES = tuple(
    [f"ewma_{c}_hl{hl}" for c in DECAY_COLS for hl in HALF_LIVES]
    + [f"ewma_{c}_r1_7" for c in DECAY_COLS]
    + [f"ewma_{c}_r7_30" for c in DECAY_COLS]
)
