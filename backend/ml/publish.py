"""Create a compact, shareable experiment report without exporting raw sensor records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def publish(run_dir: Path, data_dir: Path, output: Path) -> None:
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "sources.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    test = report["test"]
    lines = [
        "# Первый backtest: технические сигналы насосов",
        "",
        "## Результат",
        "",
        (f"Precision **{test['precision']:.3f}**, Recall **{test['recall']:.3f}**. "
        "Цели Precision > 0,7 и Recall > 0,5 пока не достигнуты."),
        "",
        ("Цель: хотя бы одна запись «Неисправен» в следующие календарные сутки после суток "
        "без такой записи. Единица оценки — наблюдаемый канал-день. Это метрики сигнала; "
        "подтверждённых физических отказов в выборке нет."),
        "",
        "## Протокол",
        "",
        "- Обучение: 2024; выбор модели: январь–сентябрь 2025; выбор порога: октябрь–декабрь 2025.",
        "- Финальный тест: январь–июнь 2026. Его исходы не участвуют в выборе модели и порога.",
        "- Окна признаков: предыдущие 1 и 7 суток. Целевое окно: следующие 24 часа.",
        "- Граница t — полночь Europe/Moscow (предположение); признаки [t−7d,t), цель [t,t+24h).",
        "- Цели, пересекающие границу следующего периода, исключаются.",
        "- Вчерашняя запись «Неисправен» исключает точку из прогнозирования.",
        "- Дни с неполным глобальным покрытием и отсутствием будущих наблюдений канала исключаются.",
        "- Отдельный августовский пример не смешивается с годовой историей.",
        "",
        (f"Выбрана модель `{report['selected_model']}`; порог {report['threshold']:.6f}. "
        "На периоде выбора порога ограничение Precision > 0,7 при минимум 10 уведомлениях "
        "не выполнено; использован заранее заданный запасной критерий F0.5. "
        "Веб-сервис автоматически на эту модель не переключается."),
        "",
        "## Сравнение на одном тесте",
        "",
        "| Метод | Precision | Recall | Average Precision | Brier |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, key in [
        ("Бустинг", "test"),
        ("Постоянная частота", "constant_baseline_test"),
        ("Был сигнал за 7 суток", "recent_fault_rule_test"),
    ]:
        m = report[key]
        lines.append(
            f"| {name} | {m['precision']:.3f} | {m['recall']:.3f} | "
            f"{m['average_precision']:.3f} | {m['brier_score']:.3f} |"
        )
    lines.extend(
        [
            "",
            ("Постоянная частота оценена на train; при выбранном пороге она не выдаёт "
            "уведомлений. Правило за 7 суток бинарное; его Brier не оценивает калибровку "
            "вероятности. Выходы бустинга также ещё не откалиброваны."),
            "",
            "## Результаты по месяцам",
            "",
            "| Месяц | Точек | Положительных | Уведомлений | Precision | Recall |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for month, m in report["test_by_month"].items():
        lines.append(
            f"| {month} | {m['samples']} | {m['positives']} | {m['alerts']} | "
            f"{m['precision']:.3f} | {m['recall']:.3f} |"
        )
    matrix, quality = test["confusion_matrix"], report["quality"]
    lines.extend(
        [
            "",
            f"TP={matrix['tp']}, FP={matrix['fp']}, FN={matrix['fn']}, TN={matrix['tn']}.",
            "",
            "## Покрытие и ограничения",
            "",
            (f"Из {quality['channel_days']} календарных канал-дней допустимы по истории "
            f"{quality['eligible_at_prediction']}; ещё {quality['unknown_future']} имеют "
            f"неизвестный будущий исход. Оценено {quality['evaluated_samples']} точек во всех периодах."),
            "",
            ("Метрики условны на этом покрытии: они не распространяются на молчащие каналы. "
            "Наличие событий во всех 24 часовых корзинах не доказывает полноту выгрузки. "
            "Совместные «Неисправен» и «Норма» не упорядочиваются искусственно. "
            "Канал-дни не равны независимым эпизодам физических отказов. "
            "Оценка 24-часового окна не доказывает предупреждение минимум за 24 часа."),
            "",
            (f"Измеренное предсказание {test['samples']} тестовых точек: "
            f"{report['test_inference_seconds']:.4f} с (только inference готовых признаков, "
            "не полный потоковый SLA)."),
            "",
            "## Следующий эксперимент",
            "",
            ("Уточнить семантику совместных состояний и получить подтверждённые исходы осмотров. "
            "Исследовать деградацию качества на late-2025 validation, более свежий train и "
            "часовые признаки с rolling backtest. После просмотра этого теста 2026 его нельзя "
            "повторно называть нетронутым при подборе новых гиперпараметров. Для окончательной "
            "проверки следующей версии нужен новый holdout или явно обозначенная повторная оценка."),
            "",
            ("Полные числа и версии: [report.json](report.json). "
            "Источники и SHA-256: [sources.json](sources.json)."),
            "",
        ]
    )
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/models/pump-signal-v1"))
    parser.add_argument("--data-dir", type=Path, default=Path("data/processed/pumps"))
    parser.add_argument("--output", type=Path, default=Path("docs/experiments/pump-signal-v1"))
    args = parser.parse_args()
    publish(args.run_dir, args.data_dir, args.output)


if __name__ == "__main__":
    main()
