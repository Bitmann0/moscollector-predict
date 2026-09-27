import datetime as dt

import duckdb

from .config import PATHS


def connect(memory_limit: str = "10GB", threads: int = 8) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET threads={threads}")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{PATHS.tmp}'")
    return con


def attach_events(con: duckdb.DuckDBPyConnection, view: str = "ev") -> None:
    pattern = str(PATHS.interim / "events_year=*.parquet")
    con.execute(
        f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{pattern}')"
    )


def attach_parquet(con: duckdb.DuckDBPyConnection, *names: str) -> None:
    """Подключить таблицы из data/interim как представления по именам файлов."""
    for name in names:
        path = PATHS.interim / f"{name}.parquet"
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{path}')"
        )


def attach_label_sources(con: duckdb.DuckDBPyConnection,
                         source_end: dt.date | None = None) -> None:
    """Подключить daily_channel, episodes и group_outages — входы меток.

    source_end показывает их такими, какими они были к концу суток source_end,
    чтобы метки и выбор порога не видели более поздних дней. Панель режется по
    суткам. Эпизод — по концу, а не по началу: длительность, по которой его
    отбирают метки, известна только после конца. Групповые отказы не
    фильтруются, а пересобираются запросом стадии states по обрезанным
    эпизодам, и флаг is_group пересчитывается по ним же: группа — это счёт
    длинных эпизодов, и эпизод, длина которого выяснилась после source_end, в
    неё входить не может. Всё остаётся представлениями, поэтому голова, чья
    метка эпизодов не читает (A_link), за обрезку не платит.
    """
    if source_end is None:
        attach_parquet(con, "daily_channel", "episodes", "group_outages")
        return
    from . import states

    end = source_end.isoformat()
    con.execute(
        "CREATE OR REPLACE VIEW daily_channel AS SELECT * FROM read_parquet("
        f"'{PATHS.interim / 'daily_channel.parquet'}') WHERE day <= DATE '{end}'")
    con.execute(
        "CREATE OR REPLACE VIEW _episodes_cut AS SELECT * FROM read_parquet("
        f"'{PATHS.interim / 'episodes.parquet'}') "
        f"WHERE CAST(t_end AS DATE) <= DATE '{end}'")
    con.execute("CREATE OR REPLACE VIEW group_outages AS "
                + states.group_outages_sql("_episodes_cut"))
    con.execute("CREATE OR REPLACE VIEW episodes AS "
                + states.marked_episodes_sql("_episodes_cut", "group_outages"))
