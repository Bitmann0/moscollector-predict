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
