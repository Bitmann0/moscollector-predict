import datetime as dt
import json
import subprocess

import polars as pl

from .config import PATHS

LOG_PATH = PATHS.experiments / "log.jsonl"

# Один идентификатор на процесс: без него отчёт смешивает записи разных
# прогонов, а журнал дописывается, а не перезаписывается.
RUN_ID = dt.datetime.now().strftime("%Y%m%dT%H%M%S")


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PATHS.root, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def log(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "run_id": RUN_ID,
        "git_sha": _git_sha(),
        **record,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def load() -> pl.DataFrame:
    if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
        return pl.DataFrame()
    return pl.read_ndjson(LOG_PATH)


def best(head: str, metric: str = "pr_auc") -> dict:
    df = load()
    if df.is_empty() or metric not in df.columns:
        return {}
    sub = df.filter(pl.col("head") == head).drop_nulls(metric)
    if sub.is_empty():
        return {}
    return sub.sort(metric, descending=True).head(1).to_dicts()[0]


def latest_run(df: pl.DataFrame | None = None) -> pl.DataFrame:
    """Записи только последнего прогона: журнал дописывается, а не очищается."""
    df = load() if df is None else df
    if df.is_empty() or "run_id" not in df.columns:
        return df
    return df.filter(pl.col("run_id") == df["run_id"].max())


def table(head: str | None = None,
          columns: tuple[str, ...] = ("step", "note", "n_pos", "base_rate",
                                      "pr_auc", "pr_auc_norm", "precision_at_k",
                                      "lift_at_k", "p_at_r50"),
          only_latest: bool = True) -> pl.DataFrame:
    df = latest_run() if only_latest else load()
    if df.is_empty():
        return df
    if head:
        df = df.filter(pl.col("head") == head)
    keep = [c for c in columns if c in df.columns]
    return df.select(keep)
