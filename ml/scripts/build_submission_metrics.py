"""Реестр метрик для сдачи: reports/SUBMISSION_METRICS.json из машинных отчётов.

Числа не переписываются руками. Скрипт читает JSON и CSV из reports/ и замер
времени ML из ../docs/submission/perf/, считает суммы по окнам и собирает один файл, где у каждого блока есть source (файл,
из которого взято число) и command (как пересчитать сам источник из ml/).
Константы ниже — числа, которых нет в машинных отчётах репозитория; у каждой
записано, откуда она и почему файла нет.

    python scripts/build_submission_metrics.py          # записать JSON
    python scripts/build_submission_metrics.py --check  # код 1, если JSON устарел

Читаемая версия — reports/SUBMISSION_METRICS.md. Блок quality_screen копируется
в contracts/quality_reference.json (экран «Качество прогноза»); совпадение
проверяет tests/test_quality_reference.py в корне репозитория.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
OUT = REPORTS / "SUBMISSION_METRICS.json"
FROZEN = "2026-09-28"

TITLES = {
    "sensor_link": "Отказ датчика: риск потери связи",
    "equipment_diag": "Износ: плановая диагностика оборудования",
    "guard_weekly": "НСД: проверка объектов с хроническими охранными тревогами",
    "fire_risk": "Пожар: риск пожарной или газовой тревоги на участке",
    "flood_risk": "Подтопление: риск затопления насосной на объекте",
}

# --- Числа без машинного отчёта в репозитории --------------------------------
# Размеченные канало-сутки A_link за 02.06–29.06.2026: выход шага labels того же
# скрипта (таблица label_link). Parquet с данными в git не кладётся. Посчитано PM
# 28.09; пересчитано 28.09 по тому же файлу: 36 816 нулей, 5 832 единицы.
JUNE_LABELED_ROWS = 42_648
JUNE_LABELS_SOURCE = ("выход шага labels scripts/eval_a_link_operating_point.py "
                      "(label_link за 02.06–29.06.2026); файл данных в git не кладётся")
# Замер ML2-04: сырой вывод scripts/measure_ml.py лежит вне ml/, в документации сдачи.
RUNTIME_FILE = ROOT.parent / "docs" / "submission" / "perf" / "ml_score_june.jsonl"
RUNTIME_CONDITIONS = ("28.09, машина разработчика: Windows 11, Docker Desktop на WSL2, "
                      "ML_MODE=real, бандл bundle-20260928-3 (модели A_link 0,70); запросы "
                      "по одному из контейнера api")
RUNTIME_COMMAND = ("docker compose -f compose.yaml -f compose.real.yaml exec -T api "
                   "python - < scripts/measure_ml.py")
GAS_PLANNED = {
    "records_in_window": 19_307, "records_total": 21_784,
    "source": "план команды docs/superpowers/plans/2026-09-25-team-plan-to-submission.md, "
              "раздел 4, C5; подтверждение заказчика — сообщение [462] в чате. "
              "Файла с расчётом в main нет. Подсказка реализована в "
              "backend/app/services/semantics.py",
}

CMD_JUNE = ("из ml/, PYTHONPATH=src: датированные модели train_latest.py A_link "
            "--train-window в двух MKL_ROOT (operating_min_precision 0.50 и 0.70), затем "
            "MKL_ROOT=<корень 0.50> python scripts/eval_a_link_operating_point.py score a050.parquet; "
            "MKL_ROOT=<корень 0.70> python scripts/eval_a_link_operating_point.py score a070.parquet; "
            "python scripts/eval_a_link_operating_point.py labels labels.parquet; "
            "python scripts/eval_a_link_operating_point.py compare a050.parquet a070.parquet "
            "labels.parquet reports/a_link_operating_point_june.json")
CMD_JUNE_LABELS = "python scripts/eval_a_link_operating_point.py labels labels.parquet"
CMD_A_LINK_5 = "python scripts/eval_a_link_policy.py --end-date 2026-06-29"
CMD_VALIDATION = ("python scripts/audit_second_ml.py --mode historical --data-root <data> "
                  "--splits 4 --test-days 28 --end 2025-12-31 --threads 12 "
                  "--output reports/rule_vs_model_2025h2.json")
CMD_42D = ("python scripts/audit_second_ml.py --mode historical --data-root <data> "
           "--splits 3 --test-days 14 --refresh-days 7 --threads 4 "
           "--output reports/second_ml_42d_local.json")
CMD_GUARD = ("python scripts/build_intrusion_eventtime_labels.py; "
             "python scripts/exp_guard_weekly_repeats.py")
CMD_GUARD_MODEL = "python scripts/exp_guard_weekly_model.py"
CMD_FIRE_FLOOD = ("MKL_ROOT=<корень с данными> python scripts/eval_fire_flood_product.py "
                  "--output reports/fire_flood_product.json")
CMD_FIRE_FLOOD_CD7 = ("MKL_ROOT=<корень с данными> python scripts/eval_fire_flood_product.py "
                      "--cooldown 7 --output reports/fire_flood_product_cooldown7.json")
FIRE_FLOOD_SOURCE = "reports/fire_flood_product.json"
FIRE_FLOOD_CD7_SOURCE = "reports/fire_flood_product_cooldown7.json"


def _load(name: str) -> dict:
    return json.loads((REPORTS / name).read_text(encoding="utf-8"))


def _ratio(num: float, den: float, digits: int = 4) -> float | None:
    return round(num / den, digits) if den else None


def _days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1


def _counts(alerts: int, hits: int, unknown: int) -> dict:
    return {"alerts": alerts, "hits": hits, "unknown": unknown,
            "precision_lower_bound": _ratio(hits, alerts),
            "precision_known_only": _ratio(hits, alerts - unknown)}


# --- sensor_link ---------------------------------------------------------------

def _a_link_pool(folds: list[dict], policy: str, side: str) -> dict:
    parts = [f["policies"][policy][side] for f in folds]
    alerts = sum(p["alerts"] for p in parts)
    hits = sum(p["hits"] for p in parts)
    unknown = sum(p["unknown_alerts"] for p in parts)
    return _counts(alerts, hits, unknown)


def _a_link_positives(fold: dict) -> int:
    """Положительные канало-сутки окна: hits / recall_known, одинаково во всех политиках."""
    values = {round(p[side]["hits"] / p[side]["recall_known"])
              for p in fold["policies"].values() for side in ("model", "baseline")
              if p[side]["hits"]}
    if len(values) != 1:
        raise ValueError(f"разные знаменатели полноты в окне {fold['test_start']}: {values}")
    return values.pop()


def sensor_link() -> dict:
    june = _load("a_link_operating_point_june.json")
    p70 = june["policies"]["min_precision_0.70"]
    june_days = _days(*june["days"])
    temporal = _load("a_link_live_policy_temporal.json")
    folds = temporal["folds"]
    positives = sum(_a_link_positives(f) for f in folds)
    known = sum(f["known_rows"] for f in folds)
    unknown_rows = sum(f["unknown_rows"] for f in folds)
    days = sum(_days(f["test_start"], f["test_end"]) for f in folds)
    period = [folds[0]["test_start"], folds[-1]["test_end"]]
    model70 = _a_link_pool(folds, "min_precision_0.70", "model")
    base_top20 = _a_link_pool(folds, "fixed_top_20", "baseline")
    model_top20 = _a_link_pool(folds, "fixed_top_20", "model")
    base70 = _a_link_pool(folds, "min_precision_0.70", "baseline")
    feasible = sum(f["policies"]["min_precision_0.70"]["baseline_selection"]["feasible"]
                   for f in folds)
    return {
        "code": "sensor_link", "title": TITLES["sensor_link"], "head": "A_link",
        "in_product": "модель LightGBM, порог выбирается на прошлом 30-дневном окне "
                      "при минимуме точности 0,70 (operating_min_precision)",
        "target": "начало необычного пропуска связи канала завтра",
        "label": "L9c: канал активен не меньше 7 из 30 суток, разрыв не меньше 2 суток "
                 "и больше 1,5 медианы собственного ритма; последнее сообщение канала "
                 "без возврата — unknown",
        "horizon_hours": 24, "limit_per_day": 20, "cooldown_days": 7,
        "config": "configs/heads.yaml (A_link)",
        "evaluations": [
            {"id": "june_2026", "period": june["days"], "days": june_days,
             "method": "temporal",
             "method_note": "каждый день оценён датированной моделью, порог выбран до дня "
                            "(задержка 1–8 суток)",
             **_counts(p70["alerts"], p70["hits"], p70["unknown"]),
             "positives": june["positives_in_period"],
             "recall": _ratio(p70["hits"], june["positives_in_period"]),
             "recall_unit": "положительные канало-сутки",
             "alerts_per_day": p70["alerts_per_day_mean"],
             "days_without_alerts": p70["days_without_alerts"],
             "source": "reports/a_link_operating_point_june.json", "command": CMD_JUNE},
            {"id": "five_windows", "period": period, "days": days, "method": "temporal",
             "method_note": "пять 90-дневных окон; порог выбран на 30 днях до окна",
             **model70, "positives": positives,
             "recall": _ratio(model70["hits"], positives),
             "recall_unit": "положительные канало-сутки",
             "alerts_per_day": round(model70["alerts"] / days, 2),
             "windows": [{"period": [f["test_start"], f["test_end"]],
                          **_a_link_pool([f], "min_precision_0.70", "model")} for f in folds],
             "source": "reports/a_link_live_policy_temporal.json", "command": CMD_A_LINK_5},
        ],
        "comparison": [
            {"what": "правило gap_vs_own_rhythm: те же 20 в сутки и пауза 7 суток, без порога",
             "period": period, **base_top20,
             "source": "reports/a_link_live_policy_temporal.json (fixed_top_20.baseline)",
             "command": CMD_A_LINK_5},
            {"what": "модель при тех же 20 в сутки и паузе 7 суток, без порога",
             "period": period, **model_top20,
             "source": "reports/a_link_live_policy_temporal.json (fixed_top_20.model)",
             "command": CMD_A_LINK_5},
            {"what": "правило gap_vs_own_rhythm с тем же выбором порога при минимуме 0,70",
             "period": period, **base70, "threshold_found_in_windows": feasible,
             "windows": len(folds),
             "note": "в окне 06.04–04.07.2025 порог не найден, но правило выдало 27 "
                     "рекомендаций (second_ml_42d_local.json, "
                     "inherited_evidence.infeasible_baseline_emissions)",
             "source": "reports/a_link_live_policy_temporal.json (min_precision_0.70.baseline)",
             "command": CMD_A_LINK_5},
        ],
        "base_rate": [
            {"period": june["days"], "positives": june["positives_in_period"],
             "known": JUNE_LABELED_ROWS,
             "rate_known": _ratio(june["positives_in_period"], JUNE_LABELED_ROWS),
             "note": "доля среди размеченных канало-суток; оценка PM 28.09",
             "source": JUNE_LABELS_SOURCE, "command": CMD_JUNE_LABELS},
            {"period": period, "positives": positives, "known": known,
             "unknown": unknown_rows,
             "rate_known": _ratio(positives, known),
             "rate_with_unknown": _ratio(positives, known + unknown_rows),
             "note": "positives = hits / recall_known по окнам",
             "source": "reports/a_link_live_policy_temporal.json", "command": CMD_A_LINK_5},
        ],
    }


# --- equipment_diag ------------------------------------------------------------

def _audit_head(report: dict, head: str) -> dict:
    return next(h for h in report["historical_metrics"] if h["head"] == head)


def _audit_eval(total: dict) -> dict:
    return {**_counts(total["alerts"], total["hits"], total["unknown_alerts"]),
            "positives": total["known_positive_days"],
            "recall": _ratio(total["hits"], total["known_positive_days"]),
            "recall_unit": "положительные канало-сутки",
            "episodes_caught": total["episodes_caught"],
            "episodes_eligible": total["episodes_eligible"],
            "episode_recall": _ratio(total["episodes_caught"], total["episodes_eligible"]),
            "alerts_per_day": round(total["alerts_per_day"], 2),
            "days_without_alerts": total["days_without_alerts"],
            "days": len(total["daily"])}


def _audit_base(total: dict) -> dict:
    known = total["candidates"] - total["unknown_candidates"]
    return {"positives": total["known_positive_days"], "known": known,
            "unknown": total["unknown_candidates"],
            "rate_known": _ratio(total["known_positive_days"], known),
            "rate_with_unknown": _ratio(total["known_positive_days"], total["candidates"])}


def equipment_diag() -> dict:
    val = _audit_head(_load("rule_vs_model_2025h2.json"), "D")
    d42 = _audit_head(_load("second_ml_42d_local.json"), "D")
    runs = [(val, "validation_2025h2", "reports/rule_vs_model_2025h2.json", CMD_VALIDATION,
             "четыре окна по 28 суток; порог правила выбирается по прошлому окну"),
            (d42, "may_june_2026", "reports/second_ml_42d_local.json", CMD_42D,
             "три окна по 14 суток; проверка уже принятого решения, новых просмотров нет")]
    evaluations, comparison, base = [], [], []
    for head, key, source, command, note in runs:
        period = [head["folds"][0]["start"], head["folds"][-1]["end"]]
        evaluations.append({"id": key, "period": period, "method": "temporal",
                            "method_note": note, **_audit_eval(head["total"]["baseline_70"]),
                            "source": f"{source} (D, baseline_70)", "command": command})
        for policy, what in (("model_70", "модель LightGBM, минимум точности 0,70"),
                             ("model_50", "модель LightGBM, минимум точности 0,50")):
            t = head["total"][policy]
            comparison.append({"what": what, "period": period,
                               **_counts(t["alerts"], t["hits"], t["unknown_alerts"]),
                               "episodes_caught": t["episodes_caught"],
                               "source": f"{source} (D, {policy})", "command": command})
        base.append({"period": period, **_audit_base(head["total"]["baseline_70"]),
                     "note": "доля положительных среди канало-суток оборудования",
                     "source": source, "command": command})
    old = _load("d_live_policy_temporal.json")["folds"]
    old_alerts = sum(f["model"]["alerts"] for f in old)
    old_hits = sum(f["model"]["true_alerts"] for f in old)
    return {
        "code": "equipment_diag", "title": TITLES["equipment_diag"], "head": "D",
        "in_product": "правило n_bad_w7 (число плохих состояний канала за 7 суток); порог "
                      "выбирается по понедельникам на прошлом 30-дневном окне при минимуме "
                      "точности 0,70 и не меньше 30 рекомендаций (src/mkl/rule_head.py); "
                      "обученной модели нет",
        "target": "записанный сигнал тревоги или плохого состояния оборудования "
                  "(насос, вентилятор, ИБП, люк, датчик затопления) в следующие 7 суток",
        "label": "D_observed_v1: промах засчитывается только при телеметрии за все 7 "
                 "будущих суток, иначе unknown",
        "horizon_hours": 168, "limit_per_day": 3, "limit_note": "с распределением по объектам",
        "cooldown_days": 7, "config": "configs/heads.yaml (D)",
        "evaluations": evaluations,
        "comparison": comparison,
        "base_rate": base,
        "previous_protocol": {
            "what": "модель LightGBM с порогом, до 3 в сутки, пауза 7 суток; в продукте её нет",
            "period": [old[0]["test_start"], old[-1]["test_end"]],
            "alerts": old_alerts, "hits": old_hits,
            "precision": _ratio(old_hits, old_alerts),
            "note": "метка label_wear: сутки без сигнала считаются отрицательными и при "
                    "отсутствии телеметрии (src/mkl/labels.py, _emit); модель заменена "
                    "правилом по RULE_VS_MODEL_PROTOCOL.md",
            "source": "reports/d_live_policy_temporal.json",
            "command": "python scripts/eval_d_live_policy.py --end-date 2026-06-23"},
    }


# --- guard_weekly --------------------------------------------------------------

def _mondays(start: str, end: str) -> int:
    day, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    day += dt.timedelta(days=(7 - day.weekday()) % 7)
    return (last - day).days // 7 + 1 if day <= last else 0


def guard_weekly() -> dict:
    report = next(r for r in _load("guard_weekly_repeats.json")["reports"]
                  if r["min_alarm_days_in_last_7"] == 4 and r["cooldown_days"] == 14)
    periods, pooled = report["periods"], report["pooled"]
    period = [periods[0]["start"], periods[-1]["end"]]
    weeks = _mondays(*period)
    candidates = sum(p["eligible_candidate_weeks"] for p in periods)
    positives = sum(p["eligible_positive_weeks"] for p in periods)
    model_report = _load("guard_weekly_model.json")
    runs = [r for r in model_report["results"] if r["min_alarm_days_in_last_7"] == 4]
    model = {r["method"]: r["pooled"] for r in runs}
    model_period = [runs[0]["periods"][0]["start"], runs[0]["periods"][-1]["end"]]
    return {
        "code": "guard_weekly", "title": TITLES["guard_weekly"], "head": "guard_weekly",
        "in_product": "правило: объект на охране, сообщение о состоянии не старше 7 суток, "
                      "записанный охранный сигнал не меньше чем в 4 из 7 прошлых дней; "
                      "порядок — дни с сигналом за неделю, затем за месяц, затем давность",
        "target": "записанный сигнал СМВУ при охране в дни D+2…D+8 после понедельника D; "
                  "продолжение записанной активности, не прогноз проникновения",
        "label": "v2-разметка по состоянию охраны в момент сигнала; без телеметрии — unknown",
        "horizon_hours": 168, "limit_per_week": 4, "cooldown_days": 14,
        "config": "src/mkl/guard_weekly.py",
        "evaluations": [
            {"id": "retrospective_2023_2026", "period": period, "weeks": weeks,
             "method": "retrospective",
             "method_note": "порог «4 дня» и пауза 14 суток подобраны на тех же годах",
             **_counts(pooled["alerts"], pooled["hits"], pooled["unknown"]),
             "positives": pooled["eligible_positive_weeks"],
             "recall": _ratio(pooled["hits"], pooled["eligible_positive_weeks"]),
             "recall_unit": "положительные объект-недели",
             "alerts_per_week": round(pooled["alerts"] / weeks, 2),
             "unique_objects": pooled["unique_objects"],
             "unique_hit_episodes": pooled["unique_hit_episodes"],
             "windows": [{"period": [p["start"], p["end"]],
                          **_counts(p["alerts"], p["hits"], p["unknown"])} for p in periods],
             "source": "reports/guard_weekly_repeats.json (4 из 7, пауза 14)",
             "command": CMD_GUARD},
        ],
        "comparison": [
            {"what": f"LightGBM против правила, обучение до {model_report['fit_end']}, "
                     "те же 4 из 7 и 4 в неделю, до паузы",
             "period": model_period,
             "model": {"alerts": model["ml"]["alerts"], "hits": model["ml"]["hits"]},
             "rule": {"alerts": model["rule"]["alerts"], "hits": model["rule"]["hits"]},
             "source": "reports/guard_weekly_model.json", "command": CMD_GUARD_MODEL},
        ],
        "base_rate": [
            {"period": period, "positives": positives, "candidates": candidates,
             "rate_with_unknown": _ratio(positives, candidates),
             "note": "доля положительных объект-недель среди всех кандидатов; число "
                     "unknown среди кандидатов в отчёте не приведено",
             "source": "reports/guard_weekly_repeats.json", "command": CMD_GUARD},
        ],
    }


def _queue_block(head: dict, budget: str, side: str) -> dict:
    x = head["budgets"][budget][side]
    return {**_counts(x["recommendations"], x["hits"], x["unknown"]),
            "new_hits": x["new_hits"], "recall": x["recall"],
            "recall_new": x["recall_new"], "per_day": x["per_day"]}


def _object_queue(code: str, head_name: str, budget: str, rule: str, **fields) -> dict:
    """B и E: помесячная временная проверка той же политики, что в продукте."""
    report = _load("fire_flood_product.json")
    cd7 = _load("fire_flood_product_cooldown7.json")
    head = report["heads"][head_name]
    period = head["period"]
    model = _queue_block(head, budget, "model")
    return {
        "code": code, "title": TITLES[code], "head": head_name,
        **fields,
        "horizon_hours": 24, "limit_per_day": int(budget),
        "cooldown_days": report["cooldown_days"],
        "config": "configs/heads.yaml",
        "evaluations": [
            {"id": "monthly_refresh_2025_2026", "period": period, "days": head["days"],
             "method": "temporal",
             "method_note": "модель на каждый месяц обучена по данным до его начала; "
                            "политика (лимит, пауза) выбрана на том же периоде из шести "
                            "вариантов — ретроспективно",
             **model,
             "positives": head["positives"], "positives_new": head["positives_new"],
             "recall_unit": "положительные строки метки за период",
             "by_month": {m: {k: v for k, v in x.items() if k in (
                             "recommendations", "hits", "unknown", "precision_lower")}
                          for m, x in head["budgets"][budget]["model_by_month"].items()},
             "source": FIRE_FLOOD_SOURCE, "command": CMD_FIRE_FLOOD},
        ],
        "comparison": [
            {"what": rule, "period": period,
             **_queue_block(head, budget, "rule"),
             "source": FIRE_FLOOD_SOURCE, "command": CMD_FIRE_FLOOD},
            {"what": "та же модель с паузой 7 суток по объекту", "period": period,
             **_queue_block(cd7["heads"][head_name], budget, "model"),
             "source": FIRE_FLOOD_CD7_SOURCE, "command": CMD_FIRE_FLOOD_CD7},
        ] + [
            {"what": f"та же модель, до {other} в сутки", "period": period,
             **_queue_block(head, other, "model"),
             "source": FIRE_FLOOD_SOURCE, "command": CMD_FIRE_FLOOD}
            for other in head["budgets"] if other != budget
        ],
        "base_rate": [
            {"period": period, "positives": head["positives"],
             "candidates": head["label_rows"],
             "rate_with_unknown": _ratio(head["positives"], head["label_rows"]),
             "known": head["known_label_rows"], "known_positives": head["known_positives"],
             "rate_known": _ratio(head["known_positives"], head["known_label_rows"]),
             "note": "rate_with_unknown — доля положительных среди строк метки, сутки без "
                     "данных считаются отрицательными, как в обучении; rate_known — "
                     "только строки с данными за следующие сутки",
             "source": FIRE_FLOOD_SOURCE, "command": CMD_FIRE_FLOOD},
        ],
    }


def fire_risk() -> dict:
    return _object_queue(
        "fire_risk", "B", "10",
        "правило «тревоги участка за 7 суток» (n_alarms_w7), те же 10 в сутки, без паузы",
        in_product="LightGBM на участок объекта (10 пикетов), top-10 участков в сутки "
                   "по риску, без порога точности и без паузы",
        target="текстовое пожарное или газовое тревожное состояние (дым, газ, "
               "температура выше 40 °C) на участке в следующие сутки; подтверждённых "
               "пожаров в данных нет",
        label="label_fire (src/mkl/labels.py); в факте продукта сутки без данных "
              "участка — unknown (src/mkl/outcomes.py)")


def flood_risk() -> dict:
    return _object_queue(
        "flood_risk", "E", "5",
        "правило «Затоплен сегодня» (n_flood за сутки расчёта), те же 5 в сутки, без паузы",
        in_product="LightGBM на объект с насосами, top-5 объектов в сутки по риску, "
                   "без порога точности и без паузы",
        target="состояние «Затоплен» с каналов насосов объекта в следующие сутки",
        label="label_flood (src/mkl/labels.py), только объекты с насосами; в факте "
              "продукта сутки без данных объекта — unknown")


# --- время расчёта -------------------------------------------------------------

def runtime() -> list[dict]:
    """Время ответа ML за каждый день 01–30.06: итоговая строка вывода measure_ml.py."""
    lines = RUNTIME_FILE.read_text(encoding="utf-8").splitlines()
    summary = json.loads(lines[-1])["summary"]
    what = {"score": "POST /api/v1/score сервиса ML, один день расчёта, головы A_link и D, "
                      "с факторами и пустым журналом выданного",
            "guard_weekly": "GET /api/v1/guard-weekly-inspections, понедельники 01–29.06"}
    return [{"what": what[kind], "calls": s["n"],
             "seconds": {"min": s["min"], "median": s["median"], "max": s["max"]},
             "conditions": RUNTIME_CONDITIONS,
             "source": "docs/submission/perf/ml_score_june.jsonl (строка summary)",
             "command": RUNTIME_COMMAND}
            for kind, s in summary.items()]


# --- отклонённые постановки и рычаги ----------------------------------------

def _temperature() -> dict:
    """Среднее трёх полугодовых тестов, чистое окно 24 ч, все каналы.

    JSON — копия отчёта PR #8 без изменений; перенесённый код на полном датасете не
    перезапускался (reports/TEMPERATURE_EPISODE_HOURLY.md, «Происхождение чисел»).
    """
    protocol = _load("temperature_episode_24h.json")["protocols"]["temporal_all_channels"]
    folds = protocol["folds"]
    # В отчёте PR #8 правая граница теста не включается; в реестре периоды включительные.
    last = dt.date.fromisoformat(folds[-1]["boundaries"]["test_end"]) - dt.timedelta(days=1)
    return {"period": [folds[0]["boundaries"]["test_start"], last.isoformat()],
            "tests": len(folds),
            "precision": round(protocol["mean_test"]["precision"], 3),
            "recall": round(protocol["mean_test"]["recall"], 3)}


def rejected_setups() -> list[dict]:
    queue = _load("intrusion_operational_queue.json")["pooled"]
    ml_queue = queue["recorded_full"]["all_top4"]
    rule_queue = _load("guard_review_queue_backtest.json")["pooled"]
    strict = _load("a_strict_top1.json")
    temperature = _temperature()
    return [
        {"id": "C_daily_guard", "what": "дневная охранная очередь: записанный охранный "
                                         "сигнал при охране завтра",
         "limit_per_day": 4,
         "results": [
             {"period": ["2025-04-05", "2025-12-30"], "model": "LightGBM",
              **_counts(ml_queue["alerts"], ml_queue["recorded_hits"],
                        ml_queue["unknown_outcome_alerts"]),
              "recall": round(ml_queue["recorded_recall"], 4),
              "base_rate": _ratio(ml_queue["positive_recorded"], ml_queue["candidates"]),
              "source": "reports/intrusion_operational_queue.json (recorded_full, all_top4)",
              "command": "python scripts/exp_intrusion_operational_queue.py"},
             {"period": ["2025-04-05", "2025-12-30"], "model": "правило истории",
              **_counts(rule_queue["alerts"], rule_queue["recorded_hits"],
                        rule_queue["unknown_outcome_alerts"]),
              "source": "reports/guard_review_queue_backtest.json",
              "command": "python scripts/backtest_guard_review_queue.py"}]},
        {"id": "A_strict", "what": "аномалия датчика завтра, один канал в сутки",
         "limit_per_day": 1,
         "results": [
             {"period": [strict["folds"][0]["test_start"], strict["folds"][-1]["test_end"]],
              "alerts": strict["pooled"]["model"]["alerts"],
              "precision_daily": round(strict["pooled"]["model"]["daily_precision"], 4),
              "rule_precision_daily": round(strict["pooled"]["rule"]["daily_precision"], 4),
              "source": "reports/a_strict_top1.json",
              "command": "python scripts/exp_a_strict_top1.py"}]},
        {"id": "temperature", "what": "выход температуры за диапазон 3–40 °C за 24 часа",
         "results": [{**temperature,
                      "note": "среднее трёх полугодовых тестов 2024H1–2026H1; тест 2026H1 — "
                              "уже просмотренный период; смысл диапазона владелец данных "
                              "не подтвердил. Числа посчитаны кодом PR #8, перенесённый "
                              "код на полном датасете не перезапускался",
                      "source": "reports/temperature_episode_24h.json "
                                "(protocols.temporal_all_channels.mean_test); "
                                "reports/TEMPERATURE_EPISODE_HOURLY.md",
                      "command": "python scripts/exp_temperature_episode.py features; "
                                 "python scripts/exp_temperature_episode.py backtest "
                                 "--clean-hours 24"}]},
        {"id": "gas_detected", "what": "«Обнаружен газ» как прогнозируемое событие",
         "results": [{"share_weekdays_09_15": _ratio(GAS_PLANNED["records_in_window"],
                                                     GAS_PLANNED["records_total"]),
                      "records_in_window": GAS_PLANNED["records_in_window"],
                      "records_total": GAS_PLANNED["records_total"],
                      "note": "будни 9:00–14:59 МСК: плановые поверки баллонами",
                      "source": GAS_PLANNED["source"], "command": None}]},
    ]


def rejected_levers() -> list[dict]:
    ens = _load("a_link_ensemble_experiment.json")
    single, trio = ens["pooled"]["lgbm"], ens["pooled"]["lgbm+xgb+cat"]
    more_hits = better_precision = 0
    for fold in ens["folds"]:
        a = fold["scores"]["lgbm"]["min_precision_0.70"]
        b = fold["scores"]["lgbm+xgb+cat"]["min_precision_0.70"]
        more_hits += b["hits"] > a["hits"]
        better_precision += b["precision_lower_bound"] > a["precision_lower_bound"]
    june = _load("a_link_operating_point_june.json")["policies"]
    folds = _load("a_link_live_policy_temporal.json")["folds"]
    val = _load("rule_vs_model_2025h2.json")
    link, wear = _audit_head(val, "A_link")["total"], _audit_head(val, "D")["total"]
    laya = _load("laya_typed_d_decisions.json")

    def pair(total, policy):
        t = total[policy]
        return {**_counts(t["alerts"], t["hits"], t["unknown_alerts"]),
                "episodes_caught": t["episodes_caught"]}

    return [
        {"id": "ensemble", "what": "A_link: среднее калиброванных вероятностей LightGBM, "
                                    "XGBoost и CatBoost против одной LightGBM, политика 0,70",
         "lightgbm": {k: single["min_precision_0.70"][k] for k in ("alerts", "hits",
                                                                   "precision_lower_bound")},
         "ensemble": {k: trio["min_precision_0.70"][k] for k in ("alerts", "hits",
                                                                 "precision_lower_bound")},
         "windows": len(ens["folds"]), "windows_more_hits": more_hits,
         "windows_higher_precision": better_precision,
         "criterion": "больше попаданий при точности не ниже, суммарно и не хуже в 4 из 5 окон",
         "decision": "не принят",
         "source": "reports/a_link_ensemble_experiment.json; reports/A_LINK_ENSEMBLE.md",
         "command": "python scripts/exp_a_link_ensemble.py --end-date 2026-06-29"},
        {"id": "operating_point", "what": "A_link: минимум точности при выборе порога 0,50 "
                                           "против 0,70",
         "june_2026": {name: _counts(p["alerts"], p["hits"], p["unknown"])
                       | {"alerts_per_day": p["alerts_per_day_mean"],
                          "recall": p["recall_known"]}
                       for name, p in june.items()},
         "five_windows": {name: _a_link_pool(folds, name, "model")
                          for name in ("min_precision_0.50", "min_precision_0.70")},
         "decision": "в продукте 0,70",
         "source": "reports/a_link_operating_point_june.json; "
                   "reports/a_link_live_policy_temporal.json; reports/A_LINK_OPERATING_POINT.md",
         "command": f"{CMD_JUNE}; {CMD_A_LINK_5}"},
        {"id": "rule_vs_model", "what": "замена модели правилом по протоколу "
                                         "RULE_VS_MODEL_PROTOCOL.md, валидация 2025 года",
         "period": [val["historical_metrics"][0]["folds"][0]["start"],
                    val["historical_metrics"][0]["folds"][-1]["end"]],
         "A_link": {"model_50": pair(link, "model_50"), "rule_50": pair(link, "baseline_50"),
                    "decision": "модель остаётся"},
         "D": {"model_70": pair(wear, "model_70"), "rule_70": pair(wear, "baseline_70"),
               "decision": "правило n_bad_w7 вместо модели"},
         "source": "reports/rule_vs_model_2025h2.json; reports/RULE_VS_MODEL_RESULT.md",
         "command": CMD_VALIDATION},
        {"id": "laya_second_opinion", "what": "D: языковая модель Laya typed-decisions как "
                                               "второе мнение к shortlist LightGBM",
         "shortlist": laya["candidate_count"],
         "shortlist_precision": round(laya["shortlist_positive_rate"], 4),
         "needs_review_precision": round(laya["needs_review_top_half"]["precision"], 4),
         "action_filter": {"alerts": laya["action_manual_or_urgent"]["alerts"],
                           "hits": laya["action_manual_or_urgent"]["hits"],
                           "precision": round(laya["action_manual_or_urgent"]["precision"], 4)},
         "decision": "не принят: не отделяет кандидатов лучше исходного списка",
         "source": "reports/laya_typed_d_decisions.json; reports/LAYA_TYPED_DECISIONS.md",
         "command": "python scripts/eval_laya_typed_decisions.py"},
    ]


def holdout() -> dict:
    text = (REPORTS / "holdout_uses.md").read_text(encoding="utf-8")
    views, states = map(int, re.search(r"\*\*(\d+) раз\*\* на \*\*(\d+) состояниях", text).groups())
    last = re.findall(r"^\| (\d{4}-\d\d-\d\dT\d\d:\d\d) \|", text, flags=re.M)[-1]
    return {"period": ["2026-01-01", "2026-06-30"], "views": views, "code_states": states,
            "last_entry": last,
            "not_counted": "счётчик пишет только scripts/final_eval.py; eval_a_link_policy.py, "
                           "audit_second_ml.py, eval_a_link_operating_point.py и "
                           "exp_a_link_ensemble.py тоже считают январь–июнь 2026, "
                           "но в счётчик не пишут",
            "source": "reports/holdout_uses.md", "command": "python scripts/holdout_uses.py"}


def quality_screen(scenarios: dict[str, dict]) -> dict:
    """Опорные числа экрана «Качество прогноза»: точность там считается по известным исходам.

    Доли округляются из исходных счётчиков до трёх знаков: экран показывает процент
    с одним знаком, и повторное округление 0,1495 дало бы 15% вместо 14,9%.
    """
    link = scenarios["sensor_link"]
    link_base = link["base_rate"][1]
    link_rule = link["comparison"][0]
    wear_base = scenarios["equipment_diag"]["base_rate"][1]
    guard_base = scenarios["guard_weekly"]["base_rate"][0]
    guard_cmp = scenarios["guard_weekly"]["comparison"][0]

    def queue(code: str, rule: str, what: str) -> dict:
        sc = scenarios[code]
        base, cmp = sc["base_rate"][0], sc["comparison"][0]
        return {
            "base_rate": _ratio(base["known_positives"], base["known"], 3),
            "rule_precision": _ratio(cmp["hits"], cmp["alerts"] - cmp["unknown"], 3),
            "period": f"{span(base['period'])}, модель на каждый месяц",
            "source": "ml/reports/FIRE_FLOOD_PRODUCT.md",
            "note": f"Простое правило «{rule}» с тем же лимитом {sc['limit_per_day']} в "
                    f"сутки, без паузы. Обе величины — по известным исходам: {what}."}

    def span(period):
        a, b = (dt.date.fromisoformat(x).strftime("%d.%m.%Y") for x in period)
        return f"{a}–{b}"

    return {
        "sensor_link": {
            "base_rate": _ratio(link_base["positives"], link_base["known"], 3),
            "rule_precision": _ratio(link_rule["hits"], link_rule["alerts"] - link_rule["unknown"], 3),
            "period": f"{span(link_base['period'])}, пять 90-дневных окон",
            "source": "ml/reports/A_LINK_LIVE_POLICY_TEMPORAL.md",
            "note": "Простое правило «канал молчит дольше своего обычного ритма» с теми же "
                    "20 рекомендациями в сутки и паузой 7 суток, без порога. Обе величины — "
                    "по известным исходам, как недельная точность на экране."},
        "equipment_diag": {
            "base_rate": _ratio(wear_base["positives"], wear_base["known"], 3),
            "rule_precision": None,
            "period": f"{span(wear_base['period'])}, три окна по 14 суток",
            "source": "ml/reports/SECOND_ML_LOCAL_42D_AUDIT.md",
            "note": "В продукте само простое правило «плохие состояния агрегата за 7 суток», "
                    "сравнивать его не с чем: обученная модель при минимуме точности 0,70 "
                    "в этих окнах не выдала ни одной рекомендации. "
                    "База — доля положительных среди канало-суток оборудования с известным "
                    "исходом."},
        "guard_weekly": {
            "base_rate": _ratio(guard_base["positives"], guard_base["candidates"], 3),
            "rule_precision": None,
            "period": f"{span(guard_base['period'])}, понедельники",
            "source": "ml/reports/GUARD_WEEKLY_INSPECTIONS.md",
            "note": f"Очередь сама является правилом. Обученная модель на тех же условиях дала "
                    f"{guard_cmp['model']['hits']} из {guard_cmp['model']['alerts']}, правило — "
                    f"{guard_cmp['rule']['hits']} из {guard_cmp['rule']['alerts']}. База считается "
                    "от всех объект-недель, включая неизвестный исход."},
        "fire_risk": queue("fire_risk", "тревоги участка за 7 суток",
                           "сутки, за которые участок не прислал данных, не входят"),
        "flood_risk": queue("flood_risk", "Затоплен сегодня",
                            "сутки, за которые объект не прислал данных, не входят"),
    }


def build() -> dict:
    scenarios = {s["code"]: s for s in (sensor_link(), equipment_diag(), guard_weekly(),
                                        fire_risk(), flood_risk())}
    return {
        "schema_version": 1,
        "frozen": FROZEN,
        "command": "python scripts/build_submission_metrics.py",
        "measures": {
            "precision_lower_bound": "попадания / все рекомендации; unknown в знаменателе",
            "precision_known_only": "попадания / (рекомендации − unknown)",
            "recall": "попадания / положительные единицы периода (recall_unit)",
            "rate_known": "положительные / размеченные кандидаты",
            "rate_with_unknown": "положительные / все кандидаты, unknown в знаменателе",
            "temporal": "порог выбран на окне, которое кончается до проверяемого периода",
            "retrospective": "параметры политики подобраны на тех же периодах",
        },
        "scenarios": list(scenarios.values()),
        "runtime": runtime(),
        "holdout": holdout(),
        "rejected_setups": rejected_setups(),
        "rejected_levers": rejected_levers(),
        "quality_screen": quality_screen(scenarios),
    }


def render(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="не записывать, а сверить с reports/SUBMISSION_METRICS.json")
    args = parser.parse_args()
    text = render(build())
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.name} устарел: python scripts/build_submission_metrics.py",
                  file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"saved {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
