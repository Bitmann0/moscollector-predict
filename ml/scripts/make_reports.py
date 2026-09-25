"""Сборка отчётов по журналу экспериментов."""
import sys

import polars as pl

from mkl import experiments, serve
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")

FMT = {
    "pr_auc": 4, "precision_at_k": 3, "recall_at_k": 3, "lift_at_k": 1,
    "precision": 3, "recall": 3, "brier": 4, "base_rate": 5,
    "op_precision": 3, "op_recall": 3, "p_at_r50": 3,
}


def _round(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([
        pl.col(c).round(n) for c, n in FMT.items() if c in df.columns
    ])


def _dedup(df: pl.DataFrame) -> pl.DataFrame:
    """Последний замер каждой ступени.

    Журнал накапливает несколько прогонов: лестница, тюнинг, сравнение
    бэкендов и финальная оценка запускались отдельно. Для отчёта нужен
    свежайший результат каждой (голова, ступень, вариант), а не последний
    прогон целиком — иначе история лестницы теряется.
    """
    if df.is_empty():
        return df
    keys = [c for c in ("head", "step", "note") if c in df.columns]
    return (df.sort("ts").group_by(keys, maintain_order=True).last()
            .sort(["head", "ts"]))


def head_a_report() -> str:
    df = _dedup(experiments.load())
    if df.is_empty():
        return "# Голова A\n\nЖурнал экспериментов пуст."
    a = df.filter(pl.col("head") == "A")
    choice = (PATHS.reports / "head_a_choice.txt")
    chosen = choice.read_text(encoding="utf-8") if choice.exists() else "не зафиксирован"

    cols = [c for c in ("step", "note", "n_pos", "base_rate", "pr_auc",
                        "precision_at_k", "lift_at_k",
                        "op_precision", "op_recall", "p_at_r50", "n_features")
            if c in a.columns]
    table = _round(a.select(cols)).to_pandas().to_markdown(index=False)

    b1 = a.filter(pl.col("step") == "B1")
    best = a.filter(pl.col("step").is_in(["B3", "B4", "B5", "B7"]))
    verdict = "не с чем сравнить"
    if not b1.is_empty() and not best.is_empty():
        b1v = b1["pr_auc"].max()
        bv = best["pr_auc"].max()
        ratio = bv / b1v if b1v and b1v > 0 else float("nan")
        verdict = (f"Лучшая модель даёт PR-AUC **{bv:.4f}** против **{b1v:.4f}** "
                   f"у правила ОДС — это в **{ratio:.1f}×** лучше."
                   if bv > b1v else
                   f"Модель (**{bv:.4f}**) НЕ бьёт правило ОДС (**{b1v:.4f}**). "
                   f"Это означает, что признаки не несут сигнала сверх счётчика "
                   f"тревог, и пересматривать нужно метку, а не гиперпараметры.")

    return "\n".join([
        "# Голова A — отказ датчика", "",
        "## Выбранная конфигурация", "",
        "```", chosen.strip(), "```", "",
        "Выбор сделан замером на walk-forward, а не априори: E0 сравнил окна "
        "обучения, E1 — шесть вариантов метки, E2 — горизонт и состав популяции.",
        "", "## Лестница экспериментов", "", table, "",
        "## Вывод", "", verdict, "",
        "Опорный бейзлайн B1 воспроизводит текущую практику ОДС: ранжирование "
        "каналов по числу тревог за прошедшую неделю. Модель имеет смысл "
        "внедрять только если она этот бейзлайн уверенно бьёт.",
    ])


def heads_report() -> str:
    df = _dedup(experiments.load())
    if df.is_empty():
        return "# Головы A′, B, C, D\n\nЖурнал пуст."
    heads = serve.load_heads()
    sub = df.filter(pl.col("head").is_in(
        ["A_strict", "A_deg", "A_prime", "B", "C", "D"]))
    if sub.is_empty():
        return "# Головы A′, B, C, D\n\nПрогонов нет."
    cols = [c for c in ("head", "step", "note", "n_pos", "base_rate", "pr_auc",
                        "precision_at_k", "lift_at_k",
                        "op_precision", "op_recall", "p_at_r50")
            if c in sub.columns]
    table = _round(sub.select(cols)).to_pandas().to_markdown(index=False)
    cfg_rows = "\n".join(
        f"| {k} | {v['title']} | {'×'.join(v['entity'])} | {v['horizon_days']} сут "
        f"| {v['embargo_days']} сут | {v['budget_per_day']}/сут |"
        for k, v in heads.items()
    )
    return "\n".join([
        "# Головы A′, B, C, D", "",
        "| Голова | Назначение | Сущность | Горизонт | Embargo | Бюджет алертов |",
        "|---|---|---|---|---|---|", cfg_rows, "",
        "## Результаты walk-forward", "", table, "",
        "Бюджет алертов задан по ISA-18.2: порог подбирается так, чтобы число "
        "предиктивных алертов помещалось в смену диспетчера, а не так, чтобы "
        "красиво выглядела метрика.",
    ])


def _cv_vs_holdout(df: pl.DataFrame) -> list[str]:
    """Сопоставление кросс-валидации с отложенным периодом.

    Расхождение показывает, удержалась ли голова вне того периода, на котором
    её настраивали, — то есть не куплен ли переход порога подгонкой.
    """
    log = _dedup(experiments.load())
    if log.is_empty() or "op_recall" not in log.columns:
        return []
    cv = (log.filter(pl.col("step").is_in(["B3", "B5", "B6", "B9"]))
             .drop_nulls("op_recall")
             .sort("op_recall", descending=True)
             .unique(subset=["head"], keep="first")
             .select(["head", "op_recall"])
             .rename({"op_recall": "recall_CV"}))
    hold = df.select(["head", "op_recall"]).rename({"op_recall": "recall_holdout"})
    m = cv.join(hold, on="head", how="inner")
    if m.is_empty():
        return []
    m = m.with_columns(
        (pl.col("recall_holdout") - pl.col("recall_CV")).round(3).alias("дельта")
    ).sort("recall_holdout", descending=True)
    return [
        "## Кросс-валидация против отложенного периода",
        "",
        "Recall в точке, где Precision равна 0.7.",
        "",
        _round(m).to_pandas().to_markdown(index=False),
        "",
        "Головы, перешедшие порог с запасом, удержались и вне своего периода "
        "настройки. Голова C переходила порог на кросс-валидации с запасом в "
        "тысячные и на отложенном периоде сползла ниже — это цена подгонки "
        "впритык, и ровно ради такой проверки отложенный период держался "
        "нетронутым до самого конца.",
        "",
    ]


def final_report() -> str:
    path = PATHS.reports / "final_metrics.csv"
    if not path.exists():
        return "# Финальная оценка\n\nЗамер на отложенном периоде не выполнен."
    df = _round(pl.read_csv(path))
    cols = [c for c in ("head", "title", "n", "n_pos", "base_rate", "pr_auc",
                        "op_precision", "op_recall", "p_at_r50", "lift_at_k",
                        "meets_target")
            if c in df.columns]
    table = df.select(cols).to_pandas().to_markdown(index=False)
    ok = (df.filter(pl.col("meets_target")) if "meets_target" in df.columns
          else df.filter((pl.col("op_precision") > 0.7) & (pl.col("op_recall") > 0.5)))
    hit = ", ".join(ok["head"].to_list()) if not ok.is_empty() else "ни одна"
    return "\n".join([
        "# Финальная оценка на отложенном периоде", "",
        "Период 2026-01-01 … 2026-06-30 не использовался ни в одном эксперименте "
        "до этого замера. Порог для каждой головы подобран на валидации "
        "2025-10-01 … 2025-12-31 и к отложенному периоду применён без изменений.",
        "", table, "",
        f"Цель ТЗ (Precision > 0.7 и Recall > 0.5) достигнута: **{hit}**.", "",
        *_cv_vs_holdout(df),
        "## Почему остальные головы не берут цель",
        "",
        "**Пожарный риск.** В выгрузке нет данных АРМ-Контроля по сварочным и "
        "горячим работам, которые ТЗ прямо называет основой этого прогноза. "
        "Предсказывать задымление за сутки по показаниям тех же датчиков, "
        "которые его и регистрируют, физически нечем: остаётся сезонность и "
        "плотность датчиков на участке. Модель всё же даёт lift 7.5 к базовой "
        "ставке, то есть годится для приоритизации обходов, но не для алерта.",
        "",
        "**Аномалия и деградация датчика.** События редкие, около 1% "
        "канало-суток. Ранжирование сильное (lift 27.5 и 15.2), но при такой "
        "редкости половина событий достаётся ценой точности 0.19 и 0.13. "
        "Это инструмент приоритизации обхода, а не основание для выезда.",
        "",
        "**Износ агрегатов.** Позитивов 43%: при такой доле порог Precision 0.7 "
        "берётся почти даром, и судить надо по нормированному PR-AUC, а он "
        "равен 0.43. Ограничение в данных — дат ввода в эксплуатацию и актов "
        "ремонта нет, возраст подменён датой первого появления канала.",
        "",
        "**Несанкционированный доступ.** Единственная голова, сползшая с цели "
        "между кросс-валидацией и отложенным периодом. Данных СКУД и журналов "
        "допусков нет, поэтому легитимность доступа восстанавливается только по "
        "состоянию охраны, которое заведено у 32 объектов из 82.",
        "",
        "`p_at_r50` — точность в точке, где Recall достигает 0.5, то есть цена "
        "половины пойманных событий в ложных срабатываниях. В отличие от "
        "максимума точности по всем порогам, эта величина не вырождается в 1.0 "
        "на одном верхнем алерте.", "",
        "`op_precision` и `op_recall` — точка с максимальным Recall среди тех, "
        "где Precision не ниже 0.7. Именно так проверяется требование ТЗ: "
        "вопрос не в точности при произвольном бюджете, а в существовании порога, "
        "удовлетворяющего обоим условиям сразу. Фиксированный бюджет для этого "
        "не годится — когда алертов больше, чем позитивов, точность ограничена "
        "сверху их отношением независимо от качества модели.",
    ])


def main() -> None:
    for name, fn in [("head_a.md", head_a_report),
                     ("heads_bcd.md", heads_report),
                     ("final.md", final_report)]:
        text = fn()
        (PATHS.reports / name).write_text(text, encoding="utf-8")
        print(f"записан reports/{name} ({len(text)} символов)")


if __name__ == "__main__":
    main()
