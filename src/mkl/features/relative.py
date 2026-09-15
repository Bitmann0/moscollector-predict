import duckdb

SEG_SIZE = 10.0  # пикет = 100 м, значит сегмент = 1 км


def add_peer_features(con: duckdb.DuckDBPyConnection, source: str = "feat_base") -> None:
    """Сравнение канала с датчиками того же типа на том же объекте в те же сутки.

    Снимает сезонность и специфику объекта без явного моделирования: если весь
    объект шумит одинаково — это режим, а если шумит один канал — это он сам.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_peer AS
    SELECT *,
           CAST(floor(coalesce(picket, 0) / {SEG_SIZE}) AS INTEGER) AS seg,
           median(CAST(n_alarms AS DOUBLE))     OVER w AS peer_median_alarms,
           avg(CAST(n_events AS DOUBLE))        OVER w AS peer_mean_events,
           stddev_pop(CAST(n_events AS DOUBLE)) OVER w AS peer_std_events,
           avg(CAST(n_bad AS DOUBLE))           OVER w AS peer_mean_bad,
           count(*)                             OVER w AS peer_count
    FROM {source}
    WINDOW w AS (PARTITION BY obj, stype, day)
    """)
    con.execute("""
    CREATE OR REPLACE TABLE feat_peer AS
    SELECT *,
           CASE WHEN peer_median_alarms > 0 THEN n_alarms / peer_median_alarms
                WHEN n_alarms > 0 THEN CAST(n_alarms AS DOUBLE)
                ELSE 1.0 END AS peer_ratio_alarms,
           CASE WHEN peer_std_events > 0
                THEN (n_events - peer_mean_events) / peer_std_events
                ELSE 0.0 END AS peer_z_events,
           CASE WHEN peer_mean_bad > 0 THEN n_bad / peer_mean_bad
                WHEN n_bad > 0 THEN CAST(n_bad AS DOUBLE)
                ELSE 0.0 END AS peer_ratio_bad
    FROM feat_peer
    """)


def add_spatial_features(con: duckdb.DuckDBPyConnection, radius_seg: int = 1) -> None:
    """Соседство по пикетам через сегментные агрегаты.

    Прямой self-join каналов внутри объекта даёт O(n^2): на объекте с 1455
    каналами это 2,7 млрд пар. Сегментная агрегация делает то же самое за O(n).
    """
    con.execute("""
    CREATE OR REPLACE TABLE seg_daily AS
    SELECT obj, seg, day,
           sum(n_bad)     AS seg_bad,
           sum(n_alarms)  AS seg_alarms,
           sum(n_fire)    AS seg_fire,
           sum(n_events)  AS seg_events,
           count(*)       AS seg_channels,
           max(val_max)   AS seg_val_max,
           avg(val_mean)  AS seg_val_mean
    FROM feat_peer WHERE obj IS NOT NULL
    GROUP BY obj, seg, day
    """)

    offsets = range(-radius_seg, radius_seg + 1)
    joins = "\n    ".join(
        f"LEFT JOIN seg_daily s{i} ON s{i}.obj = p.obj AND s{i}.day = p.day "
        f"AND s{i}.seg = p.seg + ({o})"
        for i, o in enumerate(offsets)
    )
    n = len(list(offsets))
    sum_expr = lambda col: " + ".join(f"coalesce(s{i}.{col}, 0)" for i in range(n))

    con.execute(f"""
    CREATE OR REPLACE TABLE feat_spatial AS
    SELECT p.*,
           ({sum_expr('seg_bad')})    - p.n_bad    AS nbr_bad,
           ({sum_expr('seg_alarms')}) - p.n_alarms AS nbr_alarms,
           ({sum_expr('seg_fire')})   - p.n_fire   AS nbr_fire,
           ({sum_expr('seg_channels')}) - 1        AS nbr_channels,
           greatest({', '.join(f'coalesce(s{i}.seg_val_max, -1e9)' for i in range(n))})
             AS nbr_val_max,
           CASE WHEN p.val_mean IS NOT NULL AND s{radius_seg}.seg_val_mean IS NOT NULL
                THEN p.val_mean - s{radius_seg}.seg_val_mean END AS val_minus_seg_mean
    FROM feat_peer p
    {joins}
    """)
