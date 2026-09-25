"""Воспроизводимость приёма: два прохода обязаны дать одинаковые данные.

После перевода дедупликации на полный кортеж выбирать нечего, и приём должен
быть детерминирован по построению. Это проверка, а не замер разброса: если
контрольные суммы совпали, вклад пересборки в шум ровно ноль, и планкой
значимости для любого рычага остаётся только разброс по фолдам.

Прежде это было не так. `DISTINCT ON (ид_события)` без `ORDER BY` выбирал
выжившую строку недетерминированно, и три пересборки одного года давали три
разных датасета — то есть ни одну дельту между ступенями эксперимента нельзя
было отличить от разницы пересборок.
"""
import hashlib
import sys

import duckdb

from mkl import ingest
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_YEARS = (2021, 2022)  # годы с коллизиями ид_события


def digest(year: int) -> tuple[str, int]:
    """Отпечаток содержимого, не зависящий от порядка строк и сжатия."""
    con = duckdb.connect()
    src = PATHS.interim / f"events_year={year}.parquet"
    row = con.execute(f"""
        SELECT count(*),
               sum(hash(event_id) % 1000000007),
               sum(hash(ch) % 1000000007),
               sum(hash(ts) % 1000000007),
               sum(hash(coalesce(val_raw,'')) % 1000000007)
        FROM read_parquet('{src}')
    """).fetchone()
    con.close()
    return hashlib.sha256(str(row[1:]).encode()).hexdigest()[:16], row[0]


def main() -> None:
    years = [int(a) for a in sys.argv[1:]] or list(DEFAULT_YEARS)
    print(f"{'год':>6} {'проход 1':>18} {'проход 2':>18} {'строк':>12}  итог")
    ok = True
    for y in years:
        ingest.build_events([y])
        h1, n1 = digest(y)
        ingest.build_events([y])
        h2, n2 = digest(y)
        same = h1 == h2 and n1 == n2
        ok &= same
        print(f"{y:>6} {h1:>18} {h2:>18} {n1:>12,}  "
              f"{'совпало' if same else 'РАСХОЖДЕНИЕ'}", flush=True)
    print("\nприём детерминирован; вклад пересборки в шум равен нулю" if ok
          else "\nприём НЕ детерминирован — дельты рычагов несравнимы")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
