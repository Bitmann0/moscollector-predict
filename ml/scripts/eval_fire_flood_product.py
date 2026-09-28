"""Проверка пожарной (B) и паводковой (E) голов как очередей продукта.

Протокол временной. К каждому проверяемому месяцу M модель обучается
train_latest.refresh по данным на конец суток перед M: обучение, калибровка и
окно порога кончаются раньше M. Весь месяц считается этой моделью, то есть
задержка модели доходит до 31 суток, а в продукте она не больше 8
(max_model_lag_days); оценка поэтому не завышена свежестью модели.

Выдача — как в сервисе: top-k за сутки по риску, порог артефакта, пауза 7 суток
по объекту из собственной выдачи (serve._apply_budget и
serve.apply_issued_cooldown). Освободившееся место не добирается.

Исход рекомендации (obj[, seg], d) — по суткам d+1, как в mkl.outcomes:
hit — событие метки (n_fire > 0 на участке у B, n_flood > 0 на объекте у E);
miss — сущность прислала данные в d+1, события нет; unknown — данных за d+1 нет
или d+1 за концом панели. Нижняя граница точности считает unknown промахом.

Полнота — попадания / положительные строки метки (labels.build_for_head по
полной панели) за период; база — доля положительных среди строк метки.

Для сравнения в тех же условиях — правило без модели: B ранжирует участки по
числу тревог за 7 суток (n_alarms_w7), E — объекты по числу состояний
«Затоплен» за сутки расчёта (n_flood).

    python scripts/eval_fire_flood_product.py --output reports/fire_flood_product.json
"""
import argparse
import datetime as dt
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

import polars as pl

from mkl import calibrate, config, db, labels, serve, store, train
from mkl.config import PATHS, seg_sql

sys.stdout.reconfigure(encoding="utf-8")

RULES = {"B": "n_alarms_w7", "E": "n_flood"}
EVENT = {"B": "n_fire", "E": "n_flood"}
BUDGETS = {"B": (3, 5, 10), "E": (2, 3, 5)}
COOLDOWN_DAYS = 7


