# Предиктивный ML-пайплайн «Москоллектор» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Построить воспроизводимый ML-пайплайн, прогнозирующий отказы датчиков, массовые отказы объектов, пожарный риск, несанкционированный доступ и износ агрегатов по журналу событий СМВУ, с фичестором, защищённым от утечки, и журналом экспериментов.

**Architecture:** Один бэкбон — события → суточная панель → фичестор → пять голов моделей. Тяжёлые агрегаты считает DuckDB на Parquet; Python держит только оркестрацию, разметку, валидацию и обучение. Единственная функция расчёта фич используется и на обучении, и на инференсе — это защита от train/serve skew.

**Tech Stack:** Python 3.12, DuckDB 1.5, Polars 1.44, LightGBM, CatBoost, scikit-learn, PyYAML. Окружение: `U:\hackathon\.venv`.

## Global Constraints

- Питон только из `U:\hackathon\.venv\Scripts\python.exe`. Системный Python пуст.
- Все пути абсолютные. Рабочая директория `U:\hackathon`.
- Исходники не трогать: `U:\hackathon\Materials\` доступна только на чтение.
- `data/raw`, `data/interim`, `data/features`, `data/tmp`, `.venv` — в `.gitignore`, в git не попадают.
- DuckDB всегда с `SET memory_limit='10GB'; SET threads=8; SET temp_directory='U:\hackathon\data\tmp';` — без этого падает на 313 млн строк.
- Отложенный период **2026-01-01 … 2026-06-30** не используется ни в одном эксперименте до Task 14. Любое обращение к нему раньше — ошибка плана.
- Embargo = **31 сутки** (горизонт 24 ч + максимальное окно фич 30 суток). Для головы D (горизонт 7 суток) embargo = **37 суток**.
- Запрещено: SMOTE и любой oversampling; протокол point adjustment; ROC-AUC как основная метрика.
- Дисбаланс лечится только `scale_pos_weight` → изотоническая калибровка → подбор порога.
- Каждый тест запускается как `& U:\hackathon\.venv\Scripts\python.exe -m pytest <path> -v`.
- Кодировка всех файлов UTF-8, в скриптах с выводом — `sys.stdout.reconfigure(encoding='utf-8')`.

## Словарь состояний (используется во всех задачах)

```
BAD_STATES   = Неисправен, Неопределен, Обесточен, Отключено устройство,
               Батарея неисправна, Батарея разряжена
OK_STATES    = Норма, Есть питание, Устройства на объекте исправны, Питание от сети
FIRE_STATES  = Обнаружен дым, Обнаружен газ, Температура выше 40ºC
INTRUSION    = Обнаружено движение, Не замкнут, Рычаг сдернут, Разбито стекло
WEAR_STATES  = Затоплен, Работают все насосы в АНС
ARM_STATES   = На охране, Снято с охраны
```

## Структура файлов

| Файл | Ответственность |
|---|---|
| `src/mkl/config.py` | пути, словари состояний, константы окон и горизонтов |
| `src/mkl/db.py` | фабрика подключения DuckDB с обязательными настройками |
| `src/mkl/ingest.py` | CSV → Parquet, дедупликация, join справочников, отчёт качества |
| `src/mkl/states.py` | эпизоды состояний, групповые окна отказов |
| `src/mkl/panel.py` | суточная панель канал × сутки |
| `src/mkl/labels.py` | пять построителей меток |
| `src/mkl/features/base.py` | оконные агрегаты ISA-18.2, ритм канала |
| `src/mkl/features/relative.py` | peer-relative, пространственные |
| `src/mkl/features/lifecycle.py` | возраст, износ, контекст |
| `src/mkl/features/external.py` | погода, календарь |
| `src/mkl/features/compute.py` | единая точка входа расчёта фич |
| `src/mkl/store.py` | фичестор: запись, реестр, чтение среза |
| `src/mkl/cv.py` | walk-forward с purge/embargo |
| `src/mkl/metrics.py` | PR-AUC, Precision@k, event-wise P/R, калибровка |
| `src/mkl/train.py` | обучение одной головы |
| `src/mkl/experiments.py` | журнал экспериментов |
| `src/mkl/serve.py` | скоринг, снимок последних фич |
| `configs/features.yaml` | реестр фич |
| `configs/heads.yaml` | описание пяти голов |
| `experiments/log.jsonl` | журнал прогонов |

---

### Task 1: Каркас, конфиг, подключение к БД

**Files:**
- Create: `src/mkl/__init__.py`, `src/mkl/config.py`, `src/mkl/db.py`
- Create: `tests/test_config.py`, `tests/test_db.py`
- Create: `pyproject.toml`

**Interfaces:**
- Produces: `config.PATHS` (dataclass с полями `raw`, `interim`, `features`, `tmp`, `materials`), `config.BAD_STATES`/`OK_STATES`/`FIRE_STATES`/`INTRUSION_STATES`/`WEAR_STATES`/`ARM_STATES` (frozenset), `config.HOLDOUT_START`/`HOLDOUT_END` (date), `config.EMBARGO_DAYS` (int), `db.connect() -> duckdb.DuckDBPyConnection`

- [ ] **Step 1: Написать падающий тест на конфиг**

```python
# tests/test_config.py
from datetime import date
from mkl import config


def test_paths_exist():
    assert config.PATHS.materials.exists()
    assert config.PATHS.interim.exists()


def test_state_vocabularies_are_disjoint():
    assert not (config.BAD_STATES & config.OK_STATES)
    assert not (config.FIRE_STATES & config.INTRUSION_STATES)


def test_holdout_is_2026_h1():
    assert config.HOLDOUT_START == date(2026, 1, 1)
    assert config.HOLDOUT_END == date(2026, 6, 30)


def test_embargo_covers_horizon_plus_window():
    assert config.EMBARGO_DAYS >= 1 + config.MAX_FEATURE_WINDOW_DAYS
```

- [ ] **Step 2: Запустить тест, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_config.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl'`

- [ ] **Step 3: Написать pyproject.toml**

```toml
[project]
name = "mkl"
version = "0.1.0"
requires-python = ">=3.12"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 4: Написать config.py**

```python
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(r"U:\hackathon")


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    materials: Path = ROOT / "Materials"
    raw: Path = ROOT / "data" / "raw"
    interim: Path = ROOT / "data" / "interim"
    features: Path = ROOT / "data" / "features"
    tmp: Path = ROOT / "data" / "tmp"
    experiments: Path = ROOT / "experiments"
    models: Path = ROOT / "models"

    def ensure(self) -> None:
        for p in (self.raw, self.interim, self.features, self.tmp,
                  self.experiments, self.models):
            p.mkdir(parents=True, exist_ok=True)


PATHS = Paths()
PATHS.ensure()

BAD_STATES = frozenset({
    "Неисправен", "Неопределен", "Обесточен", "Отключено устройство",
    "Батарея неисправна", "Батарея разряжена",
})
OK_STATES = frozenset({
    "Норма", "Есть питание", "Устройства на объекте исправны", "Питание от сети",
})
FIRE_STATES = frozenset({"Обнаружен дым", "Обнаружен газ", "Температура выше 40ºC"})
INTRUSION_STATES = frozenset({
    "Обнаружено движение", "Не замкнут", "Рычаг сдернут", "Разбито стекло",
})
WEAR_STATES = frozenset({"Затоплен", "Работают все насосы в АНС"})
ARM_STATES = frozenset({"На охране", "Снято с охраны"})

EQUIPMENT_STYPES = frozenset({
    "Состояние насоса", "Состояние вентилятора", "ИБП",
    "Датчик затопления", "КД Люк", "9-секционный люк",
})

HOLDOUT_START = date(2026, 1, 1)
HOLDOUT_END = date(2026, 6, 30)

MAX_FEATURE_WINDOW_DAYS = 30
EMBARGO_DAYS = 1 + MAX_FEATURE_WINDOW_DAYS
EMBARGO_DAYS_WEAR = 7 + MAX_FEATURE_WINDOW_DAYS

MIN_FAILURE_DURATION_S = 3600
GROUP_OUTAGE_MIN_CHANNELS = 4
GROUP_OUTAGE_WINDOW_MIN = 5
```

- [ ] **Step 5: Написать db.py**

```python
import duckdb

from .config import PATHS


def connect(memory_limit: str = "10GB", threads: int = 8) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET threads={threads}")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{PATHS.tmp}'")
    return con


def attach_events(con: duckdb.DuckDBPyConnection, view: str = "ev") -> None:
    pattern = str(PATHS.interim / "events_year=*.parquet")
    con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{pattern}')")
```

- [ ] **Step 6: Написать тест на db.py**

```python
# tests/test_db.py
from mkl import db


def test_connect_applies_settings():
    con = db.connect()
    limit = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert "GB" in limit
    con.close()


def test_attach_events_exposes_expected_columns():
    con = db.connect()
    db.attach_events(con)
    cols = {r[0] for r in con.execute("DESCRIBE ev").fetchall()}
    assert {"event_id", "ch", "ts", "day", "alarm", "val_raw", "val_num",
            "sys", "stype", "obj", "picket"} <= cols
    con.close()
```

- [ ] **Step 7: Запустить все тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pip install -e . -q; & U:\hackathon\.venv\Scripts\python.exe -m pytest tests -v`
Expected: PASS, 6 тестов

- [ ] **Step 8: Коммит**

```bash
git add pyproject.toml src tests
git commit -m "feat: каркас проекта, конфиг состояний, подключение DuckDB"
```

---

### Task 2: Ingest и контроль качества

**Files:**
- Create: `src/mkl/ingest.py`, `tests/test_ingest.py`
- Create: `reports/data_quality.md`

**Interfaces:**
- Consumes: `config.PATHS`, `db.connect`
- Produces: `ingest.build_channels() -> None`, `ingest.build_events(years: list[int]) -> dict[int, int]`, `ingest.quality_report() -> dict`

Приём уже выполнен разово, но обязан быть воспроизводимым скриптом: жюри и прод требуют повторяемости.

- [ ] **Step 1: Написать падающий тест на парсинг пикета и объекта**

```python
# tests/test_ingest.py
import pytest
from mkl import ingest


@pytest.mark.parametrize("name,expected", [
    ("Темп. ВШ ПК88,5", 88.5),
    ("ТД ПК86-85", 86.0),
    ("КД АВ ПК28", 28.0),
    ("[Охранная зона]0: 0: ", None),
])
def test_parse_picket(name, expected):
    assert ingest.parse_picket(name) == expected


@pytest.mark.parametrize("tag,expected", [
    ("15-11.1.131.2.", "15-11"),
    ("847-1.1.4096.4095.", "847-1"),
])
def test_parse_object(tag, expected):
    assert ingest.parse_object(tag) == expected
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_ingest.py -v`
Expected: FAIL с `AttributeError: module 'mkl.ingest' has no attribute 'parse_picket'`

- [ ] **Step 3: Реализовать ingest.py**

```python
import re
import sys

from .config import PATHS
from .db import connect

_PICKET_RE = re.compile(r"ПК\s*(\d+(?:[.,]\d+)?)")


def parse_picket(name: str | None) -> float | None:
    if not name:
        return None
    m = _PICKET_RE.search(name)
    return float(m.group(1).replace(",", ".")) if m else None


def parse_object(tag: str | None) -> str | None:
    return tag.split(".")[0] if tag else None


def build_channels() -> None:
    con = connect()
    src = PATHS.materials / "справочник_каналов_датчиков.csv"
    con.execute(f"""
        COPY (
          SELECT TRY_CAST(ид_канала_данных AS BIGINT) AS ch,
                 тип_инж_системы AS sys,
                 тип_датчика     AS stype,
                 тег_инженерной_системы AS tag,
                 название_датчика AS sname,
                 split_part(тег_инженерной_системы,'.',1) AS obj,
                 TRY_CAST(replace(regexp_extract(название_датчика,'ПК\\s*(\\d+(?:[.,]\\d+)?)',1),',','.') AS DOUBLE) AS picket
          FROM read_csv('{src}', header=true)
        ) TO '{PATHS.interim / "channels.parquet"}' (FORMAT PARQUET)
    """)
    con.close()


def build_events(years: list[int]) -> dict[int, int]:
    con = connect()
    con.execute(f"CREATE TABLE ref AS SELECT * FROM read_parquet('{PATHS.interim / 'channels.parquet'}')")
    counts: dict[int, int] = {}
    for y in years:
        src = PATHS.raw / f"ext-journal-{y}.csv"
        dst = PATHS.interim / f"events_year={y}.parquet"
        con.execute(f"""
        COPY (
          SELECT DISTINCT ON (e.event_id)
                 e.event_id, e.ch, (e.d + e.t) AS ts, e.d AS day, e.alarm,
                 e.val AS val_raw, TRY_CAST(e.val AS DOUBLE) AS val_num,
                 r.sys, r.stype, r.tag, r.sname, r.obj, r.picket
          FROM (
            SELECT TRY_CAST(ид_события AS BIGINT) AS event_id,
                   TRY_CAST(ид_канала_данных AS BIGINT) AS ch,
                   TRY_CAST(дата AS DATE) AS d,
                   TRY_CAST(время AS TIME) AS t,
                   lower(тревожное) IN ('t','true') AS alarm,
                   значение_датчика AS val
            FROM read_csv('{src}', all_varchar=true, header=true,
                 columns={{'ид_события':'VARCHAR','ид_канала_данных':'VARCHAR','дата':'VARCHAR','время':'VARCHAR','тревожное':'VARCHAR','значение_датчика':'VARCHAR'}})
            WHERE ид_события <> 'ид_события'
          ) e
          LEFT JOIN ref r ON r.ch = e.ch
          WHERE e.event_id IS NOT NULL AND e.ch IS NOT NULL AND e.d IS NOT NULL
        ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 1000000)
        """)
        counts[y] = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
    con.close()
    return counts


def quality_report() -> dict:
    from .db import attach_events
    con = connect()
    attach_events(con)
    rows = con.execute("""
        SELECT year(day) AS y, count(*) AS events, count(DISTINCT ch) AS channels,
               count(*) FILTER (WHERE stype IS NULL) AS unmapped,
               count(*) FILTER (WHERE alarm) AS alarms
        FROM ev GROUP BY 1 ORDER BY 1
    """).fetchall()
    con.close()
    return {"by_year": rows}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    build_channels()
    print(build_events(list(range(2019, 2027))))
    print(quality_report())
```

