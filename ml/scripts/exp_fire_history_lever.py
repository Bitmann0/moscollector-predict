"""Рычаг «история тревог участка» для пожарной головы B (reports/FIRE_FLOOD_PRODUCT.md).

Дописывает в segment.parquet три колонки второго шага
compute.build_segment_level: число дней с тревогой, их долю, давность последней.
В фичесторе бандла их нет: segment.parquet собран 23.09 до этого шага.

MKL_ROOT — отдельный корень назначения: его configs/features.yaml обновит
store.write, и модели B этого корня обучатся на другом отпечатке признаков.
argv[1] — исходный segment.parquet без этих колонок.

    MKL_ROOT=<новый корень> python scripts/exp_fire_history_lever.py <старый>/data/features/segment.parquet
    MKL_ROOT=<новый корень> python scripts/eval_fire_flood_product.py B --output reports/fire_history_lever.json
"""
import sys

from mkl import db, store

src = sys.argv[1]
con = db.connect()
con.execute(f"""
CREATE OR REPLACE TABLE feat_segment AS
SELECT s.*,
       sum(CAST(n_fire > 0 AS INTEGER)) OVER w AS fire_days_to_date,
       CAST(sum(CAST(n_fire > 0 AS INTEGER)) OVER w AS DOUBLE)
         / count(*) OVER w AS fire_rate_to_date,
       date_diff('day',
         max(CASE WHEN n_fire > 0 THEN day END) OVER w, day)
         AS days_since_fire
FROM read_parquet('{src}') s
WINDOW w AS (PARTITION BY obj, seg ORDER BY day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
""")
dst = store.write(con, "feat_segment", "segment")
print(dst, con.execute("SELECT count(*), max(fire_days_to_date) FROM feat_segment").fetchone())
con.close()
