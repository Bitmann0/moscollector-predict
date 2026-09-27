"""Сборка бандла C4 для ML_MODE=real (ML2-02, решение D10 плана команды).

    python scripts/build_bundle.py --source . --out ../dist
    python scripts/build_bundle.py --source . --out ../dist --version bundle-20260928-1 --archive
    python scripts/build_bundle.py --source U:/hackathon --dry-run

Из корня ML-проекта в <out>/<версия>/ копируется ровно то, что сервис читает в
режиме real, и рядом пишется MANIFEST.sha256 в формате sha256sum: хеш, два
пробела, путь от корня бандла. Раскладка совпадает с томами compose.real.yaml:
data/, models/, configs/features.yaml, reports/intrusion_eventtime_v2_build.json.
Лишние файлы корня (события прошлых лет, модели других голов) не берутся.

--archive упаковывает содержимое каталога в <out>/<версия>.7z с шифрованием
имён (-mhe=on). Пароль — тот же, что у датасета организаторов (D10). Его берём
из переменной BUNDLE_PASSWORD, а без неё 7z спрашивает пароль сам. В файлы и в
лог пароль не попадает; в команде, которую печатает скрипт, он заменён звёздами.
Из переменной пароль уходит в 7z ключом -p, поэтому на время упаковки он виден
в списке процессов этой машины.

Только stdlib: скрипт запускается там, где ML-зависимостей нет.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ML_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "MANIFEST.sha256"
VERSION_RE = re.compile(r"bundle-\d{8}-\d+")
PASSWORD_ENV = "BUNDLE_PASSWORD"
SEVEN_ZIP = ("7z", "7za", "7zz")
CHUNK = 8 * 1024 * 1024

# Верхний уровень бандла — контракт compose.real.yaml и scripts/fetch_bundle.*:
# два каталога монтируются целиком, два файла поштучно.
LAYOUT = ("data", "models", "configs/features.yaml",
          "reports/intrusion_eventtime_v2_build.json")

# Пилотные головы с файлом модели. D — правило n_bad_w7 (configs/heads.yaml:215),
# артефакт собирается при расчёте, файла нет (src/mkl/rule_head.py:16).
MODEL_HEADS = ("A_link",)

# Путь в бандле → кто его читает. Пути ведутся от корня ML-проекта, строки —
# на момент origin/main 9856984. tests/test_build_bundle.py сверяет список с
# константами модулей mkl и с томами compose.real.yaml.
REQUIRED = {
    "configs/features.yaml":
        "src/mkl/store.py:11 — реестр фичестора; из него serve.feature_signature "
        "(src/mkl/serve.py:25) сверяет модель с признаками",
    "reports/intrusion_eventtime_v2_build.json":
        "src/mkl/guard_queue.py:21 — версия и конец v2-кэша; при version != 2 "
        "очередь stale (src/mkl/guard_weekly.py:172)",
    **{f"models/{head}.pkl": "src/mkl/serve.py:22 — serve.model_path"
       for head in MODEL_HEADS},
    "data/features/sensor.parquet":
        "src/mkl/product_api.py:58, src/mkl/store.py:55 — признаки A_link и D",
    "data/features/object.parquet":
        "src/mkl/guard_weekly.py:148, src/mkl/outcomes.py:131 — недельная "
        "охранная очередь и её факт",
    "data/features/intrusion_eventtime_days_v2.parquet":
        "src/mkl/guard_queue.py:20 — v2-кэш охранной очереди",
    "data/interim/channels.parquet":
        "src/mkl/product_api.py:103, src/mkl/address.py:44 — справочник каналов, "
        "адреса алертов",
    "data/interim/daily_channel.parquet":
        "src/mkl/outcomes.py:92, src/mkl/rule_head.py:40 — факт A_link и D, "
        "порог правила D",
    "data/interim/episodes.parquet":
        "src/mkl/outcomes.py:111, src/mkl/rule_head.py:40 — эпизоды для факта D "
        "и порога правила D",
    "data/interim/group_outages.parquet":
        "scripts/train_latest.py:28 — дообучение в контейнере (ML1-10)",
    "data/interim/events_year=2026.parquet":
        "../scripts/replay.py:38 — поток событий демо-окна (C4, D10)",
}

# Берутся, если есть; без них сервис работает, но беднее.
OPTIONAL = {
    "data/interim/maintenance_2026.json":
        "src/mkl/maintenance.py:29 — графики ППР и ТО для контекста алерта A_link; "
        "без файла контекст schedule_not_loaded (src/mkl/service.py:69)",
}
# Артефакты окна {head}@{threshold_end}.pkl из ML1-05b (план, раздел 5).
# serve.model_path пока читает только {head}.pkl, поэтому они необязательны.
OPTIONAL_PATTERNS = {
    f"models/{head}@*.pkl": "артефакты окна ML1-05b; serve.model_path их пока не читает"
    for head in MODEL_HEADS
}
# Не берутся, хотя перечислены в C4 (план, раздел 4): в режиме real их не читает
# ни сервис, ни дообучение. data/features/segment.parquet нужен только голове B
# (не пилот), data/interim/weather.parquet — только сборке фичестора
# (src/mkl/features/external.py:12).


class BundleError(Exception):
    """Бандл собрать нельзя; текст объясняет, чего не хватает."""


@dataclass(frozen=True)
class Entry:
    rel: str
    size: int
    sha256: str | None = None


def default_version(today: dt.date | None = None) -> str:
    return f"bundle-{(today or dt.date.today()):%Y%m%d}-1"


def select_files(source: Path) -> tuple[list[str], list[str]]:
    """Что войдёт в бандл и каких необязательных файлов нет.

    Отсутствующие обязательные файлы перечисляются разом, а не по одному за
    запуск, и до копирования.
    """
    missing = [rel for rel in REQUIRED if not (source / rel).is_file()]
    if missing:
        lines = "\n".join(f"  {rel}  ({REQUIRED[rel]})" for rel in missing)
        raise BundleError(f"в {source} нет обязательных файлов бандла:\n{lines}")
    _check_build_info(source / "reports/intrusion_eventtime_v2_build.json")
    chosen = list(REQUIRED)
    absent = []
    for rel in OPTIONAL:
        (chosen if (source / rel).is_file() else absent).append(rel)
    for pattern in OPTIONAL_PATTERNS:
        chosen += sorted(p.relative_to(source).as_posix()
                         for p in source.glob(pattern) if p.is_file())
    for rel in chosen:
        # sha256sum экранирует такие имена, и проверка на стороне эксперта
        # разошлась бы с манифестом.
        if "\\" in rel or "\n" in rel:
            raise BundleError(f"недопустимое имя файла для манифеста: {rel!r}")
    return sorted(set(chosen)), absent


def _check_build_info(path: Path) -> None:
    """Без второй версии кэша недельная очередь отдаст stale на каждом запросе."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"{path} не читается как JSON: {exc}") from exc
    if not isinstance(info, dict) or info.get("version") != 2 or "end" not in info:
        raise BundleError(
            f"{path}: нужен v2-кэш охранной очереди (version 2 и поле end); "
            "пересоберите scripts/build_intrusion_eventtime_labels.py")