- [ ] **Step 4: Запустить тест, убедиться что проходит**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_ingest.py -v`
Expected: PASS, 6 тестов

- [ ] **Step 5: Прогнать ingest целиком и записать отчёт качества**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m mkl.ingest`
Expected: словарь с 8 годами, суммарно 307 519 938 строк ±0,1%. Записать вывод в `reports/data_quality.md` с колонками год, события, каналы, без справочника, тревоги.

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/ingest.py tests/test_ingest.py reports/data_quality.md
git commit -m "feat: воспроизводимый ingest с дедупликацией и отчётом качества"
```

---

### Task 3: Эпизоды состояний и групповые окна

**Files:**
- Create: `src/mkl/states.py`, `tests/test_states.py`

**Interfaces:**
- Consumes: `config.BAD_STATES`, `config.GROUP_OUTAGE_MIN_CHANNELS`, `config.GROUP_OUTAGE_WINDOW_MIN`, `db.connect`, `db.attach_events`
- Produces: `states.build_episodes(con) -> None` создаёт таблицу `episodes(ch, obj, stype, t_start, t_end, dur_s, n_events, states, is_group)`; `states.build_group_outages(con) -> None` создаёт `group_outages(obj, t_start, t_end, n_channels)`

Эпизод — непрерывный прогон событий канала в состояниях `BAD_STATES` до первого события вне их. `is_group` помечает эпизоды, стартовавшие внутри окна группового отказа своего объекта.

- [ ] **Step 1: Написать падающий тест на склейку эпизодов**

```python
# tests/test_states.py
import duckdb
import pytest

from mkl import states


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE ev (event_id BIGINT, ch BIGINT, ts TIMESTAMP, day DATE,
                         alarm BOOLEAN, val_raw VARCHAR, val_num DOUBLE,
                         sys VARCHAR, stype VARCHAR, tag VARCHAR, sname VARCHAR,
                         obj VARCHAR, picket DOUBLE)
    """)
    yield c
    c.close()


def _ins(c, rows):
    for i, (ch, ts, val, obj) in enumerate(rows):
        c.execute(
            "INSERT INTO ev VALUES (?,?,?,?,false,?,NULL,'s','Датчик дыма','t','n',?,1.0)",
            [i, ch, ts, ts[:10], val, obj],
        )


def test_consecutive_bad_states_merge_into_one_episode(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", "A"),
        (1, "2025-01-01 11:00:00", "Неопределен", "A"),
        (1, "2025-01-01 12:00:00", "Обесточен", "A"),
        (1, "2025-01-01 14:00:00", "Норма", "A"),
    ])
    states.build_episodes(con)
    ep = con.execute("SELECT ch, dur_s, n_events FROM episodes").fetchall()
    assert ep == [(1, 3600, 2)]


def test_episode_broken_by_normal_state(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неисправен", "A"),
        (1, "2025-01-01 10:30:00", "Норма", "A"),
        (1, "2025-01-01 11:00:00", "Неисправен", "A"),
    ])
    states.build_episodes(con)
    assert con.execute("SELECT count(*) FROM episodes").fetchone()[0] == 2


def test_group_outage_flags_simultaneous_channels(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неопределен", "A"),
        (2, "2025-01-01 10:01:00", "Неопределен", "A"),
        (3, "2025-01-01 10:02:00", "Неопределен", "A"),
        (4, "2025-01-01 10:03:00", "Неопределен", "A"),
        (9, "2025-02-01 10:00:00", "Неопределен", "B"),
    ])
    states.build_episodes(con)
    states.build_group_outages(con)
    states.mark_group_episodes(con)
    grouped = con.execute("SELECT ch FROM episodes WHERE is_group ORDER BY ch").fetchall()
    assert grouped == [(1,), (2,), (3,), (4,)]
    solo = con.execute("SELECT ch FROM episodes WHERE NOT is_group").fetchall()
    assert solo == [(9,)]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_states.py -v`
Expected: FAIL с `AttributeError: module 'mkl.states' has no attribute 'build_episodes'`

- [ ] **Step 3: Реализовать states.py**

```python
import duckdb

from .config import BAD_STATES, GROUP_OUTAGE_MIN_CHANNELS, GROUP_OUTAGE_WINDOW_MIN


def _bad_sql() -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(BAD_STATES)) + ")"


def build_episodes(con: duckdb.DuckDBPyConnection, source: str = "ev") -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE episodes AS
    WITH s AS (
      SELECT ch, obj, stype, ts, val_raw,
             CASE WHEN val_raw IN {_bad_sql()} THEN 1 ELSE 0 END AS bad
      FROM {source}
      WHERE val_num IS NULL
    ), g AS (
      SELECT *,
             row_number() OVER (PARTITION BY ch ORDER BY ts)
           - row_number() OVER (PARTITION BY ch, bad ORDER BY ts) AS grp
      FROM s
    )
    SELECT ch,
           any_value(obj)   AS obj,
           any_value(stype) AS stype,
           min(ts)          AS t_start,
           max(ts)          AS t_end,
           CAST(epoch(max(ts)) - epoch(min(ts)) AS BIGINT) AS dur_s,
           count(*)         AS n_events,
           string_agg(DISTINCT val_raw, '|') AS states,
           false            AS is_group
    FROM g WHERE bad = 1
    GROUP BY ch, grp
    """)


def build_group_outages(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE group_outages AS
    WITH bucketed AS (
      SELECT obj,
             CAST(epoch(t_start) AS BIGINT) / ({GROUP_OUTAGE_WINDOW_MIN} * 60) AS bucket,
             ch, t_start
      FROM episodes WHERE obj IS NOT NULL
    )
    SELECT obj, bucket,
           min(t_start) AS t_start, max(t_start) AS t_end,
           count(DISTINCT ch) AS n_channels
    FROM bucketed
    GROUP BY obj, bucket
    HAVING count(DISTINCT ch) >= {GROUP_OUTAGE_MIN_CHANNELS}
    """)


def mark_group_episodes(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE episodes AS
    SELECT e.* REPLACE (
      EXISTS (
        SELECT 1 FROM group_outages o
        WHERE o.obj = e.obj
          AND CAST(epoch(e.t_start) AS BIGINT) / ({GROUP_OUTAGE_WINDOW_MIN} * 60) = o.bucket
      ) AS is_group
    )
    FROM episodes e
    """)
```

- [ ] **Step 4: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_states.py -v`
Expected: PASS, 3 теста

- [ ] **Step 5: Прогнать на реальных данных и записать статистику**

```python
# scripts/build_states.py
import sys
from mkl import db, states
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")
con = db.connect()
db.attach_events(con)
states.build_episodes(con)
states.build_group_outages(con)
states.mark_group_episodes(con)
con.execute(f"COPY episodes TO '{PATHS.interim / 'episodes.parquet'}' (FORMAT PARQUET)")
con.execute(f"COPY group_outages TO '{PATHS.interim / 'group_outages.parquet'}' (FORMAT PARQUET)")
print(con.execute("""
  SELECT is_group, count(*) episodes, count(DISTINCT ch) channels,
         count(*) FILTER (WHERE dur_s >= 3600) long_episodes
  FROM episodes GROUP BY 1
""").df().to_string(index=False))
con.close()
```

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/build_states.py`
Expected: одиночных длинных эпизодов заметно меньше групповых — это подтверждает находку Ф2 спеки.

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/states.py tests/test_states.py scripts/build_states.py
git commit -m "feat: реконструкция эпизодов состояний и выделение групповых отказов"
```

---

### Task 4: Суточная панель

**Files:**
- Create: `src/mkl/panel.py`, `tests/test_panel.py`, `scripts/build_panel.py`

**Interfaces:**
- Consumes: `db.connect`, `db.attach_events`
- Produces: `panel.build_daily_channel(con) -> None` создаёт `daily_channel` с ключом `(ch, day)` и колонками: `n_events, n_alarms, n_bad, n_ok, n_fire, n_intrusion, n_transitions, n_chatter_1min, max_gap_s, med_gap_s, val_min, val_max, val_mean, val_std, obj, stype, sys, picket`

Панель — узкое место всей системы: 11,5 тыс. каналов × 1300 суток ≈ 15 млн строк, всё дальнейшее считается уже по ней, а не по 313 млн событий.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_panel.py
import duckdb
import pytest

from mkl import panel


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""
        CREATE TABLE ev (event_id BIGINT, ch BIGINT, ts TIMESTAMP, day DATE,
                         alarm BOOLEAN, val_raw VARCHAR, val_num DOUBLE,
                         sys VARCHAR, stype VARCHAR, tag VARCHAR, sname VARCHAR,
                         obj VARCHAR, picket DOUBLE)
    """)
    yield c
    c.close()


def _ins(c, rows):
    for i, (ch, ts, val, alarm, num) in enumerate(rows):
        c.execute(
            "INSERT INTO ev VALUES (?,?,?,?,?,?,?,'s','Датчик дыма','t','n','A',1.0)",
            [i, ch, ts, ts[:10], alarm, val, num],
        )


def test_counts_events_and_alarms_per_day(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", False, None),
        (1, "2025-01-01 11:00:00", "Неисправен", True, None),
        (1, "2025-01-02 10:00:00", "Норма", False, None),
    ])
    panel.build_daily_channel(con)
    got = con.execute(
        "SELECT day, n_events, n_alarms, n_bad FROM daily_channel ORDER BY day"
    ).fetchall()
    assert got[0][1:] == (2, 1, 1)
    assert got[1][1:] == (1, 0, 0)


def test_chatter_counts_three_events_within_one_minute(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Неисправен", True, None),
        (1, "2025-01-01 10:00:20", "Норма", False, None),
        (1, "2025-01-01 10:00:40", "Неисправен", True, None),
    ])
    panel.build_daily_channel(con)
    assert con.execute("SELECT n_chatter_1min FROM daily_channel").fetchone()[0] >= 1


def test_numeric_aggregates(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "0.10", False, 0.10),
        (1, "2025-01-01 11:00:00", "0.30", False, 0.30),
    ])
    panel.build_daily_channel(con)
    row = con.execute("SELECT val_min, val_max, val_mean FROM daily_channel").fetchone()
    assert row[0] == pytest.approx(0.10)
    assert row[1] == pytest.approx(0.30)
    assert row[2] == pytest.approx(0.20)


def test_max_gap_is_computed_within_day(con):
    _ins(con, [
        (1, "2025-01-01 10:00:00", "Норма", False, None),
        (1, "2025-01-01 12:00:00", "Норма", False, None),
    ])
    panel.build_daily_channel(con)
    assert con.execute("SELECT max_gap_s FROM daily_channel").fetchone()[0] == 7200
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_panel.py -v`
Expected: FAIL с `AttributeError: module 'mkl.panel' has no attribute 'build_daily_channel'`

- [ ] **Step 3: Реализовать panel.py**

```python
import duckdb

from .config import BAD_STATES, FIRE_STATES, INTRUSION_STATES, OK_STATES


def _in(states: frozenset[str]) -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(states)) + ")"


def build_daily_channel(con: duckdb.DuckDBPyConnection, source: str = "ev") -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE daily_channel AS
    WITH e AS (
      SELECT ch, day, ts, alarm, val_raw, val_num, obj, stype, sys, picket,
             epoch(ts) - lag(epoch(ts)) OVER (PARTITION BY ch, day ORDER BY ts) AS gap_s,
             epoch(ts) - lag(epoch(ts), 2) OVER (PARTITION BY ch, day ORDER BY ts) AS gap3_s,
             CASE WHEN val_raw IS DISTINCT FROM
                  lag(val_raw) OVER (PARTITION BY ch, day ORDER BY ts)
                  THEN 1 ELSE 0 END AS is_transition
      FROM {source}
    )
    SELECT ch, day,
           any_value(obj) AS obj, any_value(stype) AS stype,
           any_value(sys) AS sys, any_value(picket) AS picket,
           count(*)                                     AS n_events,
           count(*) FILTER (WHERE alarm)                AS n_alarms,
           count(*) FILTER (WHERE val_raw IN {_in(BAD_STATES)})       AS n_bad,
           count(*) FILTER (WHERE val_raw IN {_in(OK_STATES)})        AS n_ok,
           count(*) FILTER (WHERE val_raw IN {_in(FIRE_STATES)})      AS n_fire,
           count(*) FILTER (WHERE val_raw IN {_in(INTRUSION_STATES)}) AS n_intrusion,
           sum(is_transition)                           AS n_transitions,
           count(*) FILTER (WHERE gap3_s IS NOT NULL AND gap3_s <= 60) AS n_chatter_1min,
           CAST(coalesce(max(gap_s), 0) AS BIGINT)      AS max_gap_s,
           median(gap_s)                                AS med_gap_s,
           min(val_num) AS val_min, max(val_num) AS val_max,
           avg(val_num) AS val_mean, stddev_pop(val_num) AS val_std
    FROM e
    GROUP BY ch, day
    """)
