"""Подбор гиперпараметров на нынешней постановке.

Прежние подбирались до аудита: голова A обучалась на метке, которая на 98.89%
состояла из молчания, голова C — на определении, считавшем позитивом проход
персонала. Параметры, оптимальные для тех задач, для нынешних оптимальными быть
не обязаны, и держать их без пересмотра значит верить, что оптимум не сдвинулся.

Подбор — по точности на бюджете, потому что именно она идёт в отчёт. Отбирается
конфигурация, выигравшая у нынешней согласием знака по фолдам, а не средним.
"""
import datetime as dt
import itertools
import sys

from mkl import cv, experiments, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

# Сетка держится маленькой намеренно: при трёх фолдах и разбросе в доли процента
# широкая сетка выберет шум. Меняется по одному свойству от текущего значения.
GRID = {
    "max_depth": (5, 7, 10),
    "learning_rate": (0.01, 0.02, 0.05),
    "n_estimators": (400, 800, 1600),
    "min_child_weight": (5, 20, 80),
    "subsample": (0.6, 0.8, 1.0),
    "colsample_bytree": (0.6, 0.8, 1.0),
}


def run(head: str, cfg: dict) -> None:
    feats, lab = load(head, cfg, dt.date.fromisoformat(_choice()["window_start"]))
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                             embargo_days=cfg["embargo_days"])
    base_params = dict(train.params_for(cfg, train.default_backend()) or {})

    def measure(params, tag):
        out = train.run(head, feats, lab, splits, params=params,
                        budget_per_day=cfg["budget_per_day"])
        experiments.log({"head": head, "step": "F3", "note": f"тюнинг: {tag}",
                         **out["mean"]})
        return out

    print(f"\n{head}  {cfg['title']}", flush=True)
    ref = measure(base_params, "нынешние")
    rp = [f["precision_at_k"] for f in ref["folds"]]
    print(f"  {'нынешние':<34} P@k={ref['mean']['precision_at_k']:.4f}", flush=True)

    best, best_gain = None, 0.0
    for key, values in GRID.items():
        for v in values:
            if base_params.get(key) == v:
                continue
            params = {**base_params, key: v}
            out = measure(params, f"{key}={v}")
            d = [f["precision_at_k"] - a for a, f in zip(rp, out["folds"])]
            won = sum(x > 0 for x in d)
            mark = "принят" if won == len(d) else ""
            print(f"  {key + '=' + str(v):<34} "
                  f"P@k={out['mean']['precision_at_k']:.4f}  "
                  + " ".join(f"{x:+.3f}" for x in d) + f"  {won}/{len(d)}  {mark}",
                  flush=True)
            if won == len(d) and sum(d) > best_gain:
                best, best_gain = (key, v), sum(d)
    if best:
        print(f"  ЛУЧШЕЕ: {best[0]}={best[1]}, суммарная дельта {best_gain:+.4f}",
              flush=True)
    else:
        print("  ни одно изменение не выиграло во всех фолдах — оставляем как есть",
              flush=True)


def main() -> None:
    heads = serve.load_heads()
    for head in (sys.argv[1:] or ["C"]):
        run(head, heads[head])


if __name__ == "__main__":
    main()
