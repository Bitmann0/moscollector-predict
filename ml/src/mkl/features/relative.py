import duckdb

from ..config import SEG_SIZE, SEG_UNKNOWN, seg_sql  # noqa: F401  (SEG_SIZE — обратная совместимость)


def add_peer_features(con: duckdb.DuckDBPyConnection, source: str = "feat_base") -> None:
    """Сравнение канала с датчиками того же типа на том же объекте в те же сутки.

    Снимает сезонность и специфику объекта без явного моделирования: если весь
    объект шумит одинаково — это режим, а если шумит один канал — это он сам.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_peer AS
    SELECT *,
           {seg_sql()} AS seg,
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


def add_object_context(con: duckdb.DuckDBPyConnection,
                       source: str = "feat_spatial") -> None:
    """Состояние объекта как контекст для отдельного канала.

    86% отказов в данных — групповые: падает концентратор, линия или питание.
    Значит вероятность отказа конкретного канала сильнее всего зависит от того,
    что происходит с его объектом, а не только с ним самим.
    """
    con.execute(f"""
    CREATE OR REPLACE TABLE obj_daily AS
    SELECT obj, day,
           count(*)                                   AS obj_n_channels,
           sum(n_bad)                                 AS obj_n_bad,
           sum(n_alarms)                              AS obj_n_alarms,
           sum(n_events)                              AS obj_n_events,
           sum(n_bad_w7)                              AS obj_n_bad_w7,
           sum(n_bad_w30)                             AS obj_n_bad_w30,
           sum(n_alarms_w7)                           AS obj_n_alarms_w7,
           count(*) FILTER (WHERE n_bad > 0)          AS obj_channels_bad,
           avg(CAST(n_bad > 0 AS DOUBLE))             AS obj_frac_bad,
           avg(silence_z)                             AS obj_silence_z_mean,
           max(silence_z)                             AS obj_silence_z_max,
           avg(days_since_last_bad)                   AS obj_days_since_bad_mean
    FROM {source} WHERE obj IS NOT NULL
    GROUP BY obj, day
    """)
    # Уровень комплекса: официальная иерархия связывает 78 объектов в 16
    # комплексов, и авария питания или обрыв магистрали проявляются именно
    # на этом уровне, а не на отдельной охранной зоне.
    con.execute(f"""
    CREATE OR REPLACE TABLE par_daily AS
    SELECT obj_parent, day,
           count(*)                       AS par_n_channels,
           sum(n_bad)                     AS par_n_bad,
           sum(n_alarms)                  AS par_n_alarms,
           sum(n_bad_w7)                  AS par_n_bad_w7,
           count(*) FILTER (WHERE n_bad > 0) AS par_channels_bad,
           avg(CAST(n_bad > 0 AS DOUBLE)) AS par_frac_bad
    FROM {source} WHERE obj_parent IS NOT NULL
    GROUP BY obj_parent, day
    """)
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_objctx AS
    SELECT s.*,
           o.obj_n_channels, o.obj_n_bad, o.obj_n_alarms, o.obj_n_events,
           o.obj_n_bad_w7, o.obj_n_bad_w30, o.obj_n_alarms_w7,
           o.obj_channels_bad, o.obj_frac_bad,
           o.obj_silence_z_mean, o.obj_silence_z_max, o.obj_days_since_bad_mean,
           CASE WHEN o.obj_n_alarms > 0
                THEN CAST(s.n_alarms AS DOUBLE) / o.obj_n_alarms END AS share_obj_alarms,
           CASE WHEN o.obj_n_bad > 0
                THEN CAST(s.n_bad AS DOUBLE) / o.obj_n_bad END AS share_obj_bad
    FROM {source} s
    LEFT JOIN obj_daily o ON o.obj = s.obj AND o.day = s.day
    """)
    con.execute("""
    CREATE OR REPLACE TABLE feat_objctx AS
    SELECT s.*, p.par_n_channels, p.par_n_bad, p.par_n_alarms, p.par_n_bad_w7,
           p.par_channels_bad, p.par_frac_bad,
           CASE WHEN p.par_n_bad > 0 THEN s.n_bad / p.par_n_bad END AS par_share_bad
    FROM feat_objctx s
    LEFT JOIN par_daily p ON p.obj_parent = s.obj_parent AND p.day = s.day
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
           -- nullif убирает сентинел: без него отсутствие соседей попало бы
           -- в модель как настоящее значение -1e9.
           nullif(greatest({', '.join(f'coalesce(s{i}.seg_val_max, -1e9)' for i in range(n))}),
                  -1e9) AS nbr_val_max,
           CASE WHEN p.val_mean IS NOT NULL AND s{radius_seg}.seg_val_mean IS NOT NULL
                THEN p.val_mean - s{radius_seg}.seg_val_mean END AS val_minus_seg_mean
    FROM feat_peer p
    {joins}
    """)