def copy_hashed(src: Path, dst: Path) -> tuple[int, str]:
    """Копия и sha256 за один проход: sensor.parquet не читается дважды."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        while chunk := fin.read(CHUNK):
            digest.update(chunk)
            fout.write(chunk)
            size += len(chunk)
    shutil.copystat(src, dst)
    return size, digest.hexdigest()


def write_manifest(staging: Path, entries: list[Entry]) -> Path:
    path = staging / MANIFEST
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for e in sorted(entries, key=lambda e: e.rel):
            out.write(f"{e.sha256}  {e.rel}\n")
    return path


def build(source: Path, out: Path, version: str) -> tuple[Path, list[Entry], list[str]]:
    """Собрать <out>/<version>/ с раскладкой C4 и MANIFEST.sha256."""
    if not VERSION_RE.fullmatch(version):
        raise BundleError(f"версия {version!r} не в формате bundle-YYYYMMDD-N")
    files, absent = select_files(source)
    staging = out / version
    if staging.exists() and any(staging.iterdir()):
        raise BundleError(f"{staging} уже существует и не пуст; возьмите следующий "
                          f"номер версии или удалите каталог")
    staging.mkdir(parents=True, exist_ok=True)
    entries = []
    for rel in files:
        size, digest = copy_hashed(source / rel, staging / rel)
        entries.append(Entry(rel, size, digest))
    missing_layout = [rel for rel in LAYOUT if not (staging / rel).exists()]
    if missing_layout:
        raise BundleError(f"в {staging} не сложилась раскладка C4: {missing_layout}")
    write_manifest(staging, entries)
    return staging, entries, absent


def find_7z(explicit: str | None = None) -> str:
    if explicit:
        found = shutil.which(explicit)
        if not found:
            raise BundleError(f"--7z {explicit}: такого исполняемого файла нет")
        return found
    for name in SEVEN_ZIP:
        found = shutil.which(name)
        if found:
            return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base and (Path(base) / "7-Zip" / "7z.exe").is_file():
            return str(Path(base) / "7-Zip" / "7z.exe")
    raise BundleError("нет 7z: поставьте p7zip-full или 7zip (Linux), 7-Zip (Windows) "
                      "или укажите --7z")


def archive_command(seven_zip: str, archive: Path, level: int,
                    password: str | None) -> list[str]:
    """Команда упаковки. Пароль ключом -p; пустой -p велит 7z спросить его."""
    return [seven_zip, "a", "-t7z", "-mhe=on", f"-mx={level}", "-bd",
            f"-p{password}" if password else "-p", str(archive), "."]


def masked(cmd: list[str]) -> str:
    return " ".join("-p***" if part.startswith("-p") and part != "-p" else part
                    for part in cmd)


def archive_path(staging: Path) -> Path:
    return staging.parent / f"{staging.name}.7z"


def check_archive_free(archive: Path) -> None:
    if archive.exists():
        raise BundleError(f"{archive} уже существует; 7z дописал бы в него, а не "
                          "заменил. Удалите файл или возьмите следующую версию")


def make_archive(staging: Path, seven_zip: str, level: int) -> Path:
    archive = archive_path(staging)
    check_archive_free(archive)
    password = os.environ.get(PASSWORD_ENV) or None
    cmd = archive_command(seven_zip, archive.resolve(), level, password)
    print(f"упаковка: {masked(cmd)}", flush=True)
    if password is None:
        print(f"{PASSWORD_ENV} не задан — 7z спросит пароль дважды", flush=True)
    # Архив пишется из корня бандла: внутри лежат data/, models/, configs/,
    # reports/ и MANIFEST.sha256 без каталога версии, как ждёт fetch_bundle.
    rc = subprocess.call(cmd, cwd=staging)
    if rc != 0:
        raise BundleError(f"7z завершился с кодом {rc}; архив {archive} не готов")
    return archive


def human(size: int) -> str:
    return f"{size / 1024 ** 2:,.1f} МБ".replace(",", " ")


def report(source: Path, staging: Path | None, entries: list[Entry],
           absent: list[str]) -> None:
    width = max(len(e.rel) for e in entries)
    for e in entries:
        print(f"  {e.rel:<{width}}  {human(e.size):>12}")
    print(f"  {'итого, файлов ' + str(len(entries)):<{width}}  "
          f"{human(sum(e.size for e in entries)):>12}")
    for rel in absent:
        print(f"  нет необязательного {rel}: {OPTIONAL[rel]}")
    print(f"источник: {source}")
    if staging is not None:
        print(f"бандл: {staging}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        description="Бандл C4: файлы режима real, MANIFEST.sha256 и архив .7z.")
    p.add_argument("--source", type=Path, default=ML_ROOT,
                   help="корень ML-проекта с data/, models/, configs/, reports/ "
                        f"(по умолчанию {ML_ROOT})")
    p.add_argument("--out", type=Path, default=ML_ROOT.parent / "dist",
                   help="куда положить <версия>/ и <версия>.7z (по умолчанию ../dist)")
    p.add_argument("--version", default=default_version(),
                   help="bundle-YYYYMMDD-N (по умолчанию сегодняшний день, номер 1)")
    p.add_argument("--dry-run", action="store_true",
                   help="только проверить состав и показать размеры, ничего не копировать")
    p.add_argument("--archive", action="store_true",
                   help=f"упаковать в <версия>.7z; пароль из {PASSWORD_ENV} или с клавиатуры")
    p.add_argument("--level", type=int, default=1, choices=range(10), metavar="0-9",
                   help="уровень сжатия 7z (-mx); parquet уже сжат zstd, по умолчанию 1")
    p.add_argument("--7z", dest="seven_zip", help="путь к 7z, если его нет в PATH")
    args = p.parse_args(argv)
    source = args.source.resolve()
    try:
        if args.dry_run:
            files, absent = select_files(source)
            entries = [Entry(rel, (source / rel).stat().st_size) for rel in files]
            report(source, None, entries, absent)
            print("проверка состава пройдена, ничего не скопировано (--dry-run)")
            return 0
        out = args.out.resolve()
        # 7z и место под архив проверяются до копирования: иначе гигабайты
        # скопировались бы зря.
        seven_zip = None
        if args.archive:
            seven_zip = find_7z(args.seven_zip)
            check_archive_free(archive_path(out / args.version))
        staging, entries, absent = build(source, out, args.version)
        report(source, staging, entries, absent)
        print(f"манифест: {staging / MANIFEST}")
        if seven_zip:
            archive = make_archive(staging, seven_zip, args.level)
            print(f"архив: {archive}  {human(archive.stat().st_size)}")
            print(f"проверка на этой машине: sh scripts/fetch_bundle.sh {archive} <каталог>")
        else:
            print(f"проверка: sh scripts/fetch_bundle.sh {staging}")
    except BundleError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