```

- [ ] **Step 4: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_panel.py -v`
Expected: PASS, 4 теста

- [ ] **Step 5: Построить панель на реальных данных**

```python
# scripts/build_panel.py
import sys, time
from mkl import db, panel
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")
t0 = time.time()
con = db.connect()
db.attach_events(con)
panel.build_daily_channel(con)
con.execute(f"COPY daily_channel TO '{PATHS.interim / 'daily_channel.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)")
print(con.execute("SELECT count(*) rows, count(DISTINCT ch) ch, min(day), max(day) FROM daily_channel").df().to_string(index=False))
print(f"{time.time()-t0:.0f}s")
con.close()
```

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/build_panel.py`
Expected: порядка 12–16 млн строк, диапазон 2019-01-01…2026-06-30

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/panel.py tests/test_panel.py scripts/build_panel.py
git commit -m "feat: суточная панель канал x сутки как основа фичей"
```

---

### Task 5: Построители меток

**Files:**
- Create: `src/mkl/labels.py`, `tests/test_labels.py`

**Interfaces:**
- Consumes: `episodes` (Task 3), `daily_channel` (Task 4), `config.MIN_FAILURE_DURATION_S`
- Produces: `labels.build_sensor_failure(con, variant: str, horizon_days: int = 1) -> None` создаёт `label_failure(ch, day, y)`; `labels.build_group_outage(con, horizon_days=1)`; `labels.build_fire(con, horizon_days=1)`; `labels.build_intrusion(con, horizon_days=1)`; `labels.build_wear(con, horizon_days=7)`

Варианты метки отказа: `L1` любой `Неисправен`; `L2` эпизод ≥ 1 ч; `L3` = L2 без групповых; `L4` молчание дольше собственного p99; `L5` = L3 ∪ L4.

Правило против циркулярности: метка строится **только** по состояниям и молчанию. Признаки поведения (chatter, дисперсия) в метку не входят никогда.

- [ ] **Step 1: Написать падающий тест на горизонт и отсутствие протечки**

```python
# tests/test_labels.py
import duckdb
import pytest

from mkl import labels


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE daily_channel (ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR)")
    c.execute("""CREATE TABLE episodes (ch BIGINT, obj VARCHAR, stype VARCHAR,
                 t_start TIMESTAMP, t_end TIMESTAMP, dur_s BIGINT,
                 n_events BIGINT, states VARCHAR, is_group BOOLEAN)""")
    for d in range(1, 11):
        c.execute("INSERT INTO daily_channel VALUES (1, ?, 'A', 'Датчик дыма')",
                  [f"2025-01-{d:02d}"])
    yield c
    c.close()


def test_label_marks_day_before_failure(con):
    con.execute("""INSERT INTO episodes VALUES
        (1,'A','Датчик дыма','2025-01-05 03:00:00','2025-01-05 09:00:00',21600,3,'Обесточен',false)""")
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    pos = [r[0] for r in con.execute("SELECT day FROM label_failure WHERE y=1 ORDER BY day").fetchall()]
    assert [str(d) for d in pos] == ["2025-01-04"]


def test_short_episode_is_not_a_failure(con):
    con.execute("""INSERT INTO episodes VALUES
        (1,'A','Датчик дыма','2025-01-05 03:00:00','2025-01-05 03:00:30',30,2,'Неисправен',false)""")
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0


def test_L3_excludes_group_episodes(con):
    con.execute("""INSERT INTO episodes VALUES
        (1,'A','Датчик дыма','2025-01-05 03:00:00','2025-01-05 09:00:00',21600,3,'Обесточен',true)""")
    labels.build_sensor_failure(con, variant="L3", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 0
    labels.build_sensor_failure(con, variant="L2", horizon_days=1)
    assert con.execute("SELECT sum(y) FROM label_failure").fetchone()[0] == 1


def test_horizon_widens_positive_window(con):
    con.execute("""INSERT INTO episodes VALUES
        (1,'A','Датчик дыма','2025-01-08 03:00:00','2025-01-08 09:00:00',21600,3,'Обесточен',false)""")
    labels.build_sensor_failure(con, variant="L2", horizon_days=7)
    pos = sorted(str(r[0]) for r in con.execute("SELECT day FROM label_failure WHERE y=1").fetchall())
    assert pos == ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04",
                   "2025-01-05", "2025-01-06", "2025-01-07"]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_labels.py -v`
Expected: FAIL с `AttributeError: module 'mkl.labels' has no attribute 'build_sensor_failure'`

- [ ] **Step 3: Реализовать labels.py**

```python
import duckdb

from .config import FIRE_STATES, INTRUSION_STATES, MIN_FAILURE_DURATION_S, WEAR_STATES

_VARIANT_FILTER = {
    "L1": "states LIKE '%Неисправен%'",
    "L2": f"dur_s >= {MIN_FAILURE_DURATION_S}",
    "L3": f"dur_s >= {MIN_FAILURE_DURATION_S} AND NOT is_group",
}


def _in(states: frozenset[str]) -> str:
    return "(" + ",".join(f"'{s}'" for s in sorted(states)) + ")"


def _emit(con: duckdb.DuckDBPyConnection, table: str, events_sql: str,
          entity: str, horizon_days: int) -> None:
    """Метка = было ли целевое событие в окне (day, day + horizon] для сущности."""
    con.execute(f"""
    CREATE OR REPLACE TABLE {table} AS
    WITH base AS (SELECT DISTINCT {entity} AS eid, day FROM daily_channel),
         tgt  AS ({events_sql})
    SELECT b.eid, b.day,
           CASE WHEN EXISTS (
             SELECT 1 FROM tgt t
             WHERE t.eid = b.eid
               AND t.event_day >  b.day
               AND t.event_day <= b.day + INTERVAL {horizon_days} DAY
           ) THEN 1 ELSE 0 END AS y
    FROM base b
    """)
    con.execute(f"ALTER TABLE {table} RENAME COLUMN eid TO {entity}")


_SILENCE_SQL = """
  SELECT ch AS eid, day AS event_day FROM (
    SELECT ch, day,
           date_diff('day', lag(day) OVER (PARTITION BY ch ORDER BY day), day) AS gap_days,
           count(*) OVER (PARTITION BY ch ORDER BY day
                          RANGE BETWEEN INTERVAL 30 DAY PRECEDING AND CURRENT ROW) AS recent_days
    FROM daily_channel
  ) WHERE gap_days >= 2 AND recent_days >= 7
"""


def build_sensor_failure(con, variant: str = "L3", horizon_days: int = 1) -> None:
    """L1 любой Неисправен; L2 эпизод >= 1 ч; L3 = L2 без групповых;
    L4 молчание суток при живом канале; L5 = L3 объединить L4."""
    if variant == "L4":
        events = _SILENCE_SQL
    elif variant == "L5":
        events = f"""
          SELECT ch AS eid, CAST(t_start AS DATE) AS event_day FROM episodes
          WHERE dur_s >= {MIN_FAILURE_DURATION_S} AND NOT is_group
          UNION ALL
          {_SILENCE_SQL}
        """
    else:
        events = f"""
          SELECT ch AS eid, CAST(t_start AS DATE) AS event_day FROM episodes
          WHERE {_VARIANT_FILTER[variant]}
        """
    _emit(con, "label_failure", events, "ch", horizon_days)


def build_group_outage(con, horizon_days: int = 1) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE label_group_outage AS
    WITH base AS (SELECT DISTINCT obj, day FROM daily_channel WHERE obj IS NOT NULL),
         tgt AS (SELECT obj, CAST(t_start AS DATE) AS event_day FROM group_outages)
    SELECT b.obj, b.day,
           CASE WHEN EXISTS (SELECT 1 FROM tgt t WHERE t.obj = b.obj
                             AND t.event_day > b.day
                             AND t.event_day <= b.day + INTERVAL {horizon_days} DAY)
                THEN 1 ELSE 0 END AS y
    FROM base b
    """)


def build_fire(con, horizon_days: int = 1) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE label_fire AS
    WITH base AS (
      SELECT DISTINCT obj, CAST(floor(coalesce(picket,0)/10) AS INTEGER) AS seg, day
      FROM daily_channel WHERE obj IS NOT NULL
    ), tgt AS (
      SELECT obj, CAST(floor(coalesce(picket,0)/10) AS INTEGER) AS seg, day AS event_day
      FROM daily_channel WHERE n_fire > 0
    )
    SELECT b.obj, b.seg, b.day,
           CASE WHEN EXISTS (SELECT 1 FROM tgt t WHERE t.obj=b.obj AND t.seg=b.seg
                             AND t.event_day > b.day
                             AND t.event_day <= b.day + INTERVAL {horizon_days} DAY)
                THEN 1 ELSE 0 END AS y
    FROM base b
    """)


def build_intrusion(con, horizon_days: int = 1) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE label_intrusion AS
    WITH base AS (SELECT DISTINCT obj, day FROM daily_channel WHERE obj IS NOT NULL),
         tgt AS (SELECT obj, day AS event_day FROM daily_channel
                 WHERE n_intrusion > 0 AND n_alarms > 0)
    SELECT b.obj, b.day,
           CASE WHEN EXISTS (SELECT 1 FROM tgt t WHERE t.obj=b.obj
                             AND t.event_day > b.day
                             AND t.event_day <= b.day + INTERVAL {horizon_days} DAY)
                THEN 1 ELSE 0 END AS y
    FROM base b
    """)


def build_wear(con, horizon_days: int = 7) -> None:
    from .config import EQUIPMENT_STYPES
    con.execute(f"""
    CREATE OR REPLACE TABLE label_wear AS
    WITH base AS (SELECT DISTINCT ch, day FROM daily_channel
                  WHERE stype IN {_in(EQUIPMENT_STYPES)}),
         tgt AS (
           SELECT ch, CAST(t_start AS DATE) AS event_day FROM episodes
           WHERE dur_s >= {MIN_FAILURE_DURATION_S}
           UNION ALL
           SELECT ch, day FROM daily_channel WHERE n_events > 0 AND ch IN (
             SELECT ch FROM daily_channel WHERE stype IN {_in(EQUIPMENT_STYPES)}
           ) AND n_alarms > 0
         )
    SELECT b.ch, b.day,
           CASE WHEN EXISTS (SELECT 1 FROM tgt t WHERE t.ch=b.ch
                             AND t.event_day > b.day
                             AND t.event_day <= b.day + INTERVAL {horizon_days} DAY)
                THEN 1 ELSE 0 END AS y
    FROM base b
    """)
```

- [ ] **Step 4: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_labels.py -v`
Expected: PASS, 4 теста

- [ ] **Step 5: Коммит**

```bash
git add src/mkl/labels.py tests/test_labels.py
git commit -m "feat: построители меток для пяти голов с контролем горизонта"
```

---

### Task 6: Фичи — окна ISA-18.2 и ритм канала

**Files:**
- Create: `src/mkl/features/__init__.py`, `src/mkl/features/base.py`, `tests/test_features_base.py`

**Interfaces:**
- Consumes: `daily_channel`
- Produces: `base.add_rolling_windows(con, windows=(7, 30)) -> None` создаёт `feat_base(ch, day, ...)` с суффиксами `_w7`, `_w30` для `n_events, n_alarms, n_bad, n_chatter_1min, n_transitions`; плюс `days_since_last_alarm`, `days_since_last_bad`, `silence_ratio` (текущее молчание к собственному p99), `pareto_rank_obj`

Правило: все окна **строго прошлые**, текущие сутки включаются целиком (фичи считаются на конец суток D, метка смотрит в D+1…D+H). Окно `ROWS BETWEEN n PRECEDING AND CURRENT ROW` по календарю суток.

- [ ] **Step 1: Написать падающий тест на отсутствие заглядывания вперёд**

```python
# tests/test_features_base.py
import duckdb
import pytest

