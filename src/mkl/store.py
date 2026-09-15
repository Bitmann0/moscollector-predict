import datetime as dt
from pathlib import Path

import duckdb
import polars as pl
import yaml

from .config import PATHS

FEATURE_DIR = PATHS.features
REGISTRY = PATHS.root / "configs" / "features.yaml"

KEY_COLUMNS = frozenset({"ch", "obj", "seg", "day"})


def write(con: duckdb.DuckDBPyConnection, table: str, name: str) -> Path:
    FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    dst = FEATURE_DIR / f"{name}.parquet"
    con.execute(f"COPY {table} TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    described = con.execute(f"DESCRIBE {table}").fetchall()
    cols = [r[0] for r in described]
    n_rows = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]

    reg = load_registry()
    reg[name] = {
        "path": str(dst),
        "keys": sorted(set(cols) & KEY_COLUMNS),
        "columns": [c for c in cols if c not in KEY_COLUMNS],
        "dtypes": {r[0]: r[1] for r in described},
        "n_rows": int(n_rows),
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(
        yaml.safe_dump(reg, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )
    return dst


def load_registry() -> dict:
    if not REGISTRY.exists():
        return {}
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}


def read_slice(name: str, start: dt.date, end: dt.date,
               columns: list[str] | None = None) -> pl.DataFrame:
    lf = pl.scan_parquet(FEATURE_DIR / f"{name}.parquet")
    lf = lf.filter((pl.col("day") >= start) & (pl.col("day") <= end))
    if columns:
        lf = lf.select(columns)
    return lf.collect()


def latest_snapshot(name: str) -> pl.DataFrame:
    lf = pl.scan_parquet(FEATURE_DIR / f"{name}.parquet")
    last_day = lf.select(pl.col("day").max()).collect().item()
    return lf.filter(pl.col("day") == last_day).collect()
