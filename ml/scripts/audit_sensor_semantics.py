"""Аудит семантики датчиков и дневных целей на данных mkl (перенос PR #7).

Пересчитывает analysis/experiments/deep-data-audit/sensor-audit.json и
target-audit.json по events_year=*.parquet и daily_channel.parquet и сверяет
счётчики с ними. Пишет только агрегаты по типам датчиков, без ID каналов.

    cd ml
    python scripts/audit_sensor_semantics.py --temp-dir <каталог для сброса DuckDB>

Данные берутся из PATHS (MKL_ROOT, по умолчанию ml/). Лимиты памяти и потоков
по умолчанию — как в PR #7 (3GB, 4), а не 10GB и 8 из db.connect: аудит
делит машину с обучением.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import subprocess
import time
from pathlib import Path

import duckdb

from mkl import db
from mkl import sensor_audit as audit
from mkl.config import HOLDOUT_END, PATHS

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT.parent / "analysis" / "experiments" / "deep-data-audit"
CATALOG = "справочник_каналов_датчиков.csv"
CODE_FILES = ("scripts/audit_sensor_semantics.py", "src/mkl/sensor_audit.py")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inputs(con) -> list[dict]:
    """Какие файлы прочитаны: имя, размер, число строк по метаданным, SHA-256."""
    files = sorted(PATHS.interim.glob("events_year=*.parquet"))
    files.append(PATHS.interim / "daily_channel.parquet")
    out = []
    for path in files:
        rows = con.execute("SELECT count(*) FROM read_parquet(?)",
                           [path.as_posix()]).fetchone()[0]
        out.append({"file": path.name, "bytes": path.stat().st_size,
                    "rows": rows, "sha256": sha256(path)})
    return out


def scope(con) -> dict:
    catalog = PATHS.materials / CATALOG
    channels, types = con.execute(
        "SELECT count(DISTINCT ид_канала_данных), count(DISTINCT тип_датчика) "
        "FROM read_csv(?, header=true)", [catalog.as_posix()]).fetchone()
    events, observed, first, last = con.execute(
        "SELECT count(*), count(DISTINCT ch), min(day), max(day) FROM ev").fetchone()
    return {
        "source": "events_year=*.parquet и daily_channel.parquet из mkl.ingest и mkl.panel",
        "catalog": CATALOG, "catalog_sha256": sha256(catalog),
        "catalog_channels": channels, "catalog_sensor_types": types,
        "events": events, "observed_channels": observed,
        "first_day": first, "last_day": last,
        "note": ("Текущий справочник применён ко всей истории: исторических версий "
                 "нет. Тип датчика берётся из ev.stype, его заполнил ingest по "
                 "channels.parquet."),
    }


DEFINITIONS = {
    "observed_channel_days": ("Строки daily_channel; отсутствующая строка — сутки "
                              "без наблюдений, а не здоровые сутки."),
    "numeric_events": "События с val_num IS NOT NULL (TRY_CAST значения в ingest).",
    "channels_spanning_model_period": ("Первое и последнее наблюдение канала накрывают "
                                       "span_start…span_end; пропуски внутри возможны."),
    "alarms": ("Число исходных флагов тревожное=true; бизнес-смысл флага независимо "
               "не подтверждён."),
    "same_second_state_audit": ("Секунда канала как неупорядоченный набор значений: "
                                "порядок записей внутри секунды в журнале не определён."),
    "starts_after_observed_non_target_day": ("Целевые сутки, перед которыми у канала "
                                             "была строка панели без цели."),
    "temperature_outside_3_40": ("По сырому val_num, как в PR #7: переполнения -3276 и 999 "
                                 "не отсечены. Диапазон выведен из значения «В норме от +3 "
                                 "до +40», заказчиком не подтверждён."),
    "temperature_outside_3_40_valid": ("То же после отсечки config.VALUE_LIMITS (от -60 до "
                                       "150 °C), в PR #7 такого варианта нет."),
}

LIMITATIONS = [
    "Цели — сообщения, исходные флаги и выведенные пороги, а не подтверждённые инциденты.",
    "Присутствие состояния за сутки схлопывает повторы и не восстанавливает физический эпизод.",
    "Сверка с PR #7 сравнивает сборки разных хранилищ: normalized.duckdb PR #4 и parquet mkl.",
]


def run(args) -> dict:
    started, cpu = time.perf_counter(), time.process_time()
    con = db.connect(args.memory_limit, args.threads)
    if args.temp_dir:
        args.temp_dir.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory='{args.temp_dir.as_posix()}'")
    try:
        db.attach_events(con)
        db.attach_parquet(con, "daily_channel")
        result = {"schema_version": 1, "scope": scope(con), "inputs": inputs(con),
                  "parameters": {"second_start": args.second_start,
                                 "second_end": args.second_end,
                                 "span_start": args.span_start, "span_end": args.span_end,
                                 "top_values": args.top_values}}
        result.update(audit.audit_report(
            con, second_start=args.second_start, second_end=args.second_end,
            span_start=args.span_start, span_end=args.span_end, top_n=args.top_values))
    finally:
        con.close()
    sensor_ref = REFERENCE_DIR / "sensor-audit.json"
    target_ref = REFERENCE_DIR / "target-audit.json"
    result["parity_with_pr7"] = audit.compare_with_reference(
        result, json.loads(sensor_ref.read_text(encoding="utf-8")),
        json.loads(target_ref.read_text(encoding="utf-8")))
    result["parity_with_pr7"]["reference"] = {
        p.name: sha256(p) for p in (sensor_ref, target_ref)}
    result["definitions"] = DEFINITIONS
    result["limitations"] = LIMITATIONS
    result["runtime"] = {"wall_seconds": time.perf_counter() - started,
                         "cpu_seconds": time.process_time() - cpu,
                         "memory_limit": args.memory_limit, "threads": args.threads,
                         "duckdb": duckdb.__version__, "python": platform.python_version(),
                         "platform": platform.platform()}
    # Коммит без отметки о правках врёт: прогон незакоммиченного кода выглядел
    # бы прогоном HEAD. Хеши файлов ниже точны в любом случае.
    try:
        result["code_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        result["code_dirty"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--", *CODE_FILES], cwd=ROOT, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        result["code_commit"] = result["code_dirty"] = None
    result["code_files_sha256"] = {name: sha256(ROOT / name) for name in CODE_FILES}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    date = dt.date.fromisoformat
    parser.add_argument("--memory-limit", default="3GB")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--temp-dir", type=Path,
                        help="каталог сброса DuckDB; по умолчанию PATHS.tmp")
    parser.add_argument("--second-start", type=date, default=dt.date(2024, 1, 1))
    parser.add_argument("--second-end", type=date, default=HOLDOUT_END)
    parser.add_argument("--span-start", type=date, default=dt.date(2024, 1, 7))
    parser.add_argument("--span-end", type=date, default=dt.date(2026, 6, 23))
    parser.add_argument("--top-values", type=int, default=30)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports" / "sensor_semantics_audit.json")
    args = parser.parse_args()
    if args.threads < 1 or args.top_values < 1:
        parser.error("threads и top-values должны быть положительными")
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                      allow_nan=False, default=str) + "\n",
                           encoding="utf-8")
    parity = result["parity_with_pr7"]
    print(f"{args.output}: {result['scope']['events']} событий, "
          f"{len(parity['mismatches'])} расхождений с PR #7 "
          f"из {parity['checked_values']} сверенных значений, "
          f"{result['runtime']['wall_seconds']:.0f} с")


if __name__ == "__main__":
    main()