from mkl.features import base


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE daily_channel (
        ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR, sys VARCHAR, picket DOUBLE,
        n_events BIGINT, n_alarms BIGINT, n_bad BIGINT, n_ok BIGINT,
        n_fire BIGINT, n_intrusion BIGINT, n_transitions BIGINT,
        n_chatter_1min BIGINT, max_gap_s BIGINT, med_gap_s DOUBLE,
        val_min DOUBLE, val_max DOUBLE, val_mean DOUBLE, val_std DOUBLE)""")
    yield c
    c.close()


def _day(c, ch, day, n_events=1, n_alarms=0):
    c.execute("""INSERT INTO daily_channel VALUES
        (?,?,'A','Датчик дыма','s',1.0,?,?,0,0,0,0,0,0,0,0.0,NULL,NULL,NULL,NULL)""",
        [ch, day, n_events, n_alarms])


def test_rolling_window_excludes_future_days(con):
    _day(con, 1, "2025-01-01", n_events=1)
    _day(con, 1, "2025-01-02", n_events=10)
    base.add_rolling_windows(con, windows=(7,))
    v = con.execute(
        "SELECT n_events_w7 FROM feat_base WHERE day = DATE '2025-01-01'"
    ).fetchone()[0]
    assert v == 1, "окно на 1 января не должно видеть 2 января"


def test_rolling_window_accumulates_past(con):
    _day(con, 1, "2025-01-01", n_events=1)
    _day(con, 1, "2025-01-02", n_events=10)
    base.add_rolling_windows(con, windows=(7,))
    v = con.execute(
        "SELECT n_events_w7 FROM feat_base WHERE day = DATE '2025-01-02'"
    ).fetchone()[0]
    assert v == 11


def test_days_since_last_alarm(con):
    _day(con, 1, "2025-01-01", n_alarms=1)
    _day(con, 1, "2025-01-02")
    _day(con, 1, "2025-01-05")
    base.add_rolling_windows(con, windows=(7,))
    got = {str(d): v for d, v in con.execute(
        "SELECT day, days_since_last_alarm FROM feat_base ORDER BY day").fetchall()}
    assert got["2025-01-01"] == 0
    assert got["2025-01-02"] == 1
    assert got["2025-01-05"] == 4


def test_pareto_rank_orders_channels_within_object(con):
    _day(con, 1, "2025-01-01", n_alarms=100)
    _day(con, 2, "2025-01-01", n_alarms=1)
    base.add_rolling_windows(con, windows=(7,))
    got = dict(con.execute("SELECT ch, pareto_rank_obj FROM feat_base").fetchall())
    assert got[1] < got[2]
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_features_base.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.features'`

- [ ] **Step 3: Реализовать features/base.py**

```python
import duckdb

ROLLING_COLS = ("n_events", "n_alarms", "n_bad", "n_chatter_1min", "n_transitions")


def add_rolling_windows(con: duckdb.DuckDBPyConnection,
                        windows: tuple[int, ...] = (7, 30)) -> None:
    parts = []
    for w in windows:
        for c in ROLLING_COLS:
            parts.append(
                f"sum({c}) OVER (PARTITION BY ch ORDER BY day "
                f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) AS {c}_w{w}"
            )
        parts.append(
            f"avg(CAST(n_events AS DOUBLE)) OVER (PARTITION BY ch ORDER BY day "
            f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) AS n_events_mean_w{w}"
        )
        parts.append(
            f"stddev_pop(CAST(n_events AS DOUBLE)) OVER (PARTITION BY ch ORDER BY day "
            f"RANGE BETWEEN INTERVAL {w - 1} DAY PRECEDING AND CURRENT ROW) AS n_events_std_w{w}"
        )
    rolling = ",\n           ".join(parts)

    con.execute(f"""
    CREATE OR REPLACE TABLE feat_base AS
    WITH r AS (
      SELECT ch, day, obj, stype, sys, picket,
             n_events, n_alarms, n_bad, n_chatter_1min, n_transitions,
             max_gap_s, med_gap_s, val_mean, val_std, val_min, val_max,
             {rolling},
             max(CASE WHEN n_alarms > 0 THEN day END) OVER (
               PARTITION BY ch ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS last_alarm_day,
             max(CASE WHEN n_bad > 0 THEN day END) OVER (
               PARTITION BY ch ORDER BY day ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
             ) AS last_bad_day,
             quantile_cont(med_gap_s, 0.99) OVER (
               PARTITION BY ch ORDER BY day
               RANGE BETWEEN INTERVAL 89 DAY PRECEDING AND CURRENT ROW
             ) AS p99_gap_s
      FROM daily_channel
    )
    SELECT *,
           date_diff('day', last_alarm_day, day) AS days_since_last_alarm,
           date_diff('day', last_bad_day,   day) AS days_since_last_bad,
           CASE WHEN p99_gap_s > 0 THEN max_gap_s / p99_gap_s END AS silence_ratio,
           row_number() OVER (PARTITION BY obj, day ORDER BY n_alarms DESC, ch) AS pareto_rank_obj
    FROM r
    """)
```

- [ ] **Step 4: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_features_base.py -v`
Expected: PASS, 4 теста

- [ ] **Step 5: Коммит**

```bash
git add src/mkl/features tests/test_features_base.py
git commit -m "feat: оконные фичи ISA-18.2 и ритм канала без заглядывания вперёд"
```

---

### Task 7: Фичи — peer-relative, пространственные, жизненный цикл, контекст

**Files:**
- Create: `src/mkl/features/relative.py`, `src/mkl/features/lifecycle.py`, `tests/test_features_relative.py`

**Interfaces:**
- Consumes: `feat_base`
- Produces: `relative.add_peer_features(con) -> None` добавляет `peer_ratio_alarms`, `peer_z_events`, `peer_median_alarms`; `relative.add_spatial_features(con) -> None` добавляет `nbr_bad_w7`, `nbr_alarms_w7`, `dist_to_nearest_bad`; `lifecycle.add_lifecycle_features(con) -> None` добавляет `age_days`, `cum_events`, `n_prior_failures`, `days_since_prior_failure`, `n_duty_cycles_w30`; результат в таблице `feat_full`

Peer — датчики того же `stype` на том же `obj` в те же сутки. Ресёрч называет это семейство сильнейшим: оно бесплатно снимает сезонность и специфику объекта.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_features_relative.py
import duckdb
import pytest

from mkl.features import lifecycle, relative


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("""CREATE TABLE feat_base (
        ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR, picket DOUBLE,
        n_events BIGINT, n_alarms BIGINT, n_bad BIGINT, n_transitions BIGINT)""")
    yield c
    c.close()


def _row(c, ch, day, picket, n_alarms=0, n_bad=0, n_events=1):
    c.execute("INSERT INTO feat_base VALUES (?,?,'A','Датчик дыма',?,?,?,?,0)",
              [ch, day, picket, n_events, n_alarms, n_bad])


def test_peer_ratio_flags_outlier_channel(con):
    _row(con, 1, "2025-01-01", 10.0, n_alarms=100)
    _row(con, 2, "2025-01-01", 20.0, n_alarms=1)
    _row(con, 3, "2025-01-01", 30.0, n_alarms=1)
    relative.add_peer_features(con)
    got = dict(con.execute("SELECT ch, peer_ratio_alarms FROM feat_peer").fetchall())
    assert got[1] > 10
    assert got[2] == pytest.approx(1.0)


def test_spatial_neighbour_picks_up_adjacent_picket(con):
    _row(con, 1, "2025-01-01", 10.0, n_bad=0)
    _row(con, 2, "2025-01-01", 12.0, n_bad=5)
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius=5.0)
    got = dict(con.execute("SELECT ch, nbr_bad FROM feat_spatial").fetchall())
    assert got[1] == 5


def test_age_days_counts_from_first_seen(con):
    _row(con, 1, "2025-01-01", 10.0)
    _row(con, 1, "2025-01-11", 10.0)
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius=5.0)
    lifecycle.add_lifecycle_features(con)
    got = {str(d): v for d, v in con.execute(
        "SELECT day, age_days FROM feat_full ORDER BY day").fetchall()}
    assert got["2025-01-01"] == 0
    assert got["2025-01-11"] == 10
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_features_relative.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.features.relative'`

- [ ] **Step 3: Реализовать features/relative.py**

```python
import duckdb


def add_peer_features(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
    CREATE OR REPLACE TABLE feat_peer AS
    SELECT b.*,
           median(CAST(n_alarms AS DOUBLE)) OVER (PARTITION BY obj, stype, day) AS peer_median_alarms,
           avg(CAST(n_events AS DOUBLE))    OVER (PARTITION BY obj, stype, day) AS peer_mean_events,
           stddev_pop(CAST(n_events AS DOUBLE)) OVER (PARTITION BY obj, stype, day) AS peer_std_events
    FROM feat_base b
    """)
    con.execute("""
    CREATE OR REPLACE TABLE feat_peer AS
    SELECT *,
           CASE WHEN peer_median_alarms > 0 THEN n_alarms / peer_median_alarms
                WHEN n_alarms > 0 THEN n_alarms
                ELSE 1.0 END AS peer_ratio_alarms,
           CASE WHEN peer_std_events > 0 THEN (n_events - peer_mean_events) / peer_std_events
                ELSE 0.0 END AS peer_z_events
    FROM feat_peer
    """)


def add_spatial_features(con: duckdb.DuckDBPyConnection, radius: float = 5.0) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_spatial AS
    SELECT p.*,
           coalesce(n.nbr_bad, 0)    AS nbr_bad,
           coalesce(n.nbr_alarms, 0) AS nbr_alarms,
           n.dist_to_nearest_bad
    FROM feat_peer p
    LEFT JOIN (
      SELECT a.ch, a.day,
             sum(b.n_bad)    AS nbr_bad,
             sum(b.n_alarms) AS nbr_alarms,
             min(CASE WHEN b.n_bad > 0 THEN abs(a.picket - b.picket) END) AS dist_to_nearest_bad
      FROM feat_peer a
      JOIN feat_peer b
        ON a.obj = b.obj AND a.day = b.day AND a.ch <> b.ch
       AND abs(a.picket - b.picket) <= {radius}
      GROUP BY a.ch, a.day
    ) n ON n.ch = p.ch AND n.day = p.day
    """)
```

- [ ] **Step 4: Реализовать features/lifecycle.py**

```python
import duckdb


def add_lifecycle_features(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
    CREATE OR REPLACE TABLE feat_full AS
    WITH first_seen AS (SELECT ch, min(day) AS fs FROM feat_spatial GROUP BY ch)
    SELECT s.*,
           date_diff('day', f.fs, s.day) AS age_days,
           sum(s.n_events) OVER (PARTITION BY s.ch ORDER BY s.day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum_events,
           sum(CASE WHEN s.n_bad > 0 THEN 1 ELSE 0 END) OVER (PARTITION BY s.ch ORDER BY s.day
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS n_prior_failures,
           sum(s.n_transitions) OVER (PARTITION BY s.ch ORDER BY s.day
             RANGE BETWEEN INTERVAL 29 DAY PRECEDING AND CURRENT ROW) AS n_duty_cycles_w30,
           date_diff('day',
             max(CASE WHEN s.n_bad > 0 THEN s.day END) OVER (PARTITION BY s.ch ORDER BY s.day
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
             s.day) AS days_since_prior_failure
    FROM feat_spatial s
    JOIN first_seen f ON f.ch = s.ch
    """)
```

- [ ] **Step 5: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_features_relative.py -v`
Expected: PASS, 3 теста

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/features tests/test_features_relative.py
git commit -m "feat: peer-relative, пространственные и lifecycle фичи"
```

---

### Task 8: Внешние данные — погода Москвы и календарь

**Files:**
- Create: `src/mkl/features/external.py`, `tests/test_features_external.py`, `scripts/fetch_weather.py`

**Interfaces:**
- Produces: `external.fetch_moscow_weather(start: date, end: date) -> pl.DataFrame` с колонками `day, t_mean, t_min, t_max, precip_mm, snow_depth_cm, t_range`; `external.add_calendar(con) -> None` добавляет `dow, month, is_weekend, is_holiday, doy_sin, doy_cos`

Источник погоды — Open-Meteo Historical Weather API (бесплатный, без ключа, координаты Москвы 55.75 / 37.62). Результат кэшируется в `data/interim/weather.parquet`; при недоступности сети пайплайн обязан продолжать работу на календарных фичах, а не падать.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_features_external.py
import datetime as dt

import duckdb
import pytest

from mkl.features import external


def test_calendar_features_are_cyclic(tmp_path):
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE feat_full (ch BIGINT, day DATE)")
    con.execute("INSERT INTO feat_full VALUES (1, DATE '2025-01-01'), (1, DATE '2025-07-01')")
    external.add_calendar(con)
    rows = con.execute("SELECT day, dow, month, doy_sin, doy_cos FROM feat_ext ORDER BY day").fetchall()
    assert rows[0][2] == 1 and rows[1][2] == 7
    for r in rows:
        assert -1.0 <= r[3] <= 1.0 and -1.0 <= r[4] <= 1.0
    con.close()


def test_weather_cache_is_reused(tmp_path, monkeypatch):
    cache = tmp_path / "weather.parquet"
    calls = []

    def fake_download(start, end):
        calls.append((start, end))
        import polars as pl
        return pl.DataFrame({
            "day": [dt.date(2025, 1, 1)], "t_mean": [1.0], "t_min": [0.0],
            "t_max": [2.0], "precip_mm": [0.0], "snow_depth_cm": [0.0],
        })

    monkeypatch.setattr(external, "_download_weather", fake_download)
    monkeypatch.setattr(external, "WEATHER_CACHE", cache)
    external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 1))
    external.fetch_moscow_weather(dt.date(2025, 1, 1), dt.date(2025, 1, 1))
    assert len(calls) == 1, "второй вызов обязан читать кэш"
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_features_external.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.features.external'`

