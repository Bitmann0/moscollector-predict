import datetime as dt
import pickle
from pathlib import Path

import polars as pl
import yaml

from . import store
from .config import PATHS

HEADS_CONFIG = PATHS.root / "configs" / "heads.yaml"


def load_heads() -> dict:
    return yaml.safe_load(HEADS_CONFIG.read_text(encoding="utf-8"))


def model_path(head: str) -> Path:
    return PATHS.models / f"{head}.pkl"


def save(head: str, model, iso, feature_names: list[str]) -> Path:
    PATHS.models.mkdir(parents=True, exist_ok=True)
    registry = store.load_registry()
    dst = model_path(head)
    with dst.open("wb") as f:
        pickle.dump({
            "model": model,
            "iso": iso,
            "features": feature_names,
            "feature_set_built_at": {k: v.get("built_at") for k, v in registry.items()},
            "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        }, f)
    return dst


def _apply_budget(df: pl.DataFrame, budget: int) -> pl.DataFrame:
    if df.is_empty():
        return df.with_columns(pl.lit(False).alias("alert"))
    k = min(int(budget), df.height)
    thr = df["risk"].sort(descending=True)[k - 1]
    return df.with_columns((pl.col("risk") >= thr).alias("alert"))


def score(head: str, asof: dt.date | None = None) -> pl.DataFrame:
    """Скоринг одной головы. Фичи берутся только из фичестора,
    построенного той же функцией compute.build_all, что и на обучении."""
    cfg = load_heads()[head]
    with model_path(head).open("rb") as f:
        art = pickle.load(f)

    feats = (store.latest_snapshot(cfg["feature_set"]) if asof is None
             else store.read_slice(cfg["feature_set"], asof, asof))
    missing = [c for c in art["features"] if c not in feats.columns]
    if missing:
        raise ValueError(
            f"фичестор не содержит признаков модели {head}: {missing[:5]}"
        )

    keys = [k for k in cfg["entity"] if k in feats.columns]
    risk = art["model"].predict_proba(feats.select(art["features"]).to_numpy())[:, 1]
    if art["iso"] is not None:
        from .calibrate import apply as cal_apply
        risk = cal_apply(art["iso"], risk)

    out = feats.select(keys).with_columns(pl.Series("risk", risk))
    return _apply_budget(out, cfg["budget_per_day"]).sort("risk", descending=True)


def score_all(asof: dt.date | None = None) -> dict[str, pl.DataFrame]:
    return {h: score(h, asof) for h in load_heads() if model_path(h).exists()}
