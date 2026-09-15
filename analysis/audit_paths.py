"""Source location shared by audit scripts; raw data is intentionally not in Git."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get('MOSCOLLECTOR_DATA_DIR', ROOT / 'data' / 'raw')).resolve()

def source_sql(filename: str) -> str:
    """Quote a source path for a DuckDB SQL string literal."""
    return (DATA_ROOT / filename).as_posix().replace("'", "''")