- [ ] **Step 3: Реализовать features/external.py**

```python
import datetime as dt
import json
import urllib.request

import duckdb
import polars as pl

from ..config import PATHS

WEATHER_CACHE = PATHS.interim / "weather.parquet"
LAT, LON = 55.75, 37.62

RU_HOLIDAYS_MMDD = {
    (1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7), (1, 8),
    (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4),
}


def _download_weather(start: dt.date, end: dt.date) -> pl.DataFrame:
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={LAT}&longitude={LON}&start_date={start}&end_date={end}"
        "&daily=temperature_2m_mean,temperature_2m_min,temperature_2m_max,"
        "precipitation_sum,snow_depth_max&timezone=Europe%2FMoscow"
    )
    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = json.load(resp)
    d = payload["daily"]
    return pl.DataFrame({
        "day": [dt.date.fromisoformat(x) for x in d["time"]],
        "t_mean": d["temperature_2m_mean"],
        "t_min": d["temperature_2m_min"],
        "t_max": d["temperature_2m_max"],
        "precip_mm": d["precipitation_sum"],
        "snow_depth_cm": [(v or 0.0) * 100 for v in d["snow_depth_max"]],
    })


def fetch_moscow_weather(start: dt.date, end: dt.date) -> pl.DataFrame:
    if WEATHER_CACHE.exists():
        return pl.read_parquet(WEATHER_CACHE)
    df = _download_weather(start, end)
    df = df.with_columns((pl.col("t_max") - pl.col("t_min")).alias("t_range"))
    WEATHER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(WEATHER_CACHE)
    return df


def add_calendar(con: duckdb.DuckDBPyConnection, source: str = "feat_full") -> None:
    holidays = ",".join(f"({m},{d})" for m, d in sorted(RU_HOLIDAYS_MMDD))
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_ext AS
    SELECT *,
           dayofweek(day)                        AS dow,
           month(day)                            AS month,
           CASE WHEN dayofweek(day) IN (0, 6) THEN 1 ELSE 0 END AS is_weekend,
           CASE WHEN (month(day), day(day)) IN ({holidays}) THEN 1 ELSE 0 END AS is_holiday,
           sin(2 * pi() * dayofyear(day) / 365.25) AS doy_sin,
           cos(2 * pi() * dayofyear(day) / 365.25) AS doy_cos
    FROM {source}
    """)


def add_weather(con: duckdb.DuckDBPyConnection) -> None:
    if not WEATHER_CACHE.exists():
        con.execute("CREATE OR REPLACE TABLE feat_ext AS SELECT * FROM feat_ext")
        return
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_ext AS
    SELECT e.*, w.t_mean, w.t_min, w.t_max, w.precip_mm, w.snow_depth_cm, w.t_range
    FROM feat_ext e
    LEFT JOIN read_parquet('{WEATHER_CACHE}') w ON w.day = e.day
    """)
```

- [ ] **Step 4: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_features_external.py -v`
Expected: PASS, 2 теста

- [ ] **Step 5: Выкачать погоду**

```python
# scripts/fetch_weather.py
import datetime as dt, sys
from mkl.features import external

sys.stdout.reconfigure(encoding="utf-8")
df = external.fetch_moscow_weather(dt.date(2019, 1, 1), dt.date(2026, 6, 30))
print(df.shape, df.head())
```

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/fetch_weather.py`
Expected: около 2738 строк, колонки `day, t_mean, t_min, t_max, precip_mm, snow_depth_cm, t_range`. Если сети нет — зафиксировать это в отчёте и продолжать без погодных фич.

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/features/external.py tests/test_features_external.py scripts/fetch_weather.py
git commit -m "feat: погода Москвы и календарные фичи с кэшем и мягкой деградацией"
```

---

### Task 9: Фичестор — единая точка расчёта, реестр, чтение среза

**Files:**
- Create: `src/mkl/features/compute.py`, `src/mkl/store.py`, `configs/features.yaml`, `tests/test_store.py`, `scripts/build_features.py`

**Interfaces:**
- Consumes: всё из Task 6–8
- Produces: `compute.build_all(con) -> None` — единственный способ получить фичи, вызывается и обучением, и инференсом; `store.write(con, table, name) -> Path`; `store.read_slice(name, start, end) -> pl.DataFrame`; `store.latest_snapshot(name) -> pl.DataFrame`; `store.load_registry() -> dict`

Главная защита от train/serve skew: обучение и инференс не имеют иного пути получить фичи, кроме `compute.build_all`.

- [ ] **Step 1: Написать падающий тест на отсутствие утечки и на реестр**

```python
# tests/test_store.py
import datetime as dt

import duckdb
import polars as pl
import pytest

from mkl import store
from mkl.features import compute


def test_registry_lists_every_feature_column(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FEATURE_DIR", tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE feat_ext AS SELECT 1 AS ch, DATE '2025-01-01' AS day, 2.0 AS n_events_w7")
    store.write(con, "feat_ext", "sensor")
    reg = store.load_registry()
    assert "n_events_w7" in reg["sensor"]["columns"]
    con.close()


