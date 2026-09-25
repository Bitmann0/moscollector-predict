"""Сравнение бэкендов на детерминированном замере.

Прежний вывод «LightGBM, XGBoost и CatBoost в пределах шума» получен ДО
починки недетерминизма, когда один и тот же вход в двух процессах давал
разброс до 14% относительных. То есть вывод «разницы нет» сделан инструментом,
который разницы такого порядка и не различал.

Здесь меряются обе величины сразу: качество и время. Время важно не меньше —
на 4.27 млн строк и 196 признаках обучение занимает минуты, и каждый следующий
эксперимент упирается в него.
"""
import datetime as dt
import sys
import time

from mkl import cv, experiments, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

BACKENDS = ("lgbm", "xgb", "cat")


def run(head: str, cfg: dict, window_start: dt.date) -> None:
    feats, lab = load(head, cfg, window_start)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    print(f"\n{head}  {cfg['title']}  ({feats.height:,} строк, {feats.width} признаков)",
          flush=True)
    print(f"{'бэкенд':<8}{'ROC-AUC':>9}{'PR-AUC':>9}{'P@k':>8}{'время':>9}"
          f"  по фолдам", flush=True)
    for b in BACKENDS:
        t = time.time()
        try:
            # Параметры головы подобраны под LightGBM; для остальных берутся
            # их собственные умолчания, иначе сравнение было бы нечестным.
            out = train.run(head, feats, lab, splits, backend=b,
                            params=cfg.get("params") if b == "lgbm" else None,
                            budget_per_day=cfg["budget_per_day"])
        except Exception as exc:
            print(f"{b:<8} не запустился: {type(exc).__name__}: {exc}", flush=True)
            continue
        el = time.time() - t
        m = out["mean"]
        experiments.log({"head": head, "step": "E1", "note": f"бэкенд {b}",
                         "seconds": el, **m})
        print(f"{b:<8}{m['roc_auc']:>9.4f}{m['pr_auc']:>9.4f}"
              f"{m['precision_at_k']:>8.3f}{el:>8.0f}с  "
              + " ".join(f"{f['roc_auc']:.4f}" for f in out["folds"]), flush=True)


def main() -> None:
    heads = serve.load_heads()
    ws = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or ["C", "A_link"]):
        run(head, heads[head], ws)


if __name__ == "__main__":
    main()
