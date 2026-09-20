"""Даёт ли что-нибудь иерархия объектов, открывшаяся в обновлённом справочнике.

Плечи разведены: вид объекта (охранная зона против диспетчерской) и контекст
комплекса — разные признаки, и если прирост даст только один, тащить в прод
второй незачем.
"""
import datetime as dt
import sys

from mkl import config, cv, experiments, serve, train
from verify_head import TEST_DAYS, load, _choice

sys.stdout.reconfigure(encoding="utf-8")

ARMS = {
    "база":      ("par_", "is_guard_object"),
    "+вид":      ("par_",),
    "+комплекс": (),
}


def main():
    heads = serve.load_heads()
    window_start = dt.date.fromisoformat(_choice()["window_start"])
    for head in (sys.argv[1:] or ["A_prime", "C", "E"]):
        cfg = heads[head]
        feats, lab = load(head, cfg, window_start)
        if lab.is_empty() or lab["y"].sum() == 0:
            print(f"{head}: позитивов нет — пропуск", flush=True)
            continue
        days = sorted(lab["day"].unique().to_list())
        splits = cv.walk_forward(days, n_splits=3, test_days=TEST_DAYS,
                                 embargo_days=cfg["embargo_days"])
        print(f"\n{head}  {cfg['title']}", flush=True)
        for arm, drop in ARMS.items():
            f = feats.select([c for c in feats.columns
                              if not any(c.startswith(p) for p in drop)])
            out = train.run(head, f, lab, splits, params=cfg.get("params"),
                            budget_per_day=cfg["budget_per_day"])
            m = out["mean"]
            experiments.log({"head": head, "step": "C1", "note": f"иерархия: {arm}",
                             "n_features": len(out["feature_names"]), **m})
            print(f"  {arm:10} {f.width:>3} призн  норм={m.get('pr_auc_norm', float('nan')):.4f}"
                  f"  P@R50={m.get('p_at_r50', float('nan')):.3f}"
                  f"  maxP={m.get('op_precision', float('nan')):.3f}"
                  f"@R={m.get('op_recall', float('nan')):.3f}", flush=True)
            if arm == "+комплекс" and out["model"] is not None:
                top = train.importance(out["model"], out["feature_names"], top=10)
                par = [f_ for f_ in top["feature"]
                       if f_.startswith("par_") or f_ == "is_guard_object"]
                print(f"   из топ-10 иерархических: {par or '—'}", flush=True)


if __name__ == "__main__":
    main()