def test_read_slice_respects_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FEATURE_DIR", tmp_path)
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE feat_ext AS
        SELECT * FROM (VALUES (1, DATE '2025-01-01', 1.0), (1, DATE '2025-06-01', 2.0))
        AS t(ch, day, n_events_w7)""")
    store.write(con, "feat_ext", "sensor")
    got = store.read_slice("sensor", dt.date(2025, 1, 1), dt.date(2025, 1, 31))
    assert got.height == 1
    con.close()


def test_feature_value_does_not_change_when_future_rows_added(tmp_path, monkeypatch):
    """Ключевой тест на утечку: добавление будущих суток не меняет фичу прошлых."""
    monkeypatch.setattr(store, "FEATURE_DIR", tmp_path)

    def build(days):
        con = duckdb.connect(":memory:")
        con.execute("""CREATE TABLE daily_channel (
            ch BIGINT, day DATE, obj VARCHAR, stype VARCHAR, sys VARCHAR, picket DOUBLE,
            n_events BIGINT, n_alarms BIGINT, n_bad BIGINT, n_ok BIGINT,
            n_fire BIGINT, n_intrusion BIGINT, n_transitions BIGINT,
            n_chatter_1min BIGINT, max_gap_s BIGINT, med_gap_s DOUBLE,
            val_min DOUBLE, val_max DOUBLE, val_mean DOUBLE, val_std DOUBLE)""")
        for d, n in days:
            con.execute("""INSERT INTO daily_channel VALUES
                (1,?, 'A','Датчик дыма','s',1.0,?,0,0,0,0,0,0,0,0,0.0,NULL,NULL,NULL,NULL)""",
                [d, n])
        compute.build_all(con, with_weather=False)
        v = con.execute(
            "SELECT n_events_w7 FROM feat_ext WHERE day = DATE '2025-01-01'"
        ).fetchone()[0]
        con.close()
        return v

    short = build([("2025-01-01", 5)])
    long = build([("2025-01-01", 5), ("2025-01-02", 999)])
    assert short == long
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_store.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.store'`

- [ ] **Step 3: Реализовать features/compute.py**

```python
import duckdb

from . import base, external, lifecycle, relative


def build_all(con: duckdb.DuckDBPyConnection,
              windows: tuple[int, ...] = (7, 30),
              spatial_radius: float = 5.0,
              with_weather: bool = True) -> None:
    """Единственный путь получения фич. Используется и обучением, и инференсом."""
    base.add_rolling_windows(con, windows=windows)
    relative.add_peer_features(con)
    relative.add_spatial_features(con, radius=spatial_radius)
    lifecycle.add_lifecycle_features(con)
    external.add_calendar(con)
    if with_weather:
        external.add_weather(con)
```

- [ ] **Step 4: Реализовать store.py**

```python
import datetime as dt
from pathlib import Path

import duckdb
import polars as pl
import yaml

from .config import PATHS

FEATURE_DIR = PATHS.features
REGISTRY = PATHS.root / "configs" / "features.yaml"

KEY_COLUMNS = {"ch", "obj", "seg", "day"}


def write(con: duckdb.DuckDBPyConnection, table: str, name: str) -> Path:
    FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    dst = FEATURE_DIR / f"{name}.parquet"
    con.execute(f"COPY {table} TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    dtypes = {r[0]: r[1] for r in con.execute(f"DESCRIBE {table}").fetchall()}
    reg = load_registry()
    reg[name] = {
        "path": str(dst),
        "keys": sorted(set(cols) & KEY_COLUMNS),
        "columns": [c for c in cols if c not in KEY_COLUMNS],
        "dtypes": dtypes,
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(yaml.safe_dump(reg, allow_unicode=True, sort_keys=True),
                        encoding="utf-8")
    return dst


def load_registry() -> dict:
    if not REGISTRY.exists():
        return {}
    return yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}


def read_slice(name: str, start: dt.date, end: dt.date) -> pl.DataFrame:
    path = FEATURE_DIR / f"{name}.parquet"
    return (
        pl.scan_parquet(path)
        .filter((pl.col("day") >= start) & (pl.col("day") <= end))
        .collect()
    )


def latest_snapshot(name: str) -> pl.DataFrame:
    path = FEATURE_DIR / f"{name}.parquet"
    lf = pl.scan_parquet(path)
    last_day = lf.select(pl.col("day").max()).collect().item()
    return lf.filter(pl.col("day") == last_day).collect()
```

- [ ] **Step 5: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_store.py -v`
Expected: PASS, 3 теста. Третий — главный: он доказывает отсутствие заглядывания вперёд.

- [ ] **Step 6: Построить фичи на реальных данных**

```python
# scripts/build_features.py
import sys, time
from mkl import db, store
from mkl.config import PATHS
from mkl.features import compute

sys.stdout.reconfigure(encoding="utf-8")
t0 = time.time()
con = db.connect()
con.execute(f"CREATE VIEW daily_channel AS SELECT * FROM read_parquet('{PATHS.interim / 'daily_channel.parquet'}')")
compute.build_all(con)
store.write(con, "feat_ext", "sensor")
print(con.execute("SELECT count(*) rows, count(DISTINCT ch) ch FROM feat_ext").df().to_string(index=False))
print(f"{time.time()-t0:.0f}s")
con.close()
```

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/build_features.py`
Expected: фичестор в `data/features/sensor.parquet`, реестр в `configs/features.yaml`

- [ ] **Step 7: Коммит**

```bash
git add src/mkl/features/compute.py src/mkl/store.py tests/test_store.py scripts/build_features.py configs/features.yaml
git commit -m "feat: фичестор с единой точкой расчёта и тестом на утечку из будущего"
```

---

### Task 10: Валидация — walk-forward, purge, embargo, метрики

**Files:**
- Create: `src/mkl/cv.py`, `src/mkl/metrics.py`, `tests/test_cv.py`, `tests/test_metrics.py`

**Interfaces:**
- Produces: `cv.walk_forward(days, n_splits, test_days, embargo_days) -> list[Split]` где `Split` — dataclass с `train_start, train_end, test_start, test_end`; `metrics.pr_auc(y, p) -> float`; `metrics.precision_at_k(y, p, k) -> float`; `metrics.recall_at_k(y, p, k) -> float`; `metrics.threshold_for_budget(p, budget) -> float`; `metrics.summary(y, p, budget) -> dict`

- [ ] **Step 1: Написать падающий тест на embargo**

```python
# tests/test_cv.py
import datetime as dt

import pytest

from mkl import cv


def _days(n, start=dt.date(2024, 1, 1)):
    return [start + dt.timedelta(days=i) for i in range(n)]


def test_embargo_gap_is_respected():
    splits = cv.walk_forward(_days(400), n_splits=3, test_days=30, embargo_days=31)
    for s in splits:
        gap = (s.test_start - s.train_end).days
        assert gap > 31, f"разрыв {gap} суток не покрывает embargo"


def test_train_always_precedes_test():
    for s in cv.walk_forward(_days(400), n_splits=3, test_days=30, embargo_days=31):
        assert s.train_end < s.test_start
        assert s.train_start < s.train_end
        assert s.test_start < s.test_end


def test_test_windows_do_not_overlap():
    splits = cv.walk_forward(_days(400), n_splits=3, test_days=30, embargo_days=31)
    for a, b in zip(splits, splits[1:]):
        assert a.test_end < b.test_start


def test_raises_when_history_too_short():
    with pytest.raises(ValueError):
        cv.walk_forward(_days(40), n_splits=3, test_days=30, embargo_days=31)
```

```python
# tests/test_metrics.py
import numpy as np
import pytest

from mkl import metrics


def test_precision_at_k_takes_top_scores():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.9, 0.8])
    assert metrics.precision_at_k(y, p, k=2) == pytest.approx(1.0)


def test_recall_at_k():
    y = np.array([1, 1, 1, 0])
    p = np.array([0.9, 0.8, 0.1, 0.2])
    assert metrics.recall_at_k(y, p, k=2) == pytest.approx(2 / 3)


def test_pr_auc_is_one_for_perfect_ranking():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.2, 0.8, 0.9])
    assert metrics.pr_auc(y, p) == pytest.approx(1.0)


def test_threshold_for_budget_selects_that_many_alerts():
    p = np.array([0.1, 0.5, 0.9, 0.7, 0.3])
    thr = metrics.threshold_for_budget(p, budget=2)
    assert (p >= thr).sum() == 2


def test_summary_reports_required_keys():
    y = np.array([0, 1, 0, 1])
    p = np.array([0.2, 0.8, 0.3, 0.7])
    out = metrics.summary(y, p, budget=2)
    assert {"pr_auc", "precision_at_k", "recall_at_k", "brier",
            "precision", "recall", "threshold", "n_pos"} <= out.keys()
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_cv.py tests/test_metrics.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.cv'`

- [ ] **Step 3: Реализовать cv.py**

```python
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class Split:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def walk_forward(days: list[dt.date], n_splits: int, test_days: int,
                 embargo_days: int, min_train_days: int = 90) -> list[Split]:
    days = sorted(set(days))
    if not days:
        raise ValueError("пустой список суток")
    last = days[-1]
    first = days[0]
    needed = min_train_days + embargo_days + test_days * n_splits
    if (last - first).days + 1 < needed:
        raise ValueError(
            f"истории {(last - first).days + 1} суток, нужно минимум {needed}"
        )
    splits: list[Split] = []
    for i in range(n_splits, 0, -1):
        test_end = last - dt.timedelta(days=test_days * (i - 1))
        test_start = test_end - dt.timedelta(days=test_days - 1)
        train_end = test_start - dt.timedelta(days=embargo_days + 1)
        splits.append(Split(first, train_end, test_start, test_end))
    return splits
```

- [ ] **Step 4: Реализовать metrics.py**

```python
import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss


def pr_auc(y: np.ndarray, p: np.ndarray) -> float:
    if y.sum() == 0:
        return float("nan")
    return float(average_precision_score(y, p))


def _top_k_mask(p: np.ndarray, k: int) -> np.ndarray:
    k = min(int(k), len(p))
    mask = np.zeros(len(p), dtype=bool)
    if k > 0:
        mask[np.argsort(-p, kind="stable")[:k]] = True
    return mask


def precision_at_k(y: np.ndarray, p: np.ndarray, k: int) -> float:
    m = _top_k_mask(p, k)
    return float(y[m].sum() / m.sum()) if m.sum() else float("nan")


def recall_at_k(y: np.ndarray, p: np.ndarray, k: int) -> float:
    m = _top_k_mask(p, k)
    return float(y[m].sum() / y.sum()) if y.sum() else float("nan")


def threshold_for_budget(p: np.ndarray, budget: int) -> float:
    budget = min(int(budget), len(p))
    if budget <= 0:
        return float("inf")
    return float(np.sort(p)[::-1][budget - 1])


def summary(y: np.ndarray, p: np.ndarray, budget: int) -> dict:
    thr = threshold_for_budget(p, budget)
    pred = p >= thr
    tp = int((pred & (y == 1)).sum())
    return {
        "n": int(len(y)),
        "n_pos": int(y.sum()),
        "pr_auc": pr_auc(y, p),
        "precision_at_k": precision_at_k(y, p, budget),
        "recall_at_k": recall_at_k(y, p, budget),
        "precision": float(tp / pred.sum()) if pred.sum() else float("nan"),
        "recall": float(tp / y.sum()) if y.sum() else float("nan"),
        "threshold": thr,
        "brier": float(brier_score_loss(y, np.clip(p, 0, 1))) if len(set(y)) > 1 else float("nan"),
    }
```

- [ ] **Step 5: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_cv.py tests/test_metrics.py -v`
Expected: PASS, 9 тестов

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/cv.py src/mkl/metrics.py tests/test_cv.py tests/test_metrics.py
git commit -m "feat: walk-forward с embargo и метрики под бюджет алертов"
```

---

### Task 11: Журнал экспериментов и обучение головы

**Files:**
- Create: `src/mkl/experiments.py`, `src/mkl/train.py`, `tests/test_experiments.py`, `tests/test_train.py`

**Interfaces:**
- Produces: `experiments.log(record: dict) -> None` дописывает строку в `experiments/log.jsonl`; `experiments.load() -> pl.DataFrame`; `experiments.best(head: str, metric: str) -> dict`; `train.run(head: str, features: pl.DataFrame, labels: pl.DataFrame, splits: list[Split], params: dict, budget_per_day: int) -> dict` возвращает `{"folds": [...], "mean": {...}, "model": booster, "feature_names": [...]}`

Бюджет алертов: `budget = budget_per_day * число суток в тесте`. По ISA-18.2 разумный старт — 20 предиктивных алертов в сутки на всю сеть.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_experiments.py
import json

from mkl import experiments


def test_log_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "log.jsonl")
    experiments.log({"head": "A", "step": "B1", "pr_auc": 0.1})
    experiments.log({"head": "A", "step": "B3", "pr_auc": 0.4})
    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[1])["step"] == "B3"


def test_log_stamps_time_and_git_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "log.jsonl")
    experiments.log({"head": "A", "step": "B1"})
    rec = json.loads((tmp_path / "log.jsonl").read_text(encoding="utf-8").strip())
    assert "ts" in rec and "git_sha" in rec


def test_best_returns_highest_metric(tmp_path, monkeypatch):
    monkeypatch.setattr(experiments, "LOG_PATH", tmp_path / "log.jsonl")
    experiments.log({"head": "A", "step": "B1", "pr_auc": 0.1})
    experiments.log({"head": "A", "step": "B3", "pr_auc": 0.4})
    experiments.log({"head": "B", "step": "B3", "pr_auc": 0.9})
    assert experiments.best("A", "pr_auc")["step"] == "B3"
```

```python
# tests/test_train.py
import datetime as dt

import numpy as np
import polars as pl

from mkl import cv, train


def test_run_learns_a_separable_signal():
    rng = np.random.default_rng(0)
    days = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(400)]
    rows = []
    for d in days:
        for ch in range(20):
            y = int(rng.random() < 0.2)
            rows.append({"ch": ch, "day": d, "x": y * 3.0 + rng.normal(), "y": y})
    df = pl.DataFrame(rows)
    feats = df.select(["ch", "day", "x"])
    labels = df.select(["ch", "day", "y"])
    splits = cv.walk_forward(days, n_splits=2, test_days=30, embargo_days=31)
    out = train.run("test", feats, labels, splits,
                    params={"n_estimators": 50, "verbose": -1}, budget_per_day=5)
    assert out["mean"]["pr_auc"] > 0.5
    assert len(out["folds"]) == 2
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_experiments.py tests/test_train.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.experiments'`

- [ ] **Step 3: Реализовать experiments.py**

```python
import datetime as dt
import json
import subprocess

import polars as pl

from .config import PATHS

LOG_PATH = PATHS.experiments / "log.jsonl"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PATHS.root, text=True
        ).strip()
    except Exception:
        return "unknown"


def log(record: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
               "git_sha": _git_sha(), **record}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def load() -> pl.DataFrame:
    if not LOG_PATH.exists():
        return pl.DataFrame()
    return pl.read_ndjson(LOG_PATH)


def best(head: str, metric: str = "pr_auc") -> dict:
    df = load()
    if df.is_empty():
        return {}
    sub = df.filter(pl.col("head") == head).drop_nulls(metric)
    if sub.is_empty():
        return {}
    return sub.sort(metric, descending=True).head(1).to_dicts()[0]
```

- [ ] **Step 4: Реализовать train.py**

```python
import lightgbm as lgb
import numpy as np
import polars as pl

from . import metrics
from .cv import Split

DEFAULT_PARAMS = {
    "objective": "binary",
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_child_samples": 100,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "reg_lambda": 1.0,
    "verbose": -1,
    "n_jobs": 8,
}

KEYS = ("ch", "obj", "seg", "day")


def _matrix(df: pl.DataFrame) -> tuple[np.ndarray, list[str]]:
    cols = [c for c in df.columns
            if c not in KEYS and df.schema[c].is_numeric()]
    return df.select(cols).to_numpy(), cols


def run(head: str, features: pl.DataFrame, labels: pl.DataFrame,
        splits: list[Split], params: dict | None = None,
        budget_per_day: int = 20) -> dict:
    join_keys = [k for k in KEYS if k in features.columns and k in labels.columns]
    data = features.join(labels, on=join_keys, how="inner")
    p = {**DEFAULT_PARAMS, **(params or {})}

    folds, model, names = [], None, []
    for s in splits:
        tr = data.filter((pl.col("day") >= s.train_start) & (pl.col("day") <= s.train_end))
        te = data.filter((pl.col("day") >= s.test_start) & (pl.col("day") <= s.test_end))
        if tr.is_empty() or te.is_empty() or tr["y"].sum() == 0:
            continue
        Xtr, names = _matrix(tr.drop("y"))
        Xte, _ = _matrix(te.drop("y"))
        ytr, yte = tr["y"].to_numpy(), te["y"].to_numpy()
        pos = max(int(ytr.sum()), 1)
        model = lgb.LGBMClassifier(**p, scale_pos_weight=(len(ytr) - pos) / pos)
        model.fit(Xtr, ytr)
        proba = model.predict_proba(Xte)[:, 1]
        n_days = (s.test_end - s.test_start).days + 1
        res = metrics.summary(yte, proba, budget=budget_per_day * n_days)
        res["test_start"], res["test_end"] = str(s.test_start), str(s.test_end)
        folds.append(res)

    keys = ("pr_auc", "precision_at_k", "recall_at_k", "precision", "recall", "brier")
    mean = {k: float(np.nanmean([f[k] for f in folds])) for k in keys} if folds else {}
    return {"head": head, "folds": folds, "mean": mean,
            "model": model, "feature_names": names}
```

- [ ] **Step 5: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_experiments.py tests/test_train.py -v`
Expected: PASS, 4 теста

- [ ] **Step 6: Коммит**

```bash
git add src/mkl/experiments.py src/mkl/train.py tests/test_experiments.py tests/test_train.py
git commit -m "feat: журнал экспериментов и обучение головы на walk-forward"
```

---

### Task 12: Эксперименты E0, E1 и лестница бейзлайнов для головы A

**Files:**
- Create: `scripts/exp_a_failure.py`, `reports/head_a.md`

**Interfaces:**
- Consumes: всё предыдущее
- Produces: записи в `experiments/log.jsonl` с полями `head="A"`, `step`, `variant`, `window`, `pr_auc`, `precision_at_k`, `recall_at_k`, `n_pos`

Порядок обязателен: сначала E0 и E1 закрывают развилки, затем лестница B0→B7. Каждая ступень сравнивается с предыдущей; проигравшая откатывается и фиксируется в журнале как отклонённая гипотеза.

- [ ] **Step 1: Реализовать скрипт лестницы**

```python
# scripts/exp_a_failure.py
import datetime as dt
import sys

import numpy as np
import polars as pl

from mkl import cv, db, experiments, labels, metrics, store, train
from mkl.config import EMBARGO_DAYS, HOLDOUT_START, PATHS
from mkl.features import compute

sys.stdout.reconfigure(encoding="utf-8")

TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)


def load(window_start: dt.date, variant: str) -> tuple[pl.DataFrame, pl.DataFrame]:
    con = db.connect()
    con.execute(f"CREATE VIEW daily_channel AS SELECT * FROM read_parquet('{PATHS.interim / 'daily_channel.parquet'}')")
    con.execute(f"CREATE VIEW episodes AS SELECT * FROM read_parquet('{PATHS.interim / 'episodes.parquet'}')")
    con.execute(f"CREATE VIEW group_outages AS SELECT * FROM read_parquet('{PATHS.interim / 'group_outages.parquet'}')")
    labels.build_sensor_failure(con, variant=variant, horizon_days=1)
    lab = con.execute(
        "SELECT ch, day, y FROM label_failure WHERE day >= ? AND day <= ?",
        [window_start, TRAIN_END],
    ).pl()
    con.close()
    feats = store.read_slice("sensor", window_start, TRAIN_END)
    return feats, lab


def evaluate(step: str, feats, lab, splits, params=None, cols=None, note=""):
    f = feats.select(cols) if cols else feats
    out = train.run("A", f, lab, splits, params=params, budget_per_day=20)
    experiments.log({"head": "A", "step": step, "note": note,
                     "n_features": len(out["feature_names"]), **out["mean"]})
    print(f"{step:6} {note:34} PR-AUC={out['mean'].get('pr_auc', float('nan')):.4f} "
          f"P@k={out['mean'].get('precision_at_k', float('nan')):.3f} "
          f"R@k={out['mean'].get('recall_at_k', float('nan')):.3f}", flush=True)
    return out


def main() -> None:
    # --- E0: окно обучения ---
    e0 = {}
    for name, start in [("2019+", dt.date(2019, 1, 1)), ("2023+", dt.date(2023, 1, 1))]:
        feats, lab = load(start, "L3")
        days = sorted(lab["day"].unique().to_list())
        splits = cv.walk_forward(days, n_splits=3, test_days=30, embargo_days=EMBARGO_DAYS)
        out = evaluate("E0", feats, lab, splits, note=f"окно {name}")
        e0[name] = out["mean"].get("pr_auc", float("nan"))
    window_start = dt.date(2019, 1, 1) if e0["2019+"] > e0["2023+"] else dt.date(2023, 1, 1)
    print(f"E0 выбрал окно: {window_start}", flush=True)

    # --- E1: вариант метки ---
    e1 = {}
    for variant in ("L1", "L2", "L3", "L5"):
        feats, lab = load(window_start, variant)
        days = sorted(lab["day"].unique().to_list())
        splits = cv.walk_forward(days, n_splits=3, test_days=30, embargo_days=EMBARGO_DAYS)
        out = evaluate("E1", feats, lab, splits, note=f"метка {variant}")
        e1[variant] = out["mean"].get("pr_auc", float("nan"))
    variant = max(e1, key=lambda k: (e1[k] if e1[k] == e1[k] else -1))
    print(f"E1 выбрал метку: {variant}", flush=True)

    feats, lab = load(window_start, variant)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=30, embargo_days=EMBARGO_DAYS)

    # --- B0: случайный ---
    rng = np.random.default_rng(0)
    joined = feats.join(lab, on=["ch", "day"], how="inner")
    y = joined["y"].to_numpy()
    experiments.log({"head": "A", "step": "B0", "note": "случайный",
                     **metrics.summary(y, rng.random(len(y)), budget=20 * 90)})

    # --- B1: текущая практика ОДС ---
    b1_score = joined["n_alarms_w7"].fill_null(0).to_numpy().astype(float)
    b1 = metrics.summary(y, b1_score, budget=20 * 90)
    experiments.log({"head": "A", "step": "B1", "note": "правило ОДС: тревоги за 7 сут", **b1})
    print(f"B1     правило ОДС                       PR-AUC={b1['pr_auc']:.4f} "
          f"P@k={b1['precision_at_k']:.3f} R@k={b1['recall_at_k']:.3f}", flush=True)

    # --- B2..B7 ---
    keys = ["ch", "day"]
    simple = keys + [c for c in ["n_alarms_w7", "n_alarms_w30", "n_bad_w7", "n_bad_w30",
                                 "n_chatter_1min_w7", "days_since_last_alarm",
                                 "days_since_last_bad", "silence_ratio",
                                 "n_events_w7", "age_days"] if c in feats.columns]
    evaluate("B2", feats, lab, splits, cols=simple,
             params={"n_estimators": 200}, note="10 ручных фич")

    peer_cols = [c for c in feats.columns if c.startswith("peer_")]
    spat_cols = [c for c in feats.columns if c.startswith("nbr_") or c == "dist_to_nearest_bad"]
    base_cols = [c for c in feats.columns if c not in peer_cols + spat_cols]

    evaluate("B3", feats, lab, splits, cols=base_cols, note="LightGBM без peer/spatial")
    evaluate("B4", feats, lab, splits, cols=base_cols + peer_cols, note="+ peer-relative")
    best = evaluate("B5", feats, lab, splits, note="+ пространственные")

    evaluate("B7", feats, lab, splits,
             params={"n_estimators": 1200, "learning_rate": 0.02, "num_leaves": 127},
             note="долгое обучение")

    print("\nЛучшее по журналу:", experiments.best("A", "pr_auc"), flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Прогнать лестницу**

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/exp_a_failure.py`
Expected: журнал пополнен записями E0, E1, B0…B7; каждая ступень напечатала PR-AUC, P@k, R@k

- [ ] **Step 3: Проверить, что B3 бьёт B1**

Run: `& U:\hackathon\.venv\Scripts\python.exe -c "from mkl import experiments; import polars as pl; d=experiments.load().filter(pl.col('head')=='A'); print(d.select(['step','note','pr_auc','precision_at_k','recall_at_k']).to_pandas().to_string())"`
Expected: `pr_auc` у B3 строго больше, чем у B1. Если нет — это сигнал, что фичи не несут сигнала; зафиксировать в отчёте и пересмотреть метку, а не подкручивать гиперпараметры.

- [ ] **Step 4: Аудит метки на 250 случаях**

Спека требует проверить, что метка отражает реальный отказ, а не артефакт эвристики. Без этого заявленный Precision недоказуем: иначе мы измеряем согласие модели с собственным правилом.

```python
# scripts/audit_labels.py
import sys

from mkl import db
from mkl.config import PATHS

sys.stdout.reconfigure(encoding="utf-8")
con = db.connect()
db.attach_events(con)
for t in ("episodes", "group_outages"):
    con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{PATHS.interim / (t + '.parquet')}')")

print(con.execute("""
WITH sample AS (
  SELECT ch, obj, stype, t_start, t_end, dur_s, n_events, states
  FROM episodes
  WHERE dur_s >= 3600 AND NOT is_group AND year(t_start) = 2025
  USING SAMPLE 250 ROWS (reservoir, 42)
)
SELECT s.*,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch
          AND e.ts BETWEEN s.t_start - INTERVAL 24 HOUR AND s.t_start) AS ev_24h_before,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch
          AND e.ts BETWEEN s.t_start - INTERVAL 7 DAY AND s.t_start - INTERVAL 24 HOUR) / 6.0
          AS ev_daily_baseline,
       (SELECT count(*) FROM ev e WHERE e.ch = s.ch AND e.alarm
          AND e.ts BETWEEN s.t_start - INTERVAL 7 DAY AND s.t_start) AS alarms_7d_before
