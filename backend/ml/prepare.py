"""Stream annual CSVs into a small daily pump dataset; never mix the demo source."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import py7zr


def extract_year(dataset_zip: Path, year: int, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    name = f"ext-journal-{year}.7z"
    archive_path = dest / name
    if not archive_path.exists():
        with (zipfile.ZipFile(dataset_zip) as bundle,
              bundle.open(f"dataset/{name}") as source, archive_path.open("wb") as target):
            shutil.copyfileobj(source, target)
    with py7zr.SevenZipFile(archive_path) as archive:
        entries = archive.list()
        files = [entry for entry in entries if not entry.is_directory]
        if len(files) != 1 or not files[0].filename.endswith(".csv"):
            raise ValueError(f"Expected one CSV in {name}")
        for entry in entries:
            if not (dest / entry.filename).resolve().is_relative_to(dest.resolve()):
                raise ValueError("Unsafe archive path")
        csv_path = dest / files[0].filename
        if not csv_path.exists():
            archive.extractall(path=dest)
        if csv_path.stat().st_size != files[0].uncompressed:
            raise ValueError(f"Incomplete extraction: {csv_path}")
    return csv_path


def prepare(paths: list[Path], catalog: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    channels = pd.read_csv(catalog)
    pump_ids = channels.loc[channels["тип_датчика"].eq("Состояние насоса"),
                           ["ид_канала_данных"]].rename(columns={"ид_канала_данных": "channel"})
    daily_parts, coverage_parts, sources = [], [], []
    with duckdb.connect() as con:
        con.execute("SET memory_limit='2GB'")
        con.execute("SET threads=4")
        con.register("pump_ids", pump_ids)
        for path in paths:
            started = time.perf_counter()
            escaped = str(path.resolve()).replace("'", "''")
            con.execute(f"""CREATE OR REPLACE VIEW raw AS SELECT
                try_cast("ид_канала_данных" AS BIGINT) channel,
                try_cast("дата" || ' ' || "время" AS TIMESTAMP) ts,
                try_cast("тревожное" AS BOOLEAN) alarm,
                "значение_датчика" sensor_value
                FROM read_csv('{escaped}', all_varchar=true, strict_mode=true)""")
            raw_count, rejected = con.execute("""SELECT count(*), count(*) FILTER (
                WHERE ts IS NULL OR channel IS NULL OR alarm IS NULL OR sensor_value IS NULL)
                FROM raw""").fetchone()
            coverage = con.execute('''SELECT CAST(ts AS DATE) AS "day", count(*) global_events,
                count(DISTINCT hour(ts)) observed_hours FROM raw
                WHERE ts IS NOT NULL AND channel IS NOT NULL AND alarm IS NOT NULL
                    AND sensor_value IS NOT NULL
                GROUP BY 1 ORDER BY 1''').df()
            # Deduplicate by semantic content, not unstable event ID. Simultaneous
            # values remain an unordered set; no synthetic state sequence is inferred.
            con.execute("""CREATE OR REPLACE TEMP TABLE pumps AS SELECT DISTINCT
                r.channel, ts, alarm, sensor_value FROM raw r JOIN pump_ids USING(channel)
                WHERE ts IS NOT NULL AND alarm IS NOT NULL AND sensor_value IS NOT NULL""")
            daily = con.execute("""WITH seconds AS (
                SELECT channel, ts, count(*) records, sum(CAST(alarm AS INT)) alarms,
                    max(CAST(sensor_value = 'Неисправен' AS INT)) fault,
                    max(CAST(sensor_value = 'Неопределен' AS INT)) undefined,
                    max(CAST(sensor_value = 'Обесточен' AS INT)) power_loss,
                    max(CAST(sensor_value = 'Включен' AS INT)) running,
                    CAST(count(DISTINCT sensor_value) > 1 AS INT) ambiguous
                FROM pumps GROUP BY channel, ts)
                SELECT channel, CAST(ts AS DATE) AS "day", sum(records) events,
                    sum(alarms) alarms, sum(fault) faults, sum(undefined) undefined,
                    sum(power_loss) power_loss, sum(running) running,
                    sum(ambiguous) ambiguous_seconds FROM seconds GROUP BY 1,2""").df()
            daily_parts.append(daily)
            coverage_parts.append(coverage)
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            sources.append({"file": path.name, "bytes": path.stat().st_size,
                            "sha256": digest.hexdigest(), "pump_rows": len(daily),
                            "raw_rows": raw_count, "rejected_rows": rejected,
                            "seconds": round(time.perf_counter() - started, 2)})
            print(json.dumps(sources[-1]), flush=True)
    daily = pd.concat(daily_parts, ignore_index=True).sort_values(["channel", "day"])
    coverage = pd.concat(coverage_parts, ignore_index=True).sort_values("day")
    if daily.duplicated(["channel", "day"]).any() or coverage.duplicated("day").any():
        raise ValueError("Sources overlap; use non-overlapping annual archives")
    daily.to_csv(output / "pump_daily.csv", index=False)
    coverage.to_csv(output / "coverage.csv", index=False)
    (output / "manifest.json").write_text(json.dumps({
        "sources": sources, "catalog_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "sensor_type": "Состояние насоса", "timezone_assumption": "Europe/Moscow",
        "deduplication": "channel,timestamp,alarm,value within source",
        "coverage_rule": "All 24 hour bins must contain global telemetry; heuristic only",
    }, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-zip", type=Path, default=Path("dataset.zip"))
    parser.add_argument("--years", type=int, nargs="+", default=[2024, 2025, 2026])
    parser.add_argument("--catalog", type=Path,
                        default=Path("data/raw/справочник_каналов_датчиков.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/pumps"))
    args = parser.parse_args()
    paths = []
    for year in args.years:
        print(f"Extracting {year}", flush=True)
        paths.append(extract_year(args.dataset_zip, year, Path("analysis/extracted")))
    prepare(paths, args.catalog, args.output)


if __name__ == "__main__":
    main()
