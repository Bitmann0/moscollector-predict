import re
import sys

from .config import PATHS
from .db import attach_events, connect

_PICKET_RE = re.compile(r"ПК\s*(\d+(?:[.,]\d+)?)")

_RAW_COLUMNS = (
    "{'ид_события':'VARCHAR','ид_канала_данных':'VARCHAR','дата':'VARCHAR',"
    "'время':'VARCHAR','тревожное':'VARCHAR','значение_датчика':'VARCHAR'}"
)


def parse_picket(name: str | None) -> float | None:
    if not name:
        return None
    m = _PICKET_RE.search(name)
    return float(m.group(1).replace(",", ".")) if m else None


def parse_object(tag: str | None) -> str | None:
    return tag.split(".")[0] if tag else None


def build_channels() -> int:
    """Справочник каналов с официальной привязкой к объектам.

    До обновления датасета 16.09 поля `ид_объект` не было, и объект выводился
    из префикса тега. Это оказалось неверно: соответствие многие-ко-многим —
    префикс 418-1 покрывает 7 разных объектов, а объект 5122 разбросан по 8
    префиксам. Официальная привязка даёт 78 объектов вместо 83 псевдообъектов
    и открывает иерархию: лист (охранная зона или диспетчерская) -> комплекс
    -> район.
    """
    con = connect()
    src = PATHS.materials / "справочник_каналов_датчиков.csv"
    obj_src = PATHS.materials / "справочник_объектов_диспетчер.csv"
    dst = PATHS.interim / "channels.parquet"
    con.execute(f"""
        CREATE OR REPLACE TABLE _obj AS
        SELECT TRY_CAST(ид_объект AS BIGINT) AS obj_id,
               TRY_CAST(иерархия_уровень AS INTEGER) AS obj_level,
               TRY_CAST(родитель AS BIGINT) AS obj_parent_id,
               вид_объекта AS obj_kind,
               диспетчерское_название_объекта AS obj_name
        FROM read_csv('{obj_src}', header=true)
    """)
    con.execute(f"""
        COPY (
          SELECT TRY_CAST(c.ид_канала_данных AS BIGINT) AS ch,
                 c.тип_инж_системы AS sys,
                 c.тип_датчика     AS stype,
                 c.тег_инженерной_системы AS tag,
                 c.название_датчика AS sname,
                 CAST(TRY_CAST(c.ид_объект AS BIGINT) AS VARCHAR) AS obj,
                 CAST(coalesce(o.obj_parent_id,
                               TRY_CAST(c.ид_объект AS BIGINT)) AS VARCHAR)
                   AS obj_parent,
                 o.obj_kind, o.obj_level,
                 -- Диспетчерское название объекта и название комплекса.
                 -- Читались во временную таблицу и до диска не доходили, из-за
                 -- чего алерт адресовался числом: «объект 3215». Диспетчеру
                 -- нужно название, которым он пользуется сам.
                 o.obj_name,
                 (SELECT p.obj_name FROM _obj p
                  WHERE p.obj_id = o.obj_parent_id) AS obj_parent_name,
                 split_part(c.тег_инженерной_системы,'.',1) AS tag_prefix,
                 TRY_CAST(replace(regexp_extract(c.название_датчика,
                   'ПК\\s*(\\d+(?:[.,]\\d+)?)', 1), ',', '.') AS DOUBLE) AS picket
          FROM read_csv('{src}', header=true) c
          LEFT JOIN _obj o ON o.obj_id = TRY_CAST(c.ид_объект AS BIGINT)
        ) TO '{dst}' (FORMAT PARQUET)
    """)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
    con.close()
    return n