FROM sample s ORDER BY dur_s DESC
""").df().to_csv(PATHS.root / "reports" / "label_audit.csv", index=False))

print(con.execute("""
WITH sample AS (
  SELECT ch, t_start, dur_s FROM episodes
  WHERE dur_s >= 3600 AND NOT is_group AND year(t_start) = 2025
  USING SAMPLE 250 ROWS (reservoir, 42)
)
SELECT
  count(*) AS n,
  count(*) FILTER (WHERE dur_s >= 86400) AS longer_than_day,
  round(median(dur_s)/3600, 1) AS med_hours
FROM sample
""").df().to_string(index=False))
con.close()
```

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/audit_labels.py`
Expected: `reports/label_audit.csv` на 250 строк. Проверить глазами первые 30: у настоящего отказа перед `t_start` активность канала обрывается или резко растёт число тревог. Если у большинства выборки `ev_24h_before` близко к `ev_daily_baseline` и тревог перед эпизодом нет — метка ловит не отказ, а плановое отключение; тогда вернуться к E1 и выбрать другой вариант.

- [ ] **Step 5: Записать отчёт**

Записать в `reports/head_a.md`: выбранное окно, выбранную метку, таблицу лестницы, итог аудита метки, вывод о том, бьёт ли модель правило ОДС, и на сколько.

- [ ] **Step 6: Коммит**

```bash
git add scripts/exp_a_failure.py scripts/audit_labels.py reports/head_a.md reports/label_audit.csv experiments/log.jsonl
git commit -m "exp: лестница бейзлайнов головы A, выбор окна и метки замером, аудит метки"
```

---

### Task 13: Головы A′, B, C, D

**Files:**
- Create: `scripts/exp_other_heads.py`, `configs/heads.yaml`, `reports/heads_bcd.md`

**Interfaces:**
- Consumes: `labels.build_group_outage`, `labels.build_fire`, `labels.build_intrusion`, `labels.build_wear`, `train.run`
- Produces: записи в журнале с `head` из `{"A_prime", "B", "C", "D"}`; агрегированные фичестор-срезы `object.parquet`, `segment.parquet`

Головы A′, B и C работают на укрупнённой сущности, поэтому им нужен свой срез фичестора: агрегаты `feat_ext` по объекту (и по участку для B).

- [ ] **Step 1: Написать агрегацию фич до объекта и участка**

```python
# добавить в src/mkl/features/compute.py
def build_object_level(con, source: str = "feat_ext") -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_object AS
    SELECT obj, day,
           count(*)          AS n_channels,
           sum(n_events)     AS n_events,
           sum(n_alarms)     AS n_alarms,
           sum(n_bad)        AS n_bad,
           sum(n_fire)       AS n_fire,
           sum(n_intrusion)  AS n_intrusion,
           sum(n_chatter_1min) AS n_chatter_1min,
           avg(silence_ratio)  AS silence_ratio_mean,
           max(silence_ratio)  AS silence_ratio_max,
           avg(age_days)       AS age_days_mean,
           sum(n_alarms_w7)    AS n_alarms_w7,
           sum(n_alarms_w30)   AS n_alarms_w30,
           sum(n_bad_w7)       AS n_bad_w7,
           sum(n_bad_w30)      AS n_bad_w30,
           any_value(dow) AS dow, any_value(month) AS month,
           any_value(is_weekend) AS is_weekend, any_value(is_holiday) AS is_holiday,
           any_value(doy_sin) AS doy_sin, any_value(doy_cos) AS doy_cos
    FROM {source} WHERE obj IS NOT NULL GROUP BY obj, day
    """)


def build_segment_level(con, source: str = "feat_ext", seg_size: float = 10.0) -> None:
    con.execute(f"""
    CREATE OR REPLACE TABLE feat_segment AS
    SELECT obj, CAST(floor(coalesce(picket,0)/{seg_size}) AS INTEGER) AS seg, day,
           count(*) AS n_channels,
           sum(n_events) AS n_events, sum(n_alarms) AS n_alarms,
           sum(n_fire) AS n_fire, sum(n_bad) AS n_bad,
           max(val_max) AS temp_max, avg(val_mean) AS temp_mean,
           max(val_max) - min(val_min) AS temp_range,
           sum(n_alarms_w7) AS n_alarms_w7, sum(n_fire) AS n_fire_day,
           any_value(dow) AS dow, any_value(month) AS month,
           any_value(doy_sin) AS doy_sin, any_value(doy_cos) AS doy_cos
    FROM {source} WHERE obj IS NOT NULL GROUP BY obj, seg, day
    """)
```

- [ ] **Step 2: Написать тест на агрегацию**

```python
# добавить в tests/test_store.py
def test_object_aggregation_sums_channels():
    import duckdb
    from mkl.features import compute
    con = duckdb.connect(":memory:")
    con.execute("""CREATE TABLE feat_ext AS SELECT * FROM (VALUES
        ('A', DATE '2025-01-01', 1, 10, 1.0, 5, 0.5, 100.0, 2, 3, 4, 5, 1, 1, 0, 0, 0.0, 1.0),
        ('A', DATE '2025-01-01', 2, 20, 2.0, 7, 0.5, 200.0, 2, 3, 4, 5, 1, 1, 0, 0, 0.0, 1.0)
    ) AS t(obj, day, ch, n_events, picket, n_alarms, silence_ratio, age_days,
           n_bad, n_fire, n_intrusion, n_chatter_1min, n_alarms_w7, n_alarms_w30,
           n_bad_w7, n_bad_w30, doy_sin, doy_cos)""")
    con.execute("ALTER TABLE feat_ext ADD COLUMN dow INTEGER DEFAULT 1")
    con.execute("ALTER TABLE feat_ext ADD COLUMN month INTEGER DEFAULT 1")
    con.execute("ALTER TABLE feat_ext ADD COLUMN is_weekend INTEGER DEFAULT 0")
    con.execute("ALTER TABLE feat_ext ADD COLUMN is_holiday INTEGER DEFAULT 0")
    compute.build_object_level(con)
    row = con.execute("SELECT n_channels, n_events, n_alarms FROM feat_object").fetchone()
    assert row == (2, 30, 12)
    con.close()
```

- [ ] **Step 3: Запустить тест**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_store.py -v`
Expected: PASS, 4 теста

- [ ] **Step 4: Реализовать скрипт голов**

```python
# scripts/exp_other_heads.py
import datetime as dt
import sys

import polars as pl

from mkl import cv, db, experiments, labels, store, train
from mkl.config import EMBARGO_DAYS, EMBARGO_DAYS_WEAR, HOLDOUT_START, PATHS
from mkl.features import compute

sys.stdout.reconfigure(encoding="utf-8")
TRAIN_END = HOLDOUT_START - dt.timedelta(days=1)
WINDOW_START = dt.date(2023, 1, 1)


def prepare() -> None:
    con = db.connect()
    con.execute(f"CREATE VIEW feat_ext AS SELECT * FROM read_parquet('{PATHS.features / 'sensor.parquet'}')")
    compute.build_object_level(con)
    compute.build_segment_level(con)
    store.write(con, "feat_object", "object")
    store.write(con, "feat_segment", "segment")
    con.close()


def label_table(builder, name: str, horizon: int) -> pl.DataFrame:
    con = db.connect()
    for t in ("daily_channel", "episodes", "group_outages"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{PATHS.interim / (t + '.parquet')}')")
    builder(con, horizon_days=horizon)
    df = con.execute(f"SELECT * FROM label_{name} WHERE day >= ? AND day <= ?",
                     [WINDOW_START, TRAIN_END]).pl()
    con.close()
    return df


def run_head(head: str, feature_name: str, lab: pl.DataFrame,
             embargo: int, budget: int, note: str) -> None:
    feats = store.read_slice(feature_name, WINDOW_START, TRAIN_END)
    days = sorted(lab["day"].unique().to_list())
    splits = cv.walk_forward(days, n_splits=3, test_days=30, embargo_days=embargo)
    out = train.run(head, feats, lab, splits, budget_per_day=budget)
    experiments.log({"head": head, "step": "B5", "note": note, **out["mean"]})
    m = out["mean"]
    print(f"{head:8} {note:26} PR-AUC={m.get('pr_auc', float('nan')):.4f} "
          f"P@k={m.get('precision_at_k', float('nan')):.3f} "
          f"R@k={m.get('recall_at_k', float('nan')):.3f}", flush=True)


def main() -> None:
    prepare()
    run_head("A_prime", "object", label_table(labels.build_group_outage, "group_outage", 1),
             EMBARGO_DAYS, 5, "массовый отказ объекта")
    run_head("B", "segment", label_table(labels.build_fire, "fire", 1),
             EMBARGO_DAYS, 10, "пожарный риск участка")
    run_head("C", "object", label_table(labels.build_intrusion, "intrusion", 1),
             EMBARGO_DAYS, 5, "НСД по объекту")
    run_head("D", "sensor", label_table(labels.build_wear, "wear", 7),
             EMBARGO_DAYS_WEAR, 3, "износ агрегатов")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Прогнать**

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/exp_other_heads.py`
Expected: четыре строки с метриками, записи в журнале

