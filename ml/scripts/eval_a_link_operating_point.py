"""A_link: рабочая точка 0.50 против 0.70 на июне 2026, вне выборки.

Датированные модели (train_latest.py --source-end) выбирают порог на 30 днях
до своего конца окна, поэтому каждый день 02.06–29.06 оценивается моделью,
которая этот день не видела. Модели 0.50 и 0.70 обучаются одинаково, разница
только в operating_min_precision в heads.yaml, поэтому их кладут в два
разных MKL_ROOT с общими data/.

Выдача — как в сервисе: не больше 20 каналов в сутки выше сохранённого
порога, пауза 7 суток без добора. Unknown занимают место и входят в
знаменатель нижней границы точности.

Шаги (из ml/, PYTHONPATH=src):
  MKL_ROOT=<root 0.50> python scripts/eval_a_link_operating_point.py score a050.parquet
  MKL_ROOT=<root 0.70> python scripts/eval_a_link_operating_point.py score a070.parquet
  python scripts/eval_a_link_operating_point.py labels labels.parquet
  python scripts/eval_a_link_operating_point.py compare a050.parquet a070.parquet labels.parquet
"""
import datetime as dt
import json
import sys

import polars as pl

DAYS = [dt.date(2026, 6, 2) + dt.timedelta(days=i) for i in range(28)]  # 02.06–29.06
BUDGET, COOLDOWN = 20, 7


def score(out: str) -> None:
    from mkl import serve
    cfg = serve.load_heads()["A_link"]
    rows = []
    for d in DAYS:
        path = serve.artifact_path("A_link", d, cfg)
        thr = serve.load_artifact(path).get("threshold")
        df = serve.score("A_link", d)
        cand = df.filter(pl.col("risk") >= thr) if thr is not None else df
        rows.append(cand.select("ch", "day", "risk").with_columns(
            pl.lit(path.name).alias("artifact"), pl.lit(thr).alias("threshold")))
        print(d, path.name, round(thr, 3), cand.height, flush=True)
    pl.concat(rows).write_parquet(out)


def labels(out: str) -> None:
    from mkl import db, labels as lab, serve
    cfg = serve.load_heads()["A_link"]
    con = db.connect()
    db.attach_parquet(con, "daily_channel", "episodes", "group_outages")
    lab.build_for_head(con, cfg)
    con.execute("SELECT ch, day, y FROM label_link WHERE day BETWEEN ? AND ?",
                [DAYS[0], DAYS[-1]]).pl().write_parquet(out)


def compare(a: str, b: str, lab_path: str, out: str | None = None) -> None:
    from mkl import serve
    lab = pl.read_parquet(lab_path)
    positives = lab.filter(pl.col("y") == 1).height
    result = {"days": [str(DAYS[0]), str(DAYS[-1])], "budget_per_day": BUDGET,
              "cooldown_days": COOLDOWN, "positives_in_period": positives,
              "policies": {}}
    for name, path in (("min_precision_0.50", a), ("min_precision_0.70", b)):
        cand = pl.read_parquet(path)
        thresholds = (cand.group_by("artifact").agg(pl.col("threshold").first())
                      .sort("artifact"))
        served = serve.alerts_over_time(cand.join(lab, on=["ch", "day"], how="left"),
                                        BUDGET, entity="ch", cooldown_days=COOLDOWN)
        chosen = served.filter(pl.col("alert"))
        hits = chosen.filter(pl.col("y") == 1).height
        unknown = chosen["y"].null_count()
        result["policies"][name] = {
            "thresholds": dict(zip(thresholds["artifact"], thresholds["threshold"])),
            "alerts": chosen.height, "hits": hits, "unknown": unknown,
            "precision_lower_bound": round(hits / chosen.height, 4) if chosen.height else None,
            "precision_known_only": round(hits / (chosen.height - unknown), 4)
            if chosen.height > unknown else None,
            "recall_known": round(hits / positives, 4) if positives else None,
            "alerts_per_day_mean": round(chosen.height / len(DAYS), 2),
            "days_without_alerts": len(DAYS) - chosen["day"].n_unique(),
        }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        open(out, "w", encoding="utf-8").write(text + "\n")
    print(text)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd, *rest = sys.argv[1:]
    {"score": score, "labels": labels, "compare": compare}[cmd](*rest)
