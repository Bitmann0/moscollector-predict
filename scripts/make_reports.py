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


def head_a_report() -> str:
    df = experiments.load()
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
    df = experiments.load()
    if df.is_empty():
        return "# Головы A′, B, C, D\n\nЖурнал пуст."
    heads = serve.load_heads()
    sub = df.filter(pl.col("head").is_in(["A_prime", "B", "C", "D"]))
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