def _train_latest():
    path = Path(__file__).with_name("train_latest.py")
    spec = importlib.util.spec_from_file_location("train_latest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _months(first: dt.date, last: dt.date) -> list[tuple[dt.date, dt.date]]:
    out = []
    start = first.replace(day=1)
    while start <= last:
        nxt = (start + dt.timedelta(days=32)).replace(day=1)
        out.append((start, min(nxt - dt.timedelta(days=1), last)))
        start = nxt
    return out


def population(head: str, feats: pl.DataFrame) -> pl.DataFrame:
    """Те же сущности, что в serve.score."""
    return serve.restrict_population(head, serve.load_heads()[head], feats)


def model_risk(art: dict, feats: pl.DataFrame) -> pl.Series:
    X = train._matrix(feats, art["features"])
    p = art["model"].predict_proba(X)[:, 1]
    if art["iso"] is not None:
        p = calibrate.apply(art["iso"], p)
    return pl.Series("risk", p)


def issue(df: pl.DataFrame, budget: int, keys: list[str],
          thresholds: dict[dt.date, float | None]) -> pl.DataFrame:
    """Суточная выдача с порогом дня и паузой по объекту из своей выдачи."""
    issued: list[tuple[object, dt.date]] = []
    out = []
    for day in sorted(df["day"].unique().to_list()):
        cur = df.filter(pl.col("day") == day).select([*keys, "risk"])
        ranked = serve._apply_budget(cur, budget)
        thr = thresholds.get(day)
        if thr is not None:
            ranked = ranked.with_columns(
                (pl.col("alert") & (pl.col("risk") >= thr)).alias("alert"))
        ranked = serve.apply_issued_cooldown(ranked, "obj", day, issued,
                                             cooldown_days=COOLDOWN_DAYS)
        chosen = ranked.filter(pl.col("alert"))
        issued.extend((obj, day) for obj in chosen["obj"].to_list())
        out.append(chosen.select(keys))
    return pl.concat(out) if out else pl.DataFrame()


def facts(head: str, first: dt.date, last: dt.date) -> tuple[pl.DataFrame, dt.date]:
    """(сущность, сутки) с данными и флаг события, из daily_channel."""
    con = db.connect()
    db.attach_label_sources(con, None)
    seg = f", {seg_sql('picket')} AS seg" if head == "B" else ""
    group = "obj, seg, day" if head == "B" else "obj, day"
    frame = con.execute(f"""
      SELECT obj{seg}, day, max(coalesce({EVENT[head]}, 0)) > 0 AS event
      FROM daily_channel WHERE obj IS NOT NULL AND day BETWEEN ? AND ?
      GROUP BY {group}""", [first, last + dt.timedelta(days=1)]).pl()
    panel_last = con.execute("SELECT max(day) FROM daily_channel").fetchone()[0]
    con.close()
    return frame, panel_last


def label_rows(head: str, first: dt.date, last: dt.date) -> pl.DataFrame:
    con = db.connect()
    db.attach_label_sources(con, None)
    cfg = serve.load_heads()[head]
    table = labels.build_for_head(con, cfg)
    lab = con.execute(f"SELECT * FROM {table} WHERE day BETWEEN ? AND ?",
                      [first, last]).pl()
    con.close()
    return lab


def score_outcomes(chosen: pl.DataFrame, keys: list[str], fact: pl.DataFrame,
                   panel_last: dt.date) -> dict:
    if chosen.is_empty():
        return {"recommendations": 0, "hits": 0, "new_hits": 0, "unknown": 0}
    ent = [k for k in keys if k != "day"]
    target = chosen.with_columns((pl.col("day") + pl.duration(days=1)).alias("t"))
    joined = (target.join(fact.rename({"day": "t"}), on=[*ent, "t"], how="left")
              .join(fact.rename({"event": "event_today"}), on=[*ent, "day"], how="left"))
    hit = pl.col("event").fill_null(False)
    hits = int(joined.select(hit.sum()).item())
    # Новое событие: в сутки расчёта его у сущности не было. Остальные попадания —
    # продолжение сегодняшнего, их ловит и правило «было сегодня — будет завтра».
    new = int(joined.select((hit & ~pl.col("event_today").fill_null(False)).sum()).item())
    unknown = int(joined.filter(pl.col("event").is_null() |
                                (pl.col("t") > panel_last)).height)
    return {"recommendations": chosen.height, "hits": hits, "new_hits": new,
            "unknown": unknown}


def summarize(block: dict, positives: int, days: int, positives_new: int = 0) -> dict:
    n, hits, unknown = block["recommendations"], block["hits"], block["unknown"]
    known = n - unknown
    return {**block,
            "precision_lower": round(hits / n, 4) if n else None,
            "precision_known": round(hits / known, 4) if known else None,
            "recall": round(hits / positives, 4) if positives else None,
            "recall_new": (round(block["new_hits"] / positives_new, 4)
                           if positives_new else None),
            "per_day": round(n / days, 2) if days else None}


def run_head(head: str, first: dt.date, last: dt.date, tl) -> dict:
    cfg = serve.load_heads()[head]
    keys = [k for k in cfg["entity"]]
    fact, panel_last = facts(head, first, last)
    lab = label_rows(head, first, last)
    days = (last - first).days + 1
    months = []
    frames = []
    thresholds: dict[dt.date, float] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for m_first, m_last in _months(first, last):
            source_end = m_first - dt.timedelta(days=1)
            result = tl.refresh(head, cfg, source_end=source_end, out=Path(tmp))
            art = serve.load_artifact(Path(result["artifact"]))
            feats = population(head, store.read_slice(cfg["feature_set"], m_first, m_last))
            risk = model_risk(art, feats)
            frame = feats.select([*keys, RULES[head]]).with_columns(risk)
            frames.append(frame)
            for d in frame["day"].unique().to_list():
                thresholds[d] = art["threshold"]
            months.append({"month": m_first.isoformat()[:7],
                           "source_end": str(source_end),
                           "threshold_end": result["threshold_end"],
                           "threshold": art["threshold"],
                           "threshold_feasible": result["threshold_feasible"]})
            print(f"{head} {m_first:%Y-%m}: порог {art['threshold']:.4f}", flush=True)
    scored = pl.concat(frames)
    positives = int(lab["y"].sum())
    ent = [k for k in keys if k != "day"]
    today = lab.join(fact.rename({"event": "event_today"}), on=[*ent, "day"], how="left")
    positives_new = int(today.filter((pl.col("y") == 1) &
                                     ~pl.col("event_today").fill_null(False)).height)
    out = {"head": head, "period": [str(first), str(last)], "days": days,
           "label_rows": lab.height, "positives": positives,
           "base_rate": round(positives / lab.height, 4) if lab.height else None,
           "positives_new": positives_new,
           "panel_last_day": str(panel_last), "months": months,
           "budgets": {}}
    for budget in BUDGETS[head]:
        model = issue(scored, budget, keys, thresholds)
        rule = issue(scored.with_columns(pl.col(RULES[head]).cast(pl.Float64)
                                         .fill_null(0.0).alias("risk")),
                     budget, keys, {})
        out["budgets"][str(budget)] = {
            "model": summarize(score_outcomes(model, keys, fact, panel_last),
                               positives, days, positives_new),
            "rule": summarize(score_outcomes(rule, keys, fact, panel_last),
                              positives, days, positives_new),
            "model_by_month": {
                m["month"]: summarize(score_outcomes(
                    model.filter(pl.col("day").dt.strftime("%Y-%m") == m["month"]),
                    keys, fact, panel_last), int(lab.filter(
                        pl.col("day").dt.strftime("%Y-%m") == m["month"])["y"].sum()),
                    0)
                for m in months}}
    return out


def main() -> None:
    global COOLDOWN_DAYS
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("heads", nargs="*", default=["B", "E"])
    parser.add_argument("--first", type=dt.date.fromisoformat, default=dt.date(2025, 7, 1))
    parser.add_argument("--last", type=dt.date.fromisoformat, default=dt.date(2026, 6, 29))
    parser.add_argument("--output", type=Path, default=PATHS.reports / "fire_flood_product.json")
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_DAYS,
                        help="пауза по объекту в сутках; 0 — без паузы")
    args = parser.parse_args()
    COOLDOWN_DAYS = args.cooldown
    tl = _train_latest()
    report = {"protocol": "monthly_refresh_temporal", "cooldown_days": COOLDOWN_DAYS,
              "rules": RULES, "generated_by": "scripts/eval_fire_flood_product.py",
              "heads": {}}
    for head in args.heads:
        report["heads"][head] = run_head(head, args.first, args.last, tl)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    print(json.dumps({h: {b: v["model"] | {"rule_precision_lower": v["rule"]["precision_lower"]}
                          for b, v in r["budgets"].items()}
                      for h, r in report["heads"].items()}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