def build_events(years: list[int]) -> dict[int, int]:
    con = connect()
    con.execute(
        f"CREATE TABLE ref AS SELECT * FROM read_parquet('{PATHS.interim / 'channels.parquet'}')"
    )
    counts: dict[int, int] = {}
    for y in years:
        src = PATHS.raw / f"ext-journal-{y}.csv"
        dst = PATHS.interim / f"events_year={y}.parquet"
        con.execute(f"""
        COPY (
          SELECT e.event_id, e.ch, (e.d + e.t) AS ts, e.d AS day, e.alarm,
                 e.val AS val_raw, TRY_CAST(e.val AS DOUBLE) AS val_num,
                 r.sys, r.stype, r.tag, r.sname, r.obj, r.obj_parent,
                 r.obj_kind, r.picket
          FROM (
            -- Дедупликация по полному кортежу, а не по ид_события.
            --
            -- Идентификатор ключом не является: в 2021-2023 он переиспользуется
            -- разными событиями. DISTINCT ON (event_id) удалял 1 669 268 настоящих
            -- событий — 775 099 в 2021, 667 317 в 2022, 226 852 в 2023; в остальные
            -- пять лет коллизий нет вообще, так что это дефект именно тех трёх лет,
            -- а не устройство источника.
            --
            -- Вторая причина: DISTINCT ON без ORDER BY выбирает выжившую строку
            -- недетерминированно, и три пересборки одного года давали три разных
            -- датасета. При дедупликации по полному кортежу выбирать нечего —
            -- недетерминизм исчезает по построению, а не затыкается сортировкой.
            SELECT DISTINCT
                   TRY_CAST(ид_события AS BIGINT) AS event_id,
                   TRY_CAST(ид_канала_данных AS BIGINT) AS ch,
                   TRY_CAST(дата AS DATE) AS d,
                   TRY_CAST(время AS TIME) AS t,
                   lower(тревожное) IN ('t','true') AS alarm,
                   значение_датчика AS val
            FROM read_csv('{src}', all_varchar=true, header=true, columns={_RAW_COLUMNS})
            WHERE ид_события <> 'ид_события'
          ) e
          LEFT JOIN ref r ON r.ch = e.ch
          WHERE e.event_id IS NOT NULL AND e.ch IS NOT NULL AND e.d IS NOT NULL
        ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1000000)
        """)
        counts[y] = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
        print(f"  {y}: {counts[y]:,}", flush=True)
    con.close()
    return counts


def quality_report() -> str:
    con = connect()
    attach_events(con)
    result = con.execute("""
        SELECT year(day) AS год,
               count(*) AS события,
               count(DISTINCT ch) AS каналы,
               count(*) FILTER (WHERE stype IS NULL) AS без_справочника,
               count(*) FILTER (WHERE alarm) AS тревоги,
               count(*) FILTER (WHERE val_num IS NOT NULL) AS числовые
        FROM ev GROUP BY 1 ORDER BY 1
    """)
    headers = [col[0] for col in result.description]
    rows = result.fetchall()
    total = con.execute(
        "SELECT count(*), count(DISTINCT ch), min(day), max(day) FROM ev"
    ).fetchone()
    con.close()
    lines = [
        "# Отчёт качества данных",
        "",
        f"Событий после дедупликации: **{total[0]:,}**. Каналов: **{total[1]:,}**. "
        f"Период: {total[2]} … {total[3]}.",
        "",
        "| " + " | ".join(headers) + " |\n"
        + "| " + " | ".join("---" for _ in headers) + " |\n"
        + "\n".join("| " + " | ".join(str(value) for value in row) + " |"
                    for row in rows),
        "",
        "Обработанные дефекты: полные дубли строк снимаются `SELECT DISTINCT` "
        "по кортежу (ид_события, канал, дата, время, тревожное, значение) — "
        "по одному лишь `ид_события` дедуплицировать нельзя, он переиспользуется "
        "разными событиями в 2021-2023 и такая дедупликация удаляла 1 669 268 "
        "настоящих событий; строки-заголовки внутри годовых файлов отсекаются "
        "фильтром `ид_события <> 'ид_события'`; каналы вне справочника сохраняются "
        "с пустыми метаданными и видны в колонке «без_справочника».",
    ]
    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"справочник каналов: {build_channels():,}")
    build_events(list(range(2019, 2027)))
    report = quality_report()
    (PATHS.reports / "data_quality.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