- [ ] **Step 6: Записать configs/heads.yaml и отчёт**

```yaml
# configs/heads.yaml
A:
  entity: [ch, day]
  feature_set: sensor
  horizon_days: 1
  embargo_days: 31
  budget_per_day: 20
  label: label_failure
A_prime:
  entity: [obj, day]
  feature_set: object
  horizon_days: 1
  embargo_days: 31
  budget_per_day: 5
  label: label_group_outage
B:
  entity: [obj, seg, day]
  feature_set: segment
  horizon_days: 1
  embargo_days: 31
  budget_per_day: 10
  label: label_fire
C:
  entity: [obj, day]
  feature_set: object
  horizon_days: 1
  embargo_days: 31
  budget_per_day: 5
  label: label_intrusion
D:
  entity: [ch, day]
  feature_set: sensor
  horizon_days: 7
  embargo_days: 37
  budget_per_day: 3
  label: label_wear
```

Записать в `reports/heads_bcd.md` таблицу метрик по каждой голове и вывод, какие из них дотягивают до Precision > 0.7 и Recall > 0.5, а какие нет и почему.

- [ ] **Step 7: Коммит**

```bash
git add scripts/exp_other_heads.py configs/heads.yaml reports/heads_bcd.md src/mkl/features/compute.py tests/test_store.py experiments/log.jsonl
git commit -m "exp: головы массового отказа, пожара, НСД и износа на агрегированных срезах"
```

---

### Task 14: Калибровка, порог, скоринг и финальная оценка на отложенном периоде

**Files:**
- Create: `src/mkl/calibrate.py`, `src/mkl/serve.py`, `tests/test_calibrate.py`, `tests/test_serve.py`, `scripts/final_eval.py`, `reports/final.md`

**Interfaces:**
- Produces: `calibrate.fit_isotonic(p_val, y_val) -> IsotonicRegression`; `calibrate.apply(iso, p) -> np.ndarray`; `serve.fit_and_save(head: str) -> Path`; `serve.score(head: str, asof: date) -> pl.DataFrame` с колонками `entity_keys..., risk, alert`

Отложенный период 2026-01-01…2026-06-30 используется здесь **впервые**. Любые правки моделей после взгляда на него запрещены — это финальный замер.

- [ ] **Step 1: Написать падающий тест на калибровку и скоринг**

```python
# tests/test_calibrate.py
import numpy as np

from mkl import calibrate


def test_isotonic_improves_brier_on_miscalibrated_scores():
    rng = np.random.default_rng(0)
    y = (rng.random(2000) < 0.3).astype(int)
    raw = np.clip(y * 0.5 + rng.normal(0, 0.2, 2000) + 0.25, 0, 1)
    skewed = raw ** 3
    iso = calibrate.fit_isotonic(skewed, y)
    cal = calibrate.apply(iso, skewed)
    assert np.mean((cal - y) ** 2) < np.mean((skewed - y) ** 2)


def test_apply_keeps_values_in_unit_interval():
    rng = np.random.default_rng(1)
    y = (rng.random(500) < 0.5).astype(int)
    p = rng.random(500)
    iso = calibrate.fit_isotonic(p, y)
    out = calibrate.apply(iso, np.array([0.0, 0.5, 1.0]))
    assert out.min() >= 0.0 and out.max() <= 1.0
```

```python
# tests/test_serve.py
import datetime as dt

from mkl import serve


def test_score_returns_alert_flag_and_respects_budget(tmp_path, monkeypatch):
    import numpy as np
    import polars as pl

    df = pl.DataFrame({"ch": range(100), "risk": np.linspace(0, 1, 100)})
    out = serve._apply_budget(df, budget=10)
    assert out["alert"].sum() == 10
    assert out.filter(pl.col("alert"))["risk"].min() >= out.filter(~pl.col("alert"))["risk"].max()
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_calibrate.py tests/test_serve.py -v`
Expected: FAIL с `ModuleNotFoundError: No module named 'mkl.calibrate'`

- [ ] **Step 3: Реализовать calibrate.py**

```python
import numpy as np
from sklearn.isotonic import IsotonicRegression


def fit_isotonic(p: np.ndarray, y: np.ndarray) -> IsotonicRegression:
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(p, y)
    return iso


def apply(iso: IsotonicRegression, p: np.ndarray) -> np.ndarray:
    return np.clip(iso.predict(p), 0.0, 1.0)
```

- [ ] **Step 4: Реализовать serve.py**

```python
import datetime as dt
import pickle
from pathlib import Path

import polars as pl
import yaml

from . import store
from .config import PATHS

HEADS = PATHS.root / "configs" / "heads.yaml"


def load_heads() -> dict:
    return yaml.safe_load(HEADS.read_text(encoding="utf-8"))


def _apply_budget(df: pl.DataFrame, budget: int) -> pl.DataFrame:
    thr = df["risk"].sort(descending=True)[min(budget, df.height) - 1]
    return df.with_columns((pl.col("risk") >= thr).alias("alert"))


def model_path(head: str) -> Path:
    return PATHS.models / f"{head}.pkl"


def save(head: str, model, iso, feature_names: list[str]) -> None:
    PATHS.models.mkdir(parents=True, exist_ok=True)
    with model_path(head).open("wb") as f:
        pickle.dump({"model": model, "iso": iso, "features": feature_names,
                     "feature_set_version": store.load_registry()}, f)


def score(head: str, asof: dt.date | None = None) -> pl.DataFrame:
    cfg = load_heads()[head]
    with model_path(head).open("rb") as f:
        art = pickle.load(f)
    feats = (store.latest_snapshot(cfg["feature_set"]) if asof is None
             else store.read_slice(cfg["feature_set"], asof, asof))
    keys = [k for k in cfg["entity"] if k in feats.columns]
    X = feats.select(art["features"]).to_numpy()
    risk = art["model"].predict_proba(X)[:, 1]
    if art["iso"] is not None:
        from .calibrate import apply as cal_apply
        risk = cal_apply(art["iso"], risk)
    out = feats.select(keys).with_columns(pl.Series("risk", risk))
    return _apply_budget(out, cfg["budget_per_day"]).sort("risk", descending=True)
```

- [ ] **Step 5: Запустить тесты**

Run: `& U:\hackathon\.venv\Scripts\python.exe -m pytest tests/test_calibrate.py tests/test_serve.py -v`
Expected: PASS, 3 теста

- [ ] **Step 6: Финальная оценка на отложенном периоде**

```python
# scripts/final_eval.py
import datetime as dt
import sys
import time

import polars as pl

from mkl import calibrate, db, experiments, labels, metrics, serve, store, train
from mkl.config import HOLDOUT_END, HOLDOUT_START, PATHS
from mkl.cv import Split

sys.stdout.reconfigure(encoding="utf-8")

VAL_START = dt.date(2025, 10, 1)
VAL_END = dt.date(2025, 12, 31)
TRAIN_END = VAL_START - dt.timedelta(days=32)


def main() -> None:
    heads = serve.load_heads()
    rows = []
    for head, cfg in heads.items():
        lab = _labels(head, cfg)
        feats = store.read_slice(cfg["feature_set"], dt.date(2023, 1, 1), HOLDOUT_END)
        fit = train.run(head, feats, lab,
                        [Split(dt.date(2023, 1, 1), TRAIN_END, VAL_START, VAL_END)],
                        budget_per_day=cfg["budget_per_day"])
        model, names = fit["model"], fit["feature_names"]

        data = feats.join(lab, on=[k for k in cfg["entity"] if k in lab.columns], how="inner")
        val = data.filter((pl.col("day") >= VAL_START) & (pl.col("day") <= VAL_END))
        hold = data.filter((pl.col("day") >= HOLDOUT_START) & (pl.col("day") <= HOLDOUT_END))
        p_val = model.predict_proba(val.select(names).to_numpy())[:, 1]
        iso = calibrate.fit_isotonic(p_val, val["y"].to_numpy())
        p_hold = calibrate.apply(iso, model.predict_proba(hold.select(names).to_numpy())[:, 1])

        n_days = (HOLDOUT_END - HOLDOUT_START).days + 1
        res = metrics.summary(hold["y"].to_numpy(), p_hold,
                              budget=cfg["budget_per_day"] * n_days)
        experiments.log({"head": head, "step": "FINAL", "note": "отложенный 2026H1", **res})
        serve.save(head, model, iso, names)
        rows.append({"head": head, **res})
        print(f"{head:8} PR-AUC={res['pr_auc']:.4f} P={res['precision']:.3f} "
              f"R={res['recall']:.3f} P@k={res['precision_at_k']:.3f}", flush=True)

    # контроль времени формирования прогноза
    t0 = time.time()
    for head in heads:
        serve.score(head)
    print(f"\nвремя скоринга всех голов: {time.time()-t0:.1f} с (норматив < 300 с)")
    pl.DataFrame(rows).write_csv(PATHS.root / "reports" / "final_metrics.csv")


def _labels(head: str, cfg: dict) -> pl.DataFrame:
    con = db.connect()
    for t in ("daily_channel", "episodes", "group_outages"):
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{PATHS.interim / (t + '.parquet')}')")
    builder = {
        "A": lambda: labels.build_sensor_failure(con, variant="L3", horizon_days=1),
        "A_prime": lambda: labels.build_group_outage(con, horizon_days=1),
        "B": lambda: labels.build_fire(con, horizon_days=1),
        "C": lambda: labels.build_intrusion(con, horizon_days=1),
        "D": lambda: labels.build_wear(con, horizon_days=7),
    }[head]
    builder()
    name = cfg["label"]
    df = con.execute(f"SELECT * FROM {name}").pl()
    con.close()
    return df


if __name__ == "__main__":
    main()
```

Run: `& U:\hackathon\.venv\Scripts\python.exe scripts/final_eval.py`
Expected: таблица метрик по пяти головам на 2026H1 и время скоринга меньше 300 с

- [ ] **Step 7: Записать финальный отчёт**

В `reports/final.md`: таблица «голова — Precision — Recall — PR-AUC — P@k — бюджет алертов», явное указание, какие головы взяли Precision > 0.7 и Recall > 0.5, а какие нет; для недотянувших — разбор причины (редкость метки, нестационарность, отсутствие внешних данных). Приложить сравнение с правилом ОДС (B1).

- [ ] **Step 8: Коммит**

```bash
git add src/mkl/calibrate.py src/mkl/serve.py tests/test_calibrate.py tests/test_serve.py scripts/final_eval.py reports/final.md reports/final_metrics.csv experiments/log.jsonl
git commit -m "feat: калибровка, скоринг под бюджет алертов и финальная оценка на отложенном периоде"
```

---

## Проверка плана на соответствие спеке

| Требование спеки | Задача |
|---|---|
| Дедупликация, строки-заголовки, каналы вне справочника | Task 2 |
| Ф1: метка — затяжной эпизод, дребезг в фичи | Task 3, 5, 6 |
| Ф2: групповые отказы отделены, голова A′ | Task 3, 5, 13 |
| Ф3: пороги молчания по собственным квантилям | Task 6 (`silence_ratio`, `p99_gap_s`) |
| Ф4: возраст и циклы пуск/останов | Task 7 (`age_days`, `n_duty_cycles_w30`) |
| Ф5: пикеты и пространственные фичи | Task 2, 7 |
| Ф6: состояние охраны как контекст | Task 4 (`n_intrusion`, `n_alarms`), Task 13 |
| Пять голов | Task 5, 12, 13 |
| Выбор метки экспериментом (E1) | Task 12 |
| Выбор окна обучения экспериментом (E0) | Task 12 |
| ISA-18.2 фичи | Task 6 |
| Peer-relative | Task 7 |
| Погода и календарь | Task 8 |
| Фичестор, единая точка расчёта, реестр | Task 9 |
| Тест на отсутствие утечки из будущего | Task 9, Step 5 |
| Walk-forward, purge, embargo 31/37 суток | Task 10 |
| PR-AUC, Precision@k, бюджет алертов | Task 10 |
| Запрет SMOTE и point adjustment | Global Constraints; в коде нигде не используются |
| Лестница B0–B7 с опорой на правило ОДС | Task 12 |
| Журнал экспериментов | Task 11 |
| Калибровка и подбор порога | Task 14 |
| Время прогноза < 5 мин | Task 14, Step 6 |
| Отложенный период 2026H1 только в финале | Global Constraints; Task 14 |
| Ручная проверка 200–300 размеченных случаев | Task 12, Step 4 |
