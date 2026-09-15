"""Признаки числовых каналов.

На 225 млн числовых замеров в базовом пайплайне приходилось четыре признака —
суточные min/max/mean/std. Для газа, где 93% значений равны ровно нулю, а
замеров 259 в сутки на канал, эти агрегаты физически не выражают сигнал: он в
том, когда и насколько случаются отклонения и не залип ли датчик.
"""
import duckdb

# Окно, относительно которого канал сравнивается сам с собой. Медиана и MAD
# вместо среднего и стандартного отклонения: в этих данных много выбросов,
# и среднее уезжает за одним замером.
BASELINE_DAYS = 30


def add_value_features(con: duckdb.DuckDBPyConnection,
                       source: str = "feat_base") -> None:
    """Робастный остаток, относительное залипание и частота опроса."""
    con.execute(f"""
    CREATE OR REPLACE TABLE _val_base AS
    SELECT ch, day, val_ok_med, n_val_ok, max_flat_run, n_distinct_vals,
           n_val_nonzero, n_val_bad,
           median(val_ok_med) OVER w                 AS val_med_base,
           median(max_flat_run) OVER w               AS flat_run_base,
           avg(CAST(n_val_ok AS DOUBLE)) OVER w      AS n_val_base,
           avg(CAST(n_val_nonzero AS DOUBLE)) OVER w AS nonzero_base
    FROM {source}
    WINDOW w AS (PARTITION BY ch ORDER BY day
                 RANGE BETWEEN INTERVAL {BASELINE_DAYS - 1} DAY PRECEDING
                           AND CURRENT ROW)
    """)
    # MAD считается вторым проходом: медиана отклонения от медианы — агрегат
    # над агрегатом, в одном окне не выражается.
    con.execute(f"""
    CREATE OR REPLACE TABLE _val_mad AS
    SELECT ch, day,
           median(abs(val_ok_med - val_med_base)) OVER (
             PARTITION BY ch ORDER BY day
             RANGE BETWEEN INTERVAL {BASELINE_DAYS - 1} DAY PRECEDING
                       AND CURRENT ROW) AS val_mad
    FROM _val_base
    """)
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_value AS
    SELECT s.*,
           b.val_med_base, m.val_mad,
           -- Робастный остаток: во сколько MAD сегодняшнее значение отстоит
           -- от собственной медианы канала за месяц.
           CASE WHEN m.val_mad > 0
                THEN (b.val_ok_med - b.val_med_base) / m.val_mad END AS val_resid,
           -- Залипание относительно собственной нормы: у газового канала
           -- прогон из сотни нулей — норма, а у температурного — отказ.
           CASE WHEN b.flat_run_base > 0
                THEN CAST(b.max_flat_run AS DOUBLE) / b.flat_run_base
                END AS flat_run_ratio,
           CASE WHEN b.n_val_ok > 0
                THEN CAST(b.n_distinct_vals AS DOUBLE) / b.n_val_ok
                END AS distinct_ratio,
           -- Частота опроса как признак здоровья канала.
           CASE WHEN b.n_val_base > 0
                THEN b.n_val_ok / b.n_val_base END AS sampling_ratio,
           CASE WHEN b.n_val_ok > 0
                THEN CAST(b.n_val_nonzero AS DOUBLE) / b.n_val_ok
                END AS nonzero_frac,
           CASE WHEN b.nonzero_base > 0
                THEN b.n_val_nonzero / b.nonzero_base END AS nonzero_ratio,
           CASE WHEN b.n_val_ok + b.n_val_bad > 0
                THEN CAST(b.n_val_bad AS DOUBLE) / (b.n_val_ok + b.n_val_bad)
                END AS bad_value_frac
    FROM {source} s
    JOIN _val_base b USING (ch, day)
    JOIN _val_mad  m USING (ch, day)
    """)


def add_value_drift(con: duckdb.DuckDBPyConnection,
                    source: str = "feat_value") -> None:
    """Наклон значения за 7 и 30 суток методом наименьших квадратов.

    Считается через ковариацию в скользящем окне: regr_slope как оконная
    функция в DuckDB недоступна, а наклон выражается отношением ковариации
    к дисперсии времени.
    """
    parts = []
    for w in (7, 30):
        parts.append(
            f"\n           CASE WHEN var_pop(CAST(epoch(day) AS DOUBLE)) OVER w{w} > 0"
            f"\n                THEN covar_pop(val_ok_med, CAST(epoch(day) AS DOUBLE))"
            f" OVER w{w}"
            f"\n                   / var_pop(CAST(epoch(day) AS DOUBLE)) OVER w{w}"
            f" * 86400.0"
            f"\n                END AS val_slope_w{w}")
    windows = ",\n".join(
        f"      w{w} AS (PARTITION BY ch ORDER BY day "
        f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW)"
        for w in (7, 30))
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_value AS
    SELECT *,{",".join(parts)}
    FROM {source}
    WINDOW
{windows}
    """)
