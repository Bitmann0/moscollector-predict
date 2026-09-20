"""Граф стадий: что от чего зависит и что устарело.

Оркестратора здесь намеренно нет. Airflow, Dagster и подобное решают задачу
распределённого расписания, которой у нас не стоит: один репозиторий, батчевый
расчёт раз в сутки, всё на одной машине. Их цена — отдельная инфраструктура,
которую принимающей команде придётся поднимать и понимать. Для нашего случая
достаточно объявленного графа с проверкой свежести по времени файлов.

Устаревание определяется просто: стадия устарела, если хоть один её вход новее
любого её выхода. Этого хватает, потому что все выходы у нас — файлы, а все
входы либо файлы, либо код. Хеши содержимого были бы строже, но на 4 ГБ
parquet считались бы дольше самой стадии.

Стадия `ingest` дополнительно сверяется с кодом приёма: правка дедупликации
меняет данные, и без этой связи пересборку пришлось бы помнить руками.
"""
import dataclasses
import datetime as dt
import subprocess
import sys
from pathlib import Path

from .config import PATHS

ROOT = PATHS.root
SRC = ROOT / "src" / "mkl"


@dataclasses.dataclass(frozen=True)
class Stage:
    name: str
    title: str
    command: list[str]
    inputs: list[Path]
    outputs: list[Path]
    needs: tuple[str, ...] = ()
    note: str = ""

    def newest_input(self) -> float:
        times = [p.stat().st_mtime for p in self.inputs if p.exists()]
        return max(times) if times else 0.0

    def oldest_output(self) -> float | None:
        if not self.outputs:
            return None
        if any(not p.exists() for p in self.outputs):
            return None
        return min(p.stat().st_mtime for p in self.outputs)

    def state(self) -> str:
        out = self.oldest_output()
        if out is None:
            return "нет результата"
        return "устарела" if self.newest_input() > out else "свежая"


def _glob(pattern: str, base: Path) -> list[Path]:
    return sorted(base.glob(pattern))


def stages() -> list[Stage]:
    raw, interim, feat = PATHS.raw, PATHS.interim, PATHS.features
    py = [sys.executable]
    return [
        Stage(
            name="ingest", title="Приём журнала и справочников",
            command=py + ["-m", "mkl.ingest"],
            inputs=_glob("*.csv", raw) + _glob("*.csv", PATHS.materials)
                   + [SRC / "ingest.py"],
            outputs=_glob("events_year=*.parquet", interim)
                    + [interim / "channels.parquet"],
            note="дедупликация по полному кортежу; детерминирован",
        ),
        Stage(
            name="states", title="Эпизоды и групповые отказы",
            command=py + [str(ROOT / "scripts" / "build_states.py")],
            inputs=_glob("events_year=*.parquet", interim) + [SRC / "states.py"],
            outputs=[interim / "episodes.parquet", interim / "group_outages.parquet"],
            needs=("ingest",),
        ),
        Stage(
            name="panel", title="Суточная панель канал × сутки",
            command=py + [str(ROOT / "scripts" / "build_panel.py")],
            inputs=_glob("events_year=*.parquet", interim) + [SRC / "panel.py"],
            outputs=[interim / "daily_channel.parquet"],
            needs=("ingest",),
        ),
        Stage(
            name="weather", title="История погоды Москвы",
            command=py + [str(ROOT / "scripts" / "fetch_weather.py")],
            inputs=[SRC / "features" / "external.py"],
            outputs=[interim / "weather.parquet"],
            note="внешний источник; при недоступности сети колонки остаются пустыми",
        ),
        Stage(
            name="features", title="Фичестор",
            command=py + [str(ROOT / "scripts" / "build_features.py")],
            inputs=[interim / "daily_channel.parquet", interim / "episodes.parquet",
                    interim / "group_outages.parquet", interim / "weather.parquet"]
                   + _glob("*.py", SRC / "features"),
            outputs=[feat / "sensor.parquet", feat / "object.parquet",
                     feat / "segment.parquet"],
            needs=("states", "panel", "weather"),
        ),
        Stage(
            name="train", title="Обучение голов и отложенный замер",
            command=py + [str(ROOT / "scripts" / "final_eval.py")],
            inputs=[feat / "sensor.parquet", feat / "object.parquet",
                    feat / "segment.parquet", ROOT / "configs" / "heads.yaml",
                    SRC / "train.py", SRC / "labels.py"],
            outputs=_glob("*.pkl", PATHS.models),
            needs=("features",),
            note="каждый запуск — ещё один просмотр отложенного периода",
        ),
    ]


def by_name() -> dict[str, Stage]:
    return {s.name: s for s in stages()}


def status() -> list[dict]:
    return [{"stage": s.name, "title": s.title, "state": s.state(),
             "outputs": len(s.outputs), "note": s.note} for s in stages()]


def plan(target: str | None = None, force: bool = False) -> list[Stage]:
    """Какие стадии надо выполнить, чтобы получить target, в правильном порядке.

    Стадия попадает в план, если устарела сама или если в план попал хоть один
    её предшественник: пересборка входа обесценивает выход.
    """
    reg = by_name()
    order = [s.name for s in stages()]
    want = order if target is None else _with_deps(target, reg)
    chosen: list[Stage] = []
    dirty: set[str] = set()
    for name in order:
        if name not in want:
            continue
        s = reg[name]
        if force or s.state() != "свежая" or dirty & set(s.needs):
            chosen.append(s)
            dirty.add(name)
    return chosen


def _with_deps(target: str, reg: dict[str, Stage]) -> set[str]:
    out, stack = set(), [target]
    while stack:
        name = stack.pop()
        if name in out:
            continue
        out.add(name)
        stack.extend(reg[name].needs)
    return out


def run(stages_to_run: list[Stage], dry_run: bool = False) -> int:
    for s in stages_to_run:
        print(f"\n=== {s.name}: {s.title} ===", flush=True)
        if dry_run:
            print("  " + " ".join(s.command), flush=True)
            continue
        t0 = dt.datetime.now()
        rc = subprocess.call(s.command, cwd=str(ROOT))
        took = (dt.datetime.now() - t0).total_seconds()
        if rc != 0:
            print(f"  стадия {s.name} завершилась с кодом {rc} за {took:.0f} с",
                  flush=True)
            return rc
        print(f"  готово за {took:.0f} с", flush=True)
    return 0
