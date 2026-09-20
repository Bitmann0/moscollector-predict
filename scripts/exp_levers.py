"""Замер пяти новых семейств признаков по отдельности.

Каждое проверяется своим плечом, а не всё разом: если прирост даст одно, тащить
в прод остальные незачем, а если сработают два разнонаправленно, по общей сумме
этого не увидеть.

Рычаг засчитывается согласием знака по фолдам, а не средним. Приём после
исправления дедупликации детерминирован (проверено двумя проходами, отпечатки
совпали), поэтому весь наблюдаемый разброс — фолдовый, и именно он задаёт планку.
"""
import datetime as dt
import sys

import numpy as np

from mkl import cv, experiments, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

FAMILIES = {
    "CUSUM":         ("cusum_", "dev90_"),
    "EWMA":          ("ewma_",),
    "концентрация":  ("hhi_", "top1_share", "top10_share", "n_channels_80pct"),
    "увлажнение":    ("api_", "precip_7d", "precip_14d", "precip_30d",
                      "snowmelt_7d", "snowmelt_30d"),
    "режим опроса":  ("adi_w90", "cv2_w90"),
}
ALL_NEW = tuple(p for ps in FAMILIES.values() for p in ps)


def _drop(df, prefixes):
    return df.select([c for c in df.columns
                      if not any(c.startswith(p) for p in prefixes)])


def run(head: str, cfg: dict, window_start: dt.date) -> None:
    feats, lab = load(head, cfg, window_start)
    present = {name: ps for name, ps in FAMILIES.items()
               if any(c.startswith(p) for c in feats.columns for p in ps)}
    if not present:
        print(f"{head}: новых признаков в наборе нет — пропуск", flush=True)
        return
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    base_feats = _drop(feats, ALL_NEW)
    print(f"\n{head}  {cfg['title']}  "
          f"(база {base_feats.width} признаков, всего {feats.width})", flush=True)

    def measure(f, label):
        out = train.run(head, f, lab, splits, params=train.params_for(cfg, train.default_backend()),
                        budget_per_day=cfg["budget_per_day"])
        experiments.log({"head": head, "step": "D1", "note": f"рычаг: {label}",
                         "n_features": len(out["feature_names"]), **out["mean"]})
        return out

    ref = measure(base_feats, "база")
    key = "roc_auc"
    ref_folds = [f[key] for f in ref["folds"]]
    print(f"  {'база':<16} {ref['mean'][key]:.4f}  "
          f"P@k={ref['mean']['precision_at_k']:.3f}", flush=True)

    for name, ps in present.items():
        arm = _drop(feats, tuple(p for n, q in FAMILIES.items() if n != name for p in q))
        out = measure(arm, name)
        d = [b - a for a, b in zip(ref_folds, [f[key] for f in out["folds"]])]
        won = sum(v > 0 for v in d)
        verdict = "принят" if won == len(d) else "отклонён"
        print(f"  {'+' + name:<16} {out['mean'][key]:.4f}  "
              f"P@k={out['mean']['precision_at_k']:.3f}  "
              f"дельта " + " ".join(f"{v:+.4f}" for v in d)
              + f"   {won}/{len(d)}  {verdict}", flush=True)

    out = measure(feats, "всё")
    d = [b - a for a, b in zip(ref_folds, [f[key] for f in out["folds"]])]
    won = sum(v > 0 for v in d)
    print(f"  {'всё вместе':<16} {out['mean'][key]:.4f}  "
          f"P@k={out['mean']['precision_at_k']:.3f}  "
          f"дельта " + " ".join(f"{v:+.4f}" for v in d) + f"   {won}/{len(d)}",
          flush=True)


def main() -> None:
    heads = serve.load_heads()
    ws = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or ["C", "A_link", "D", "E", "A_prime"]):
        run(head, heads[head], ws)


if __name__ == "__main__":
    main()
