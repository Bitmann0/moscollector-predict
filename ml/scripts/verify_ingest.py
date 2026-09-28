"""Сверка принятого журнала с независимым аудитом исходных CSV.

    python scripts/verify_ingest.py
    python scripts/verify_ingest.py --require-all
    python scripts/verify_ingest.py --interim /srv/ml/data/interim \\
        --profiles annual_profiles.json --audit deep_audit.json

Приём (src/mkl/ingest.py) считает только записанное в events_year=Y.parquet:
сколько строк исходника туда не попало, нигде не видно. Скрипт замыкает по
каждому году равенство «строки CSV = события + точные повторы + встроенные
заголовки». Слагаемые берутся из аудита, который читал CSV независимо от
приёма: число строк и значения «тревожное» — из analysis/annual_profiles.json,
точные повторы — из analysis/deep_audit.json. Ненулевая разница — это строки,
которые приём потерял (фильтр NULL после TRY_CAST, src/mkl/ingest.py:130),
или лишние, если LEFT JOIN со справочником размножил событие.

«Тревожное» проверяется отдельно. Приём считает тревогой только t и true, а
любое другое значение молча становится false (src/mkl/ingest.py:124). Поэтому
значение вне {t, f, true, false} в профиле исходника — ошибка, а число тревог
в parquet обязано лежать между «t минус точные повторы» и «t».

Суточная панель сверяется с событиями: сумма n_events и n_alarms в
daily_channel.parquet за год равна числу событий и тревог в events_year=Y.

Отсутствующий год ошибкой не считается: в бандле C4 из событий есть только
events_year=2026.parquet (scripts/build_bundle.py). --require-all требует все
годы аудита. Код возврата 1 — расхождение, 2 — нет файлов аудита.

Скрипт только читает. Проверка вынесена сюда, а не в приём: src/mkl/ingest.py
и panel.py — входы стадий ingest и panel (src/mkl/pipeline.py), и их правка
заставила бы `mkl run` пересобрать панель, фичестор и модели.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import duckdb

from mkl.config import PATHS

EVENTS_RE = re.compile(r"events_year=(\d{4})\.parquet")
# Встроенный заголовок узнаётся по колонке «тревожное»: в ней стоит её же имя.
HEADER_VALUE = "тревожное"
ALARM_TRUE = frozenset({"t", "true"})
ALARM_KNOWN = ALARM_TRUE | {"f", "false"}
# В монорепо ml/ — корень MKL_ROOT, а аудит лежит рядом, в analysis/. В
# ML-контейнере (/srv/ml) каталога analysis/ нет, там пути передаются явно.
ANALYSIS = PATHS.root.parent / "analysis"


def audit_expectations(profiles: list[dict], audit: dict) -> dict[int, dict]:
    """Ожидание по каждому году из аудита исходных CSV.

    В deep_audit.json дубли посчитаны только для лет, где число строк не
    совпало с числом разных ID, и exact_duplicate_excess бывает null (2025:
    расхождение дал заголовок, у него ID пустой). И отсутствие, и null — ноль.
    """
    duplicates = audit.get("duplicates") or {}
    out: dict[int, dict] = {}
    for profile in profiles:
        year = int(profile["year"])
        values = {row["alarm_raw"]: int(row["n"]) for row in profile["alarm_raw"]}
        headers = values.get(HEADER_VALUE, 0)
        dups = (duplicates.get(str(year)) or {}).get("exact_duplicate_excess") or 0
        rows = int(profile["summary"]["row_count"])
        unknown = sorted((v for v in values
                          if v != HEADER_VALUE and (v is None or v.lower() not in ALARM_KNOWN)),
                         key=str)
        out[year] = {
            "source_rows": rows,
            "embedded_headers": headers,
            "exact_duplicates": int(dups),
            "expected_events": rows - headers - int(dups),
            "source_alarm_true": sum(n for v, n in values.items()
                                     if v is not None and v.lower() in ALARM_TRUE),
            # Списком, как в профиле: ключом словаря пустое значение стало бы строкой.
            "alarm_values": [{"alarm_raw": v, "n": n}
                             for v, n in sorted(values.items(), key=lambda x: str(x[0]))],
            "unknown_alarm_values": unknown,
        }
    return out


def reconcile(expected: dict[int, dict], events: dict[int, dict],
              daily: dict[int, dict] | None, *, require_all: bool = False) -> dict:
    """Сверить наблюдаемые счётчики с ожиданием. Чистая функция, без файлов.

    events: год → {"events", "alarms", "outside_year"} из events_year=Y.parquet.
    daily: год суток → {"n_events", "n_alarms"} из daily_channel.parquet или
    None, если панели нет.
    """
    errors: list[dict] = []
    years: dict[str, dict] = {}

    def fail(year: int | None, code: str, detail) -> None:
        errors.append({"year": year, "code": code, "detail": detail})

    for year, exp in sorted(expected.items()):
        # Исходник проверяется и без parquet: панель в бандле C4 собрана по всем
        # годам, и ложные false из этого года уже в ней.
        if exp["unknown_alarm_values"]:
            fail(year, "unknown_alarm_values", exp["unknown_alarm_values"])
    not_present = sorted(set(expected) - set(events))
    if require_all:
        for year in not_present:
            fail(year, "year_not_present", f"нет events_year={year}.parquet")
    not_audited = sorted(set(events) - set(expected))

    for year in sorted(events):
        got = events[year]
        row: dict = {"events": got["events"], "alarms": got["alarms"]}
        if got["outside_year"]:
            # Иначе сверка панели по году суток сравнивала бы разные множества.
            fail(year, "rows_outside_year", got["outside_year"])
        exp = expected.get(year)
        if exp is not None:
            missing = exp["expected_events"] - got["events"]
            lo = exp["source_alarm_true"] - exp["exact_duplicates"]
            hi = exp["source_alarm_true"]
            row = {**{k: v for k, v in exp.items() if k != "unknown_alarm_values"},
                   **row, "missing_events": missing, "alarms_bounds": [lo, hi]}
            if missing:
                fail(year, "missing_events", missing)
            if not lo <= got["alarms"] <= hi:
                fail(year, "alarms_out_of_bounds",
                     {"alarms": got["alarms"], "bounds": [lo, hi]})
        if daily is not None:
            day = daily.get(year, {"n_events": 0, "n_alarms": 0})
            row["daily_n_events"] = day["n_events"]
            row["daily_n_alarms"] = day["n_alarms"]
            if day["n_events"] != got["events"]:
                fail(year, "daily_events_mismatch",
                     {"events": got["events"], "daily_n_events": day["n_events"]})
            if day["n_alarms"] != got["alarms"]:
                fail(year, "daily_alarms_mismatch",
                     {"alarms": got["alarms"], "daily_n_alarms": day["n_alarms"]})
        years[str(year)] = row
    if daily is None:
        fail(None, "daily_channel_absent", "нет daily_channel.parquet")

    audited = [y for y in sorted(events) if y in expected]
    totals = {key: sum(expected[y][key] for y in audited)
              for key in ("source_rows", "embedded_headers", "exact_duplicates",
                          "expected_events")}
    totals["events"] = sum(events[y]["events"] for y in sorted(events))
    totals["alarms"] = sum(events[y]["alarms"] for y in sorted(events))
    return {
        "ok": not errors,
        "require_all": require_all,
        "years": years,
        "not_present": not_present,
        "not_audited": not_audited,
        "totals": totals,
        "errors": errors,
    }


def event_files(interim: Path) -> dict[int, Path]:
    """Годовые файлы событий; год — из имени, как их пишет приём."""
    return {int(m.group(1)): path for path in sorted(interim.glob("events_year=*.parquet"))
            if (m := EVENTS_RE.fullmatch(path.name))}


def observe_events(con: duckdb.DuckDBPyConnection, path: Path, year: int) -> dict:
    n, alarms, outside = con.execute(f"""
        SELECT count(*), count(*) FILTER (WHERE alarm),
               count(*) FILTER (WHERE year(day) <> {int(year)})
        FROM read_parquet('{path.as_posix()}')
    """).fetchone()
    return {"events": n, "alarms": alarms, "outside_year": outside}


def observe_daily(con: duckdb.DuckDBPyConnection, path: Path) -> tuple[dict[int, dict], dict]:
    src = f"read_parquet('{path.as_posix()}')"
    by_year = {int(y): {"n_events": int(e), "n_alarms": int(a)} for y, e, a in con.execute(
        f"SELECT year(day), sum(n_events), sum(n_alarms) FROM {src} GROUP BY 1 ORDER BY 1"
    ).fetchall()}
    rows, channels, n_events, n_alarms = con.execute(
        f"SELECT count(*), count(DISTINCT ch), sum(n_events), sum(n_alarms) FROM {src}"
    ).fetchone()
    panel = {"rows": rows, "channels": channels,
             "n_events": int(n_events or 0), "n_alarms": int(n_alarms or 0)}
    return by_year, panel


def _print_report(report: dict) -> None:
    print(f"{'год':>6} {'строк CSV':>12} {'ожидалось':>12} {'событий':>12} "
          f"{'разница':>8} {'панель':>12}")
    for year, row in report["years"].items():
        print(f"{year:>6} {row.get('source_rows', '—'):>12} "
              f"{row.get('expected_events', '—'):>12} {row['events']:>12} "
              f"{row.get('missing_events', '—'):>8} {row.get('daily_n_events', '—'):>12}")
    if report["not_present"]:
        print("нет parquet за годы: " + ", ".join(map(str, report["not_present"])))
    if report["not_audited"]:
        print("нет в аудите: " + ", ".join(map(str, report["not_audited"])))
    for err in report["errors"]:
        print(f"ОШИБКА {err['year']}: {err['code']} {err['detail']}")
    print("сверка сошлась" if report["ok"] else "сверка НЕ сошлась")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        description="Сверка events_year=*.parquet и daily_channel.parquet с аудитом CSV.")
    p.add_argument("--interim", type=Path, default=PATHS.interim,
                   help=f"каталог с parquet приёма (по умолчанию {PATHS.interim})")
    p.add_argument("--profiles", type=Path, default=ANALYSIS / "annual_profiles.json",
                   help="профиль исходных CSV по годам")
    p.add_argument("--audit", type=Path, default=ANALYSIS / "deep_audit.json",
                   help="аудит повторов исходных CSV")
    p.add_argument("--output", type=Path, default=PATHS.reports / "ingest_reconciliation.json",
                   help="куда записать JSON с результатом")
    p.add_argument("--require-all", action="store_true",
                   help="отсутствие любого года аудита — ошибка")
    p.add_argument("--memory-limit", default="1GB", help="предел памяти DuckDB")
    p.add_argument("--threads", type=int, default=4, help="потоки DuckDB")
    args = p.parse_args(argv)

    for path in (args.profiles, args.audit):
        if not path.is_file():
            print(f"нет файла аудита: {path}", file=sys.stderr)
            return 2
    expected = audit_expectations(
        json.loads(args.profiles.read_text(encoding="utf-8")),
        json.loads(args.audit.read_text(encoding="utf-8")))

    files = event_files(args.interim)
    daily_path = args.interim / "daily_channel.parquet"
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{args.memory_limit}'")
    con.execute(f"SET threads={int(args.threads)}")
    try:
        events = {year: observe_events(con, path, year) for year, path in files.items()}
        daily, panel = observe_daily(con, daily_path) if daily_path.is_file() else (None, None)
    finally:
        con.close()

    report = reconcile(expected, events, daily, require_all=args.require_all)
    report["daily_channel"] = panel
    # Имена, а не пути: абсолютный путь привязал бы отчёт к одной машине.
    report["inputs"] = {"profiles": args.profiles.name, "audit": args.audit.name,
                        "events": [p.name for p in files.values()],
                        "daily_channel": daily_path.name if panel is not None else None}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    _print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
