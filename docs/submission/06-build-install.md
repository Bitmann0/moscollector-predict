# Компиляция, сборка, установка и запуск

Раздел закрывает пункт ТЗ §14 «подробные инструкции по компиляции, сборке и установке».
Команды взяты из `README.md`, `ml/README.md`, `Makefile`, compose-файлов,
`.github/workflows/ci.yml` и справок самих скриптов. Пути даны от корня репозитория; если
команда выполняется из другого каталога, он назван рядом с ней.

Компилируется только фронт: `npm run build` в `frontend/` выполняет `tsc --noEmit`
(проверка типов TypeScript) и `vite build` (статика в `frontend/dist/`). Backend и ML на
Python исполняются из исходников; их сборка — установка зависимостей в образ Docker или
в venv.

Пометки «замер 28.09» и «замер 29.09» — проверки на машине разработчика: Windows, Docker
Desktop, стек `compose.yaml` + `compose.real.yaml`, ML в режиме `real` на бандле из данных
заказчика, код из `main`. 28.09 — бандл `bundle-20260928-3` (модели A_link 0,70, PR #30;
номера pull request — в `Bitmann0/moscollector-predict`), 29.09 — `bundle-20260928-5` с
пятью сценариями. `compose.stand.yaml` в замеры не входил, замеров на Linux-ВМ в разделе
нет. Условия и сырые результаты — `docs/submission/08-performance.md` и
`docs/submission/perf/`.

## Требования

### Программы

| Что | Версия | Где закреплено | Для чего |
|---|---|---|---|
| Docker с Compose v2 | для стенда — Compose v2.24 или новее | `README.md`; тег `!reset` в `compose.stand.yaml` | все три способа запуска |
| Python | 3.12 | `requires-python = ">=3.12"` в `pyproject.toml` и `ml/pyproject.toml`; образы `python:3.12-slim` в `Dockerfile` и `ml/Dockerfile` | скрипты `scripts/*.py` на хосте, режим разработки, сборка бандла |
| Node.js | 22 | `"engines": {"node": ">=22"}` в `frontend/package.json`; образ `node:22-slim` в `Dockerfile` | только режим разработки: в Docker фронт собирается внутри образа |
| 7-Zip | `7z`, `7za` или `7zz` в `PATH`; `fetch_bundle.ps1` и `build_bundle.py` находят и `7-Zip\7z.exe` в `Program Files` | `scripts/fetch_bundle.sh`, `scripts/fetch_bundle.ps1`, `ml/scripts/build_bundle.py` | распаковка и упаковка бандла. Пакет: `p7zip-full` или `7zip` на Linux, 7-Zip на Windows, `sevenzip` на macOS |
| `sha256sum` или `shasum` | — | `scripts/fetch_bundle.sh` | проверка бандла; `fetch_bundle.ps1` считает SHA-256 средствами .NET |
| `curl` или `wget` | — | `scripts/fetch_bundle.sh` | только при скачивании бандла по URL |
| duckdb для Python | 1.5.5 в `ml/requirements.lock` | `scripts/replay.py` | чтение parquet бандла при запуске `replay.py` на хосте; в образе ml duckdb есть |

`scripts/smoke_compose.py`, `scripts/preload_demo.py`, `scripts/emulate_ods.py`,
`scripts/emulate_helpdesk.py` и `ml/scripts/build_bundle.py` написаны на стандартной
библиотеке и запускаются без venv. `scripts/replay.py` без duckdb читает только CSV.

Сторонние образы: `postgres:16-alpine` (сервисы `db` и `backup`) и `caddy:2-alpine`
(стенд); `osixia/openldap:1.5.0` — только тестовый каталог `compose.ldap.yaml`. ТЗ §11 требует PostgreSQL 12 и выше; compose поднимает PostgreSQL 16 (решение D4),
тесты backend в CI идут на `postgres:16` и на SQLite.

### Ресурсы

- Стенд по решению D9 плана команды: одна Linux-ВМ, 4 vCPU, 16 ГБ памяти, 60 ГБ SSD, без
  GPU. ОС стенда в задаче PM-10 — Ubuntu 24.04.
- Лимиты памяти в `compose.stand.yaml`: ml 6 ГБ, db 3 ГБ, api 2 ГБ, replay 1 ГБ, backup
  512 МБ, caddy 256 МБ. По комментарию файла это 13 ГБ из 16, остаток — ОС и страничный
  кеш parquet.
- Распакованный бандл `bundle-20260928-5` — 2 178,7 МБ, из них
  `data/features/sensor.parquet` — 1 287,8 МБ (раздел «Состав бандла»).
- Семь суточных бэкапов займут около 3,6 ГБ — оценка по одному дампу 28.09 размером
  521 МБ (раздел «Бэкап и восстановление»).
- При обучении DuckDB берёт до 10 ГБ памяти (`ml/src/mkl/db.py`, функция `connect`),
  поэтому `ml/README.md` запрещает обучать модели на стенде.
- Сеть нужна сборке образов (базовые образы, пакеты PyPI и npm) и стадии `weather` в
  `python -m mkl.cli run`; без сети колонки погоды остаются пустыми.

## Что собирается

### Образ api: `Dockerfile`

Контекст сборки — корень репозитория, исключения — в `.dockerignore`. Две стадии:

1. `node:22-slim`: `npm ci --no-audit --no-fund` по `frontend/package-lock.json`,
   копирование `contracts/` в `/contracts` (фронт импортирует словари из `../contracts`),
   `npm run build`.
2. `python:3.12-slim`: из `pyproject.toml` ставятся только зависимости, сам пакет `app` не
   ставится — код запускается из `/app/backend`. Копия в site-packages увела бы
   `config.ROOT` в `/usr/local/lib` (комментарий в `Dockerfile`). В образ копируются
   `contracts/`, `backend/` и собранный фронт в `/app/backend/app/static/`. Из
   `backend/entrypoint.sh` срезаются символы `\r`, процесс работает под системным
   пользователем `app`, HEALTHCHECK опрашивает `/api/v1/health`.

Старт контейнера задаёт `backend/entrypoint.sh`: `alembic upgrade head`, затем
`python -m app.seed`, затем `uvicorn app.main:app` на порту 8000 с `--proxy-headers` и
`--timeout-keep-alive 75`: простаивающее соединение живёт 75 с, дольше, чем его держит
Caddy на стенде (PR #43).
Заголовки `X-Forwarded-*` принимаются только от адресов из `FORWARDED_ALLOW_IPS` (по
умолчанию `127.0.0.1`). Seed при каждом старте создаёт справочник причин решений и
настройки; при `SEED_DEMO=1` — ещё демо-пользователей и, если настоящего справочника нет,
синтетический справочник объектов. Прогнозов seed не создаёт.

### Образ ml: `ml/Dockerfile`

Контекст — тоже корень репозитория: заглушке нужен `contracts/synthetic_reference.json`.
BuildKit берёт исключения из `ml/Dockerfile.dockerignore`, а не из корневого
`.dockerignore`. Отдельно от compose образ собирается командой из шапки `ml/Dockerfile`:

```bash
docker build -f ml/Dockerfile -t mkl-ml .
```

База — `python:3.12-slim` и `libgomp1` (OpenMP для lightgbm, xgboost и catboost).
Зависимости ставятся отдельным слоем командой `pip install -r requirements.lock`, затем
`pip install --no-deps -e .`. В `ml/requirements.lock` вместо `xgboost` закреплён
`xgboost-cpu` той же версии 3.4.1: модуль тот же, без CUDA. Процесс —
`uvicorn mkl.product_api:app` на порту 8001. При `ML_MODE=stub` (значение образа по
умолчанию) сервис стартует без данных, при `ML_MODE=real` читает бандл из томов. На этом
же образе работает сервис `replay` стенда: ему нужен duckdb.

### Что в образы не попадает

`.dockerignore` образа api исключает, в частности, `ml/`, `data/`, `Materials/`, `models/`,
`bundle/`, `dist/`, `state/`, `.env`, `*.7z`, `*.parquet`, `*.pkl`.
`ml/Dockerfile.dockerignore` пропускает в контекст только `ml/`,
`contracts/synthetic_reference.json` и `contracts/vocabularies.json`, а внутри `ml/`
исключает, в частности, `data`, `models`, `Materials`, `experiments`, `.venv`, `*.parquet`, `*.pkl`,
`*.7z`. Данные заказчика и модели попадают в контейнеры только томами из бандла.

`.gitattributes` хранит `*.sh`, `*.py`, `Dockerfile`, `*.dockerignore`, `Makefile`,
`Caddyfile`, `*.json`, `*.yaml` и `*.yml` с концами строк LF: при клоне на Windows с
`core.autocrlf=true` скрипт с CRLF ломает `/bin/sh` в контейнере.

## Файл `.env`

Compose передаёт `.env` из корня в api (`env_file: .env`) и подставляет из него переменные
в compose-файлы; без этого файла `docker compose up` не запускается.

```bash
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
```

| Переменная | В `.env.example` | Назначение |
|---|---|---|
| `DEMO_PASSWORD` | пусто | общий пароль пользователей `dispatcher`, `technician`, `analyst`, `manager`, `admin`. При `SEED_DEMO=1` пустое значение останавливает seed, и api не стартует |
| `SECRET_KEY` | пусто | ключ подписи cookie-сессий; без него вход не работает (`backend/app/security.py`) |
| `INTEGRATION_API_KEY` | пусто | заголовок `X-API-Key` машинных клиентов: `replay.py`, эмуляторы. Без него `compose.stand.yaml` не разбирается |
| `DEMO_TODAY` | `2026-06-30` | демо-«сегодня» (решение D6) |
| `DEMO_SETTINGS_LOCKED` | `0` | `1` запрещает менять демо-настройки; стенд ставит `1` |
| `SEED_DEMO` | `1` | создавать демо-пользователей; синтетический справочник — только если в `RAW_DATA_DIR` нет настоящего (`backend/app/seed.py`) |
| `ML_MODE` | `stub` | режим сервиса ml в `compose.yaml`; `compose.real.yaml` ставит `real` сам |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | закомментированы; compose подставляет `moscollector` | БД в compose |
| `BUNDLE_DIR` | закомментирован; по умолчанию `./bundle` | каталог бандла для `compose.real.yaml`, `compose.stand.yaml` и `scripts/replay.py` |
| `STAND_DOMAIN` | закомментирован | домен стенда; без него `compose.stand.yaml` не разбирается |
| `DATABASE_URL`, `ML_URL` | закомментированы | только для запуска без Docker; в compose их задаёт `compose.yaml` |
| `LDAP_URL` и остальные `LDAP_*` | закомментированы | вход через каталог LDAP/AD; пустой `LDAP_URL` его выключает (раздел «Вход через LDAP/AD») |

Значения — латиница, цифры, «-» и «_»: compose подставляет `$VAR` внутри значений, а
`POSTGRES_PASSWORD` попадает в `DATABASE_URL` без URL-кодирования (`.env.example`).
Случайную строку для `SECRET_KEY` и `INTEGRATION_API_KEY` печатает
`python -c "import secrets; print(secrets.token_hex(32))"`. Пароли и ключи стенда в
документацию не входят (решение D13).

Скрипты `scripts/` берут `DEMO_PASSWORD`, `INTEGRATION_API_KEY` и `BUNDLE_DIR` из
окружения, а если там их нет — из `.env` в корне (`scripts/_api.py`). Следующие переменные
читаются только из окружения:

| Переменная | Кто читает | Назначение |
|---|---|---|
| `BUNDLE_URL` | `scripts/fetch_bundle.sh`, `scripts/fetch_bundle.ps1` | адрес папки с архивами: по версии `bundle-YYYYMMDD-N` скачивается `$BUNDLE_URL/<версия>.7z` |
| `BUNDLE_PASSWORD` | `scripts/fetch_bundle.*`, `ml/scripts/build_bundle.py` | пароль 7z для запуска без терминала, если архив под паролем. Уходит в 7z ключом `-p` и на время работы 7z виден в списке процессов |
| `MKL_ROOT` | `ml/src/mkl/config.py`, через него — код ML и `ml/scripts/train_latest.py` | корень данных ML; по умолчанию `ml/`, в образе ml — `/srv/ml` |
| `LOAD_MUTATIONS` | `scripts/load_test/locustfile.py` | `1` добавляет в нагрузочный сценарий решения диспетчера |
| `TEST_DATABASE_URL` | `tests/conftest.py` | тесты backend на PostgreSQL вместо временной SQLite |

## Способ 1. Без данных заказчика: ML-заглушка

После заполнения `.env`:

```bash
docker compose up --build
```

То же в фоне — `make up` (`docker compose up -d --build`).

| Сервис | Образ | Порт |
|---|---|---|
| `db` | `postgres:16-alpine`, том `pgdata` | наружу не открыт |
| `ml` | `ml/Dockerfile`, `ML_MODE=stub` | 8001, только внутри сети compose (решение D3) |
| `api` | `Dockerfile` | 8000 |

api стартует после того, как `db` и `ml` прошли healthcheck. Ответы ML строит
`ml/src/mkl/product_stub.py` по синтетическому справочнику
`contracts/synthetic_reference.json`; на этом же стеке идёт смоук в CI.

- Интерфейс — <http://localhost:8000>, Swagger — <http://localhost:8000/docs>. В Swagger
  сначала выполняется `POST /api/v1/auth/login`: ответ ставит HttpOnly cookie, дальше
  браузер отправляет её сам.
- Журнал прогнозов после первого старта пуст. Его наполняет
  `python scripts/preload_demo.py` или, на один день, `POST /api/v1/admin/run-daily` с
  телом `{"asof": "2026-06-29"}` под `admin`.
- Проверка контура — `python scripts/smoke_compose.py`.

`docker compose down` останавливает сервисы, `docker compose down -v` удаляет и том БД.

## Способ 2. Реальные модели: `compose.real.yaml`

### Состав бандла

`compose.real.yaml` переводит ml в `ML_MODE=real` и монтирует бандл C4 (раздел 4 плана
команды) из каталога `${BUNDLE_DIR:-./bundle}`:

| Из бандла | Куда | Сервис |
|---|---|---|
| `data/` | `/srv/ml/data` | ml |
| `models/` | `/srv/ml/models` | ml |
| `configs/features.yaml` | `/srv/ml/configs/features.yaml` | ml |
| `reports/intrusion_eventtime_v2_build.json` | `/srv/ml/reports/intrusion_eventtime_v2_build.json` | ml |
| `Materials/` | `/app/data/raw`, только чтение | api |

Тома ml смонтированы без `:ro`: дообучение ML1-10 пишет в `models/retrain`. Одиночные файлы
должны существовать до старта, иначе Docker создаст на их месте пустые каталоги, и
скоринг упадёт на чтении `features.yaml` (оба замечания — в комментарии
`compose.real.yaml`). Поэтому бандл распаковывается и проверяется до `docker compose up`.

Обязательные файлы перечислены в словаре `REQUIRED` в `ml/scripts/build_bundle.py`, их 16: модель на каждую голову из `MODEL_HEADS` — `A_link`, `B`, `E` — и 13 файлов данных, реестров и справочников:

| Файл | Кто и зачем читает |
|---|---|
| `configs/features.yaml` | реестр фичестора; по нему сервис сверяет модель с признаками |
| `reports/intrusion_eventtime_v2_build.json` | версия и конец v2-кэша охранной очереди; при `version != 2` очередь отвечает `stale` |
| `models/A_link.pkl` | модель A_link |
| `models/B.pkl`, `models/E.pkl` | модели пожарного риска и подтопления |
| `data/features/sensor.parquet` | признаки A_link и D |
| `data/features/object.parquet` | недельная охранная очередь и её факт; признаки E |
| `data/features/segment.parquet` | признаки B по участкам |
| `data/features/intrusion_eventtime_days_v2.parquet` | v2-кэш охранной очереди |
| `data/interim/channels.parquet` | справочник каналов, адреса в алертах; список объектов с насосами для E |
| `data/interim/daily_channel.parquet` | факт A_link, D, B и E, порог правила D |
| `data/interim/episodes.parquet` | эпизоды для факта D и порога правила D |
| `data/interim/group_outages.parquet` | дообучение в контейнере (ML1-10) |
| `data/interim/events_year=2026.parquet` | `scripts/replay.py`: поток и история событий |
| `Materials/справочник_объектов_диспетчер.csv` | api: объекты для журнала, заявок и схемы |
| `Materials/справочник_каналов_датчиков.csv` | api: каналы и пикеты |

Необязательные файлы: `data/interim/maintenance_2026.json` — графики ППР и ТО, без него
контекст алерта A_link получает статус `schedule_not_loaded`; `models/A_link@*.pkl`,
`models/B@*.pkl`, `models/E@*.pkl` — датированные модели для дней июня (раздел «Обучение
датированных моделей»). У головы D файла модели нет: это правило `n_bad_w7`.

При старте api seed сверяет таблицы `ref_objects` и `ref_channels` с двумя CSV из
`Materials/` и удаляет синтетический справочник прошлых запусков (`backend/app/seed.py`).

Версии бандла:

- `bundle-20260928-5` — текущая версия для пяти сценариев: тот же `-4` плюс
  `data/features/segment.parquet` и модели `models/B.pkl`, `models/E.pkl` с четырьмя
  датированными у каждой. 29 файлов, 2 178,7 МБ по сумме размеров; остальные 18
  контрольных сумм совпадают с `-4` (сверка `MANIFEST.sha256` двух версий 29.09). 29.09
  `fetch_bundle.sh` распаковал архив `-5` без `BUNDLE_PASSWORD` и сверил 29 контрольных
  сумм из 29.
- `bundle-20260928-4` — тот же `-3` плюс `data/interim/maintenance_2026.json`: графики ППР
  и ТО от 25.09, нормализованные `ml/scripts/normalize_maintenance_schedules.py`,
  `available_from` 2026-09-25 (сверка `MANIFEST.sha256`). В продуктовом контракте C1
  контекста плановых работ нет, экраны с этим файлом не меняются. Моделей B и E в `-4` и
  более ранних версиях нет: `/ready` сервиса ML перебирает все пилотные головы и ответит
  `missing_data`, а в расчёте B и E получат `error` (`_real_ready` и `_real_score` в
  `ml/src/mkl/product_api.py`; вывод из кода, не проверялось).
- `bundle-20260928-3` собран после перехода A_link на минимум точности 0,70 (PR #30):
  17 файлов, от `bundle-20260928-2` отличаются только пять `models/A_link*.pkl`
  (сверка `MANIFEST.sha256` двух версий). Версии `-1` и `-2` с текущим `main` не работают:
  в `-1` нет каталога `Materials/`, и `fetch_bundle` отклоняет раскладку; модели 0,50
  из `-2` отвергает проверка метаданных `validate_pilot_artifact`.

Замер 28.09, до моделей B и E:

- `build_bundle.py` собрал бандл из 15 файлов, 2 128 МБ; `fetch_bundle.sh` сверил 15
  контрольных сумм из 15. Два CSV из `Materials/` тогда ещё не входили в `REQUIRED`.
- `build_bundle.py --dry-run` отбирал на локальном корне ML 17 файлов, 2 129,7 МБ:
  13 обязательных и четыре датированные модели A_link. Самый крупный файл —
  `data/features/sensor.parquet`, 1 287,8 МБ; файла `maintenance_2026.json` в корне нет.

### Получение и проверка бандла

Бандл распространяется архивом `bundle-YYYYMMDD-N.7z`. Текущий архив
`bundle-20260928-5.7z` (2 144 923 082 байт) лежит на Google Диске:
<https://drive.google.com/file/d/17Dh6tC8sPO0bWPf3ukHCuErg15_bSIg-/view?usp=sharing>.
SHA-256 архива: `b50e2d11f4db53d7c3d2a5e2f423dac63ea576554e1263c7c8325d95d7885705`.
Скачивать его нужно в браузере: для файлов больше 100 МБ Google Диск вместо файла
отдаёт страницу с предупреждением, и `fetch_bundle` по этому URL архив не получит.
Архив упакован без пароля (`7z a -t7z -mx=1`). Тот же бандл собирается из своей копии датасета (раздел «Сборка
бандла из датасета организаторов»); `build_bundle.py --archive` упаковывает его под
паролем датасета организаторов (решение D10).

```bash
sh scripts/fetch_bundle.sh ~/Downloads/bundle-20260928-5.7z ./bundle
```

```powershell
powershell -File scripts\fetch_bundle.ps1 -Version $HOME\Downloads\bundle-20260928-5.7z -Dest .\bundle
```

Первый аргумент (в PowerShell — `-Version`) принимает путь к `.7z`, URL архива, версию
`bundle-YYYYMMDD-N` или распакованный каталог. По версии скрипт ищет `./<версия>.7z`, а
если его нет, скачивает `$BUNDLE_URL/<версия>.7z` в каталог рядом с каталогом назначения;
повторный запуск скачанный архив не качает заново. Распакованный каталог скрипт только
проверяет.

Каталог назначения (по умолчанию `./bundle`) должен отсутствовать или быть пустым. Скрипт
распаковывает архив в `<каталог>.partial` и проверяет две вещи: раскладку томов
`compose.real.yaml` (каталоги `data/`, `models/`, `Materials/`, файлы
`configs/features.yaml`, `reports/intrusion_eventtime_v2_build.json`) и контрольные суммы
`MANIFEST.sha256`. После проверки каталог переименовывается в `<каталог>`, и скрипт
печатает строку `BUNDLE_DIR=…`; её вписывают в `.env`, без неё compose берёт `./bundle`.
В Git Bash на Windows путь печатается в виде `C:/…`, как его ждёт docker compose.

Если архив под паролем, 7z спросит его или возьмёт из `BUNDLE_PASSWORD`.
`fetch_bundle.ps1` сохранён в UTF-8 с BOM, чтобы Windows PowerShell 5.1 читал кириллицу.

### Запуск

```bash
docker compose -f compose.yaml -f compose.real.yaml up -d --build
```

То же — `make up-real`. Холодная загрузка моделей и фичестора дольше старта заглушки,
поэтому `compose.real.yaml` увеличивает `start_period` healthcheck ml до 180 с. Журнал
прогнозов, история событий и поток наполняются командами раздела «Наполнение и проверка».

Замер 29.09: `/score` ML-сервиса по A_link, D, B и E за каждый день 01–30.06 — 9,08 с по
медиане, от 0,21 до 10,63 с. 0,21 с приходится на 01.06: событий за этот день нет, ML сразу
отвечает `no_data` (`docs/submission/08-performance.md`,
`docs/submission/perf/ml_score_june_4heads.jsonl`).

Замер 28.09, A_link и D:

- В прелоаде за 01.06–29.06 A_link ответила `ok` на 13 днях, `empty_valid` на 15 и
  `no_data` на 01.06; D — `ok` на 29.06 и `empty_valid` на остальных днях с данными
  (`docs/submission/perf/preload_demo_0928.txt`).
- Смоук — 8 шагов из 8 (`docs/submission/perf/smoke_0928.txt`).

Backend сохраняет состояния `no_data`, `stale`, `empty_valid` и `error` как есть и не
подменяет их прошлой успешной выдачей (`README.md`).

## Способ 3. Стенд: `compose.stand.yaml`

Стенд (задача PM-10, решения D9 и D13) — третий файл поверх двух первых. Нужен Compose
v2.24 или новее: `ports: !reset []` снимает публикацию порта 8000.

| Что | Как |
|---|---|
| TLS | сервис `caddy` на портах 80 и 443 проксирует запросы на `api:8000`. Сертификат для `STAND_DOMAIN` Caddy получает сам по ACME: DNS домена должен указывать на ВМ, порты 80 и 443 открыты. Для проверки на своей машине подходит `STAND_DOMAIN=localhost`, тогда сертификат выпускает локальный CA Caddy |
| `deploy/Caddyfile` | заголовки `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, заголовок `Server` убран; журнал доступа в JSON в stdout; тело запроса до 200 МБ, как предел загрузки журнала в `backend/app/limits.py`; `flush_interval -1`, чтобы события SSE не копились в буфере прокси; простаивающее соединение к api — не дольше 60 с (`keepalive 60s`), меньше 75 с на стороне api |
| api | порт 8000 наружу закрыт; `DEMO_SETTINGS_LOCKED=1`, `COOKIE_SECURE=1`, `FORWARDED_ALLOW_IPS="*"` — api доверяет `X-Forwarded-Proto` и `X-Forwarded-For` от Caddy |
| `replay` | поток событий 30.06 (раздел «Поток событий 30.06») |
| `backup` | суточный дамп БД (раздел «Бэкап и восстановление») |
| Ресурсы | лимиты памяти из раздела «Ресурсы»; журналы контейнеров — драйвер `json-file`, по 10 МБ, 5 файлов на сервис |

Частоту входа ограничивает api, а не Caddy: в `caddy:2-alpine` нет модуля rate limit
(`deploy/Caddyfile`). После 10 неудачных входов с одного адреса на один логин за 5 минут
api отвечает 429 до конца пятиминутного окна (`backend/app/routers/auth.py`).

`STAND_DOMAIN` и `INTEGRATION_API_KEY` заданы в `compose.stand.yaml` как `${…:?…}`: без
них compose не разбирает файлы.

### Развёртывание

1. Поставить Docker с Compose v2.24+, 7-Zip и Python 3.12; клонировать репозиторий:
   `git clone https://github.com/Bitmann0/moscollector-predict.git` и перейти в
   `moscollector-predict/`; следующие команды выполняются оттуда.
2. `cp .env.example .env` и вписать `DEMO_PASSWORD`, `SECRET_KEY`, `INTEGRATION_API_KEY`,
   `STAND_DOMAIN`.
3. Получить и проверить бандл (`sh scripts/fetch_bundle.sh <архив> ./bundle`), вписать
   напечатанную строку `BUNDLE_DIR=…` в `.env`.
4. Поднять стенд:

   ```bash
   docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml up -d --build
   ```

5. Разово загрузить историю событий 2026-05-02…2026-06-29:

   ```bash
   docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml \
     run --rm replay python /srv/scripts/replay.py --base-url http://api:8000 --bulk
   ```

6. Предзаполнить журнал прогнозов:
   `python scripts/preload_demo.py --base-url https://$STAND_DOMAIN`.
7. Проверить контур: `python scripts/smoke_compose.py --base-url https://$STAND_DOMAIN`.

`$STAND_DOMAIN` в шагах 6 и 7 — переменная оболочки: из `.env` скрипты её не читают. Без
`--read-only` смоук сам запускает run-daily за 29.06 и сохраняет решение диспетчера с
комментарием `smoke_compose.py`. На стенде, уже подготовленном для экспертов, смоук
запускается с `--read-only`: шаги 3 и 6, которые пишут в БД, пропускаются. Время
развёртывания на ВМ не замерено.

### Поток событий 30.06

Сервис `replay` запускает `scripts/replay.py --catch-up --align-to-clock --loop` в образе
ml. На старте он отправляет события 30.06 с 00:00 до текущего времени одной серией без
уведомлений, дальше идёт в реальном темпе по часам МСК. В полночь он удаляет события дня
через `DELETE /api/v1/ingest/day/{day}` и начинает день заново.

Скрипты смонтированы в контейнер из `./scripts`, события — из
`<BUNDLE_DIR>/data/interim`, оба тома только для чтения. Журнал задержки «событие → БД»
пишется в `./state/replay/latency.csv` на хосте. `run_daily` за 30.06 в цикле не
запускается: прогнозы «на сегодня» — это расчёт за 29.06 (раздел 3 плана команды).

### Бэкап и восстановление

Сервис `backup` (`postgres:16-alpine`) при старте и затем раз в 86 400 с пишет
`pg_dump -Fc` в `./state/backups/moscollector-YYYYMMDD-HHMM.dump`: сначала во временный
файл `.part`, после успеха переименовывает его. Хранятся 7 последних дампов.

Восстановление из `README.md`, раздел «Стенд»; api и replay на это время останавливаются,
чтобы не писать в пустую схему:

```bash
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml stop api replay
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml exec -T db pg_restore --clean --if-exists -U moscollector -d moscollector < state/backups/<файл>.dump
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml start api replay
```

`-U moscollector -d moscollector` — значения `POSTGRES_USER` и `POSTGRES_DB` по
умолчанию; если в `.env` они переопределены, подставляются свои.

Замер в ночь на 28.09 на БД после загрузки истории и прелоада на моделях A_link 0,50 (до
PR #30): `pg_dump -Fc` — 74 с, файл 521 МБ.
`pg_restore` этого файла в чистый postgres:16 занял 133 с без нагрузки и 309 с, пока
параллельно шёл подсчёт строк в исходной базе (`README.md`, раздел «Стенд»); код возврата 0
в обоих прогонах. После восстановления в БД 10 277 666 событий и 222 заявки — столько же,
сколько дали загрузка истории и тот прелоад (`docs/submission/perf/backup_restore_0928.txt`).
ТЗ §11 ограничивает восстановление после сбоя 4 часами; восстановление стенда с
пересозданием ВМ, бандла и образов не замерено.

## Вход через LDAP/AD

По умолчанию `LDAP_URL` пуст, и вход идёт только по локальным учётным записям. Каталог
включается переменными `LDAP_*` в `.env` и перезапуском api той же командой
`docker compose … up -d`, которой стек поднимался. Ошибку в этих переменных api находит
при старте, пишет её текст в `docker compose logs api` и не стартует (`check_config` в
`backend/app/directory.py`). Устройство входа, коды ответов и ограничения —
`docs/submission/07-security.md`, раздел 5.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LDAP_URL` | пусто | `ldap://хост:389` или `ldaps://хост:636`; пусто — каталог выключен. Для TLS нужно имя хоста, а не IP-адрес: ldap3 сверяет с сертификатом только DNS-имена |
| `LDAP_STARTTLS` | `0` | `1` — StartTLS поверх `ldap://`; вместе с `ldaps://` не задаётся |
| `LDAP_TLS_CA_FILE` | пусто | путь к CA каталога в PEM внутри контейнера api; пусто — системные CA образа |
| `LDAP_TLS_VERIFY` | `1` | `0` выключает проверку сертификата каталога; только для теста |
| `LDAP_TIMEOUT_S` | `5` | секунд на соединение, на каждый ответ каталога (округляется вверх до целых) и на поиск |
| `LDAP_USER_DN_TEMPLATE` | пусто | DN сотрудника с подстановкой `{login}`; при нём bind идёт сразу под сотрудником |
| `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD` | пусто | служебная учётная запись для поиска сотрудника и его групп; без неё поиск анонимный |
| `LDAP_USER_BASE` | пусто | где искать сотрудника; нужен без шаблона DN и при шаблоне вида `{login}@домен` |
| `LDAP_USER_FILTER` | `(uid={login})` | фильтр поиска сотрудника; в AD — `(sAMAccountName={login})` |
| `LDAP_GROUP_BASE` | пусто | где искать группы сотрудника; пусто — роль только по `memberOf` |
| `LDAP_GROUP_FILTER` | ниже | фильтр групп сотрудника; подставляются `{user_dn}` и `{login}` |
| `LDAP_ROLE_GROUPS` | пусто; при заданном `LDAP_URL` обязателен | `роль=группа;роль=группа` или JSON `{"роль": ["группа", …]}`; группа — CN или DN. Роли — `dispatcher`, `technician`, `analyst`, `manager`, `admin` |
| `LDAP_ALLOW_LOCAL` | `1` | `0` — вход только через каталог, локальные и демо-учётки не пускаются |

`LDAP_GROUP_FILTER` по умолчанию —
`(|(member={user_dn})(uniqueMember={user_dn})(memberUid={login}))`: он находит группы
`groupOfNames`, `groupOfUniqueNames` и `posixGroup`. Значения с пробелами и JSON в `.env`
берутся в одинарные кавычки; знак `$` в значениях недопустим, как и в остальных
переменных `.env`.

### Подключение к AD заказчика

Набросок `.env` для Active Directory. Против AD он не запускался: каталога заказчика у
команды нет (QA-8).

```
LDAP_URL=ldaps://dc01.corp.example:636
LDAP_TLS_CA_FILE=/run/ldap-ca/corp-ca.pem
LDAP_BIND_DN=CN=svc-moscollector,OU=Service,DC=corp,DC=example
LDAP_BIND_PASSWORD=
LDAP_USER_BASE=OU=Users,DC=corp,DC=example
LDAP_USER_FILTER=(sAMAccountName={login})
LDAP_ROLE_GROUPS=admin=MK-Admins;dispatcher=MK-Dispatchers;technician=MK-Technicians;analyst=MK-Analysts;manager=MK-Managers
LDAP_ALLOW_LOCAL=0
SEED_DEMO=0
```

- Файл CA попадает в контейнер api томом из override-файла площадки, например
  `./deploy/corp-ca.pem:/run/ldap-ca/corp-ca.pem:ro`.
- `memberOf` в AD перечисляет только прямые группы. Вложенные группы находит правило
  `LDAP_MATCHING_RULE_IN_CHAIN`: `LDAP_GROUP_BASE=DC=corp,DC=example` и
  `LDAP_GROUP_FILTER=(member:1.2.840.113556.1.4.1941:={user_dn})` (документация Microsoft,
  «Search Filter Syntax»). Этот вариант тоже не проверялся.
- Без служебной учётной записи подходит шаблон UPN: `LDAP_USER_DN_TEMPLATE={login}@corp.example`
  вместе с `LDAP_USER_BASE` и тем же `LDAP_USER_FILTER`. Запись сотрудника и его группы
  api тогда читает под самим сотрудником.
- `SEED_DEMO=0` не создаёт демо-учётки, но строки прошлых запусков остаются в `users`. При
  `LDAP_ALLOW_LOCAL=0` войти под ними нельзя.

### Тестовый каталог: `compose.ldap.yaml`

Override поднимает OpenLDAP `osixia/openldap:1.5.0` (373 МБ) и переключает api на вход
через него:

```bash
docker compose -f compose.yaml -f compose.ldap.yaml up -d --build
```

| Что | Где |
|---|---|
| сотрудники `ldap-dispatcher`, `ldap-technician`, `ldap-analyst`, `ldap-manager`, `ldap-admin` в группах `mk-<роль>s` и `ldap-norole` в группе `mk-visitors`, которая роли не даёт | `deploy/ldap/bootstrap.ldif`; пароли в нём — хеши `{SSHA}` |
| пароли сотрудников, администратора каталога и служебной учётной записи `cn=readonly` — только для теста | `deploy/ldap/test-directory.env.example`; этот файл читают сервисы `ldap` и `api` |
| тестовый CA и сертификат сервера на имена `ldap` и `localhost` | выпускает `deploy/ldap/tls-init.sh` при первом старте, том `ldapcerts`. CA из образа истёк 15.01.2026, поэтому свой |
| настройка api: поиск под `cn=readonly`, StartTLS с проверкой по этому CA, `LDAP_ALLOW_LOCAL=1` | `compose.ldap.yaml`, сервис `api` |
| порты каталога 1389 (LDAP) и 1636 (LDAPS) | только на 127.0.0.1, для тестов с хоста |

Каталог заполняется из LDIF при первом старте с пустым томом `ldapdata`; после правки
LDIF нужен `docker compose -f compose.yaml -f compose.ldap.yaml down -v`. На стенд этот
override не ставится.

Тесты против тестового каталога в CI не входят:

```bash
docker compose -f compose.yaml -f compose.ldap.yaml up -d --wait ldap
docker compose -f compose.yaml -f compose.ldap.yaml cp \
    ldap:/container/service/slapd/assets/certs/ca.crt state/ldap-ca.crt
LDAP_TEST_URL=ldap://localhost:1389 LDAP_TEST_TLS_URL=ldaps://localhost:1636 \
    LDAP_TEST_CA_FILE=state/ldap-ca.crt python -m pytest -q tests/test_directory_live.py
```

Замер 28.09 на машине разработчика: 12 тестов из 12. Без `LDAP_TEST_URL` эти тесты
пропускаются; остальные тесты входа через каталог (`tests/test_directory.py`, подменный
каталог ldap3) идут в CI в общем `pytest -q`.

## Наполнение и проверка

Команды одинаковы для трёх способов запуска. Скрипты по умолчанию обращаются к
`http://127.0.0.1:8000`, а не к `localhost`: urllib пробует `::1` первым, а проброс порта
Docker Desktop на Windows по `::1` зависал (`scripts/_api.py`). Другой адрес задаёт
`--base-url`; у эмуляторов и `replay.py` есть синоним `--api`.

### Журнал прогнозов: `scripts/preload_demo.py`

Скрипт входит под `admin` и вызывает API по порядку:

1. `DELETE /admin/issued-log` — журнал выданного за окно; если окно начинается не позже
   01.06, стирается и неделя до него.
2. `POST /admin/run-daily` с `{"weekly_only": true}` — недельная охранная очередь за
   понедельники с 2026-01-05 до начала окна.
3. `POST /admin/run-daily` за каждый день 2026-06-01…2026-06-29 строго по возрастанию.
   Факт по созревшим прогнозам run-daily запрашивает у ML сам.
4. `POST /admin/emulate-decisions` за 2026-01-05…2026-06-29 — решения и итоги проверки по
   факту с `source=emulated`; заявки окна идут за решениями: подтверждена → в работе →
   выполнена, отменена или остаётся черновиком (PR #36). Решения, итоги и заявки людей
   скрипт не трогает.

```bash
python scripts/preload_demo.py                       # 2026-06-01…2026-06-29
python scripts/preload_demo.py --from 2026-06-22 --dry-run
```

Ключи: `--from`, `--to`, `--weekly-from`, `--timeout` (по умолчанию 300 с на один
run-daily), `--dry-run` (только напечатать план). 30.06 не считается: «сегодня» — это
расчёт за 29.06 (решение D6). Повторный запуск приводит журнал к тому же состоянию. Код
выхода 1 — упал вызов или голова вернула `result_status=error`. Unix-вариант —
`make preload`.

Один день без скрипта: `POST /api/v1/admin/run-daily` под `admin` или
`python -m app.services.daily_run --asof YYYY-MM-DD` в окружении backend
(`docs/architecture.md`).

Замер 28.09: 417 с, ошибок 0. В журнале 154 прогноза в бюджете — `sensor_link` 137,
`equipment_diag` 3, `guard_weekly` 14; у 149 есть факт, 109 эмулированных решений, 87
итогов проверки. Заявок 65: выполнено 36, отменено 8, черновиков 21, эмуляция сдвинула из
черновика 44 (`docs/submission/perf/preload_demo_0928.txt`, `journal_counts_0928.txt`).
Эти числа — по трём сценариям на `bundle-20260928-3`. Замер 29.09 на `bundle-20260928-5` с
пятью сценариями: 460 с, ошибок 0, run-daily за день — 11,6–22,8 с; в журнале за
2026-01-05…2026-06-29 — 499 прогнозов в бюджете, 352 с решением, 352 заявки
(`docs/submission/perf/preload_demo_0929.txt`).

### История событий: `scripts/replay.py --bulk`

Скрипт загружает события 2026-05-02…2026-06-29 из `events_year=2026.parquet` бандла в
`POST /api/v1/ingest/events?notify=false` пачками по 5 000 строк, без уведомлений. История
нужна журналу событий июня и динамике карточки прогноза: карточка берёт события канала за
30 суток до `asof`.

На стенде загрузку запускает команда из шага 5 развёртывания. Без Docker:

```bash
python scripts/replay.py --bulk                      # 2026-05-02…2026-06-29
python scripts/replay.py --bulk --from 2026-06-10    # продолжить прерванную загрузку
```

Для parquet нужен интерпретатор с duckdb, например из `ml/.venv`. Источник по умолчанию —
`<BUNDLE_DIR или bundle>/data/interim/events_year=2026.parquet`, другой путь задаёт
`--source`. Уже принятые строки api считает дублями, поэтому прерванную загрузку можно
запустить заново. Пачку, не принятую из-за сети, 429 или 5xx, скрипт повторяет с паузами
от 1 до 30 с, после восьмой попытки пропускает.

Замер 28.09, PostgreSQL в compose: 10 277 666 строк за 58 дней (за 01.06 событий в бандле
нет) — 2 684 с, 3 829 строк/с, ошибок 0.

### Поток 30.06 без стенда

```bash
python scripts/replay.py --day 2026-06-30 --loop --align-to-clock --catch-up
```

Поток идёт с уведомлениями. `--speed 600` проигрывает сутки за 2,4 мин и предназначен
только для видео. `--latency-log <файл>.csv` пишет по строке на пачку с задержкой
«событие → БД».

### Эмуляторы ОДС и help desk

Журнала ОДС и учётной системы заявок в данных заказчика нет, их роль играют два скрипта.
Пишут они с ключом `INTEGRATION_API_KEY`, читают под демо-пользователем с паролем
`DEMO_PASSWORD` (`dispatcher` у эмулятора ОДС, `technician` у help desk): у роли
integration нет права view.

```bash
python scripts/emulate_ods.py --day 2026-06-30 --count 20   # журнал ОДС за день; повтор не дублирует
python scripts/emulate_helpdesk.py --once                    # подтверждённые заявки — на шаг вперёд
python scripts/emulate_helpdesk.py --speed 60 --interval 5   # с задержками: час за минуту
```

Поле причины у записей эмулятора ОДС начинается с «эмуляция ОДС:»; повторный запуск с тем
же `--seed` строк не добавляет. `emulate_ods.py --loop --interval 60` раз в минуту
перечитывает прогнозы и заявки и шлёт записи, чьё время суток по часам МСК уже наступило.
Эмулятор help desk переводит заявки только по цепочке confirmed → in_progress →
completed: подтвердить черновик и отменить заявку может только роль с правом
`work_order_manage` — диспетчер, руководитель или администратор (`contracts/vocabularies.json`).

### Смоук: `scripts/smoke_compose.py`

Скрипт ждёт ответа `/api/v1/health` до `--wait` секунд (по умолчанию 180), затем проходит
8 шагов:

1. вход `dispatcher` и `GET /me`;
2. `GET /system/status` отвечает 200;
3. run-daily под `admin` на 2026-06-29;
4. `GET /forecasts` не пуст;
5. карточка прогноза открывается;
6. решение диспетчера сохраняется;
7. `GET /` отдаёт `index.html`;
8. `GET /stream` присылает первое событие.

На первом сбое скрипт завершается с кодом 1. `--read-only` пропускает шаги 3 и 6,
`--run-timeout` задаёт таймаут run-daily (по умолчанию 300 с), `--asof` — день run-daily.
Unix-вариант — `make smoke`.

### Нагрузочный тест

Сценарий на 20 пользователей (ТЗ §11) — `scripts/load_test/locustfile.py`. locust в
зависимости проекта не входит:

```bash
pip install locust
mkdir -p data/load_test
DEMO_PASSWORD=... locust -f scripts/load_test/locustfile.py --host http://127.0.0.1:8000 \
    --users 20 --spawn-rate 2 --run-time 10m --headless --csv data/load_test/run
```

p50 и p95 каждого запроса — колонки «50%» и «95%» в `data/load_test/run_stats.csv`, число
ошибок — «Failure Count» там же, тексты ошибок — в `run_failures.csv`. Сценарий только
читает; `LOAD_MUTATIONS=1` добавляет решения диспетчера. Прогон 28.09 —
`docs/submission/08-performance.md`, раздел «Нагрузка»; его CSV —
`docs/submission/perf/locust_20users_10m_*`.

## Режим разработки без Docker

Нужны Python 3.12 и Node 22. В `.env` добавляются две строки; compose их переопределяет,
поэтому `docker compose up` они не мешают:

```
DATABASE_URL=sqlite:///./data/dev.db
ML_URL=http://localhost:8001
```

Команды выполняются из корня репозитория: backend читает `.env` из текущего каталога.
Каталог файла SQLite backend создаёт сам (`backend/app/db.py`).

| Что | Unix (`make`) | Windows (PowerShell) |
|---|---|---|
| Установка backend | `make install` | `py -3.12 -m venv .venv; .venv\Scripts\pip install -e ".[dev]"` |
| Установка фронта | входит в `make install` | `cd frontend; npm ci` |
| Установка ML (тяжёлые зависимости) | `make install-ml` | `py -3.12 -m venv ml\.venv; ml\.venv\Scripts\pip install -r ml\requirements.lock; ml\.venv\Scripts\pip install --no-deps -e ml; ml\.venv\Scripts\pip install pytest httpx` |
| ML-заглушка на :8001 | `make dev-ml` | `$env:PYTHONPATH="ml\src"; .venv\Scripts\uvicorn mkl.product_api:app --port 8001` |
| api на :8000 | `make dev-api` | `.venv\Scripts\alembic -c backend\alembic.ini upgrade head; $env:PYTHONPATH="backend"; .venv\Scripts\python -m app.seed; .venv\Scripts\uvicorn app.main:app --app-dir backend --reload --port 8000` |
| Фронт на :5173, `/api` проксируется на :8000 | `make dev-fe` | `cd frontend; npm run dev` |
| Тесты backend | `make test-backend` | `.venv\Scripts\pytest -q` |
| Тесты ML | `make test-ml` | `cd ml; .venv\Scripts\python -m pytest -q --deselect tests/test_address.py --deselect tests/test_config.py::test_paths_exist --deselect tests/test_db.py` |
| Линтер | `make lint` | `.venv\Scripts\ruff check backend tests scripts` |
| Контракты | `make contracts` | `.venv\Scripts\python scripts\export_contracts.py; cd frontend; npm run gen:api` |

`make install` вызывает `python3` (переменная `PY` в `Makefile`), это должен быть Python
3.12. ML-заглушка запускается из venv backend: ей хватает fastapi, pydantic и pyyaml
(комментарий job `contracts` в `.github/workflows/ci.yml`). Тяжёлые зависимости ML
(xgboost, lightgbm, catboost, polars) ставятся в отдельный `ml/.venv`. Три теста ML
исключены: им нужны данные заказчика, которых нет ни в git, ни в CI.

`scripts/export_contracts.py` пересобирает `contracts/` из кода, `npm run gen:api` —
`frontend/src/api/schema.d.ts` из `contracts/api_v1.openapi.json`. Изменённую схему,
словарь или контрактный ответ перегенерируют и коммитят в том же PR, иначе CI падает.

## Сборка бандла из датасета организаторов

Нужны датасет организаторов и ML-окружение (`make install-ml` или строка «Установка ML» в
разделе «Режим разработки без Docker»). Команды шагов 2–6 выполняются из `ml/`; `python`
в них — интерпретатор `.venv/bin/python` (Windows: `.venv\Scripts\python`).

1. Журналы `ext-journal-YYYY.csv` положить в `ml/data/raw/`, справочники
   `справочник_каналов_датчиков.csv` и `справочник_объектов_диспетчер.csv` — в
   `ml/Materials/`.
2. `python -m mkl.cli run` — приём, эпизоды, суточная панель, погода, фичестор, обучение
   A_link, B и E: стадия `train` вызывает `ml/scripts/train_latest.py` без имён голов, и он
   обучает пилотные головы с моделью. Стадии `ingest` → `states`, `panel`, `weather` →
   `features` → `train` объявлены в `ml/src/mkl/pipeline.py`; стадия пересобирается, если
   хоть один её вход новее выхода.
   Состояние стадий печатает `python -m mkl.cli status`. `configs/features.yaml`
   создаётся вместе с фичестором и в git не хранится. `ml/README.md` оценивает полную
   сборку примерно в полтора часа.
3. `python scripts/build_intrusion_eventtime_labels.py` — v2-кэш охранной очереди: файлы
   `data/features/intrusion_eventtime_days_v2.parquet`,
   `data/features/label_intrusion_eventtime_v2.parquet` и
   `reports/intrusion_eventtime_v2_build.json`. Замер 28.09: 7 с, счётчики совпали с
   `ml/reports/intrusion_eventtime_v2_build.json` в репозитории.
4. Необязательно: `python scripts/normalize_maintenance_schedules.py --ppr <ППР.xlsx> --to
   <ТО.xlsx> --available-from YYYY-MM-DD` — графики ППР и ТО для контекста алертов A_link
   (по умолчанию пишет `data/interim/maintenance_2026.json`).
5. Датированные модели для окна демо: `python scripts/train_latest.py A_link B E
   --train-window` (раздел «Обучение датированных моделей»; в `README.md` — внутри шага
   2). Без него бандл несёт только `models/{голова}.pkl` с окном порога по
   29.06; для дней 01.06–29.06 проверка задержки эту модель отклоняет, и голова отвечает
   `stale` (`ml/README.md`).
6. `python scripts/build_bundle.py --out ../dist --version bundle-YYYYMMDD-N --archive`.
7. Из корня репозитория проверить архив тем же путём, что пройдёт эксперт:
   `sh scripts/fetch_bundle.sh dist/<версия>.7z ./bundle`.

`build_bundle.py` до копирования проверяет наличие всех обязательных файлов и v2-кэш
(`version` 2 и поле `end` в `reports/intrusion_eventtime_v2_build.json`). Если чего-то
нет, скрипт перечисляет все недостающие файлы, ничего не копирует и завершается с кодом 2.
При `--archive` он заранее ищет 7z и проверяет, что `<out>/<версия>.7z` ещё не существует:
7z дописал бы в старый архив, а не заменил его. Затем копирует файлы в `<out>/<версия>/`,
пишет `MANIFEST.sha256` в формате `sha256sum` и упаковывает каталог в `<out>/<версия>.7z`
с шифрованием имён файлов (`-mhe=on`) и уровнем сжатия 1 (ключ `--level`; parquet уже
сжат zstd). Пароль скрипт берёт из `BUNDLE_PASSWORD`; без неё передаёт 7z пустой ключ
`-p`, и 7z спрашивает пароль сам (`archive_command`). `--dry-run` только
проверяет состав и печатает размеры. Замер 28.09 — в разделе «Состав бандла».

Время всей цепочки из исходного 7z-архива датасета не замерено (задача ML2-12).

## Обучение датированных моделей

Сервис принимает пилотную модель для дня `asof`, только если окно выбора порога кончилось
за 1…`max_model_lag_days` суток до него (`serve.validate_pilot_artifact`): у A_link, B и E
это 8 суток, у D — 14 (`ml/configs/heads.yaml`). Модель по всем данным с порогом по 29.06
проходит проверку только для 30.06; остальным дням июня нужны модели по данным,
обрезанным раньше (`ml/README.md`).

`--source-end C` показывает обучению данные на конец суток C: панель режется по суткам,
эпизоды — по концу, групповые отказы и флаг `is_group` пересобираются по оставшимся
эпизодам. Артефакт пишется в `models/{голова}@{конец окна порога}.pkl`; у A_link, B и E
горизонт — сутки, и конец окна порога — C − 1; `models/{голова}.pkl` не меняется. `serve.artifact_path` выбирает для
`asof` датированный файл с самым поздним концом окна порога раньше `asof`, если задержка
не больше `max_model_lag_days`; иначе берётся `models/{голова}.pkl`, и для неподходящего
дня голова отвечает `stale`.

Команды из `ml/`, по `ml/README.md`; `MKL_ROOT` нужен, если данные лежат вне `ml/`:

```bash
MKL_ROOT=<корень> .venv/Scripts/python scripts/train_latest.py A_link --window-plan
MKL_ROOT=<корень> .venv/Scripts/python scripts/train_latest.py A_link --train-window
MKL_ROOT=<корень> .venv/Scripts/python scripts/train_latest.py B E --train-window
```

`--window-plan` печатает наименьший набор отсечек для окна 01.06–30.06 и выходит;
`--train-window` обучает все отсечки плана; `--source-end YYYY-MM-DD` обучает одну, вместе
с `--train-window` не задаётся. Границы окна меняют `--window-start` и `--window-end`.
`--out КАТАЛОГ` кладёт артефакты с теми же именами в другой каталог, `--out ФАЙЛ.pkl` — в
один файл. Отчёт прогона — `reports/model_refresh@{C}.json` в корне данных ML (`MKL_ROOT`,
по умолчанию `ml/`).

За 01.06 в `daily_channel` нет строк, и отсечку на такой день обучить нельзя, поэтому
`window_plan` в `ml/src/mkl/cv.py` сдвигает первую отсечку на 31.05. План на данных
заказчика:

| `--source-end` | Конец окна порога, имя артефакта | Годится для `asof` |
|---|---|---|
| 2026-05-31 | 2026-05-30, `A_link@2026-05-30.pkl` | 31.05–07.06 |
| 2026-06-08 | 2026-06-07, `A_link@2026-06-07.pkl` | 08.06–15.06 |
| 2026-06-16 | 2026-06-15, `A_link@2026-06-15.pkl` | 16.06–23.06 |
| 2026-06-24 | 2026-06-23, `A_link@2026-06-23.pkl` | 24.06–01.07 |

Эти четыре артефакта и дал прогон 28.09. У B и E те же концы окна порога и те же имена:
`B@2026-05-30.pkl` … `B@2026-06-23.pkl`, `E@2026-05-30.pkl` … `E@2026-06-23.pkl`
(`MANIFEST.sha256` бандла `bundle-20260928-5`). Те же отсечки приведены в таблице окна
демо в `ml/README.md`.

Замер 28.09: одна датированная модель A_link обучается 185–193 с, `--train-window` делает
четыре прогона. Четыре датированные модели E на машине разработчика обучались 10 с, B —
40 с (`ml/reports/FIRE_FLOOD_PRODUCT.md`, «Пересчёт»). На стенде обучение не запускается: каждый прогон — полное обучение на всей
истории, DuckDB берёт до 10 ГБ памяти. На стенд кладутся готовые артефакты в бандле.

Голова D — правило `n_bad_w7`, модель не обучается: `train_latest.py` печатает об этом
строку и пропускает голову. Порог правила пересчитывается по понедельникам, задержка 8–14
суток при лимите 14 (`ml/README.md`).

## CI: `.github/workflows/ci.yml`

Запускается на push в `main`, `chore/**`, `feat/**` и на каждый pull request. Права —
`contents: read`; новый прогон той же ветки отменяет предыдущий (`concurrency`,
`cancel-in-progress`).

| Job | Окружение и лимит | Шаги | Падает, если |
|---|---|---|---|
| `backend` | ubuntu-latest, Python 3.12, сервис `postgres:16`; 15 мин | `pip install -e '.[dev]'`; `ruff check backend tests scripts`; `pytest -q` на SQLite; `pytest -q` на PostgreSQL 16 через `TEST_DATABASE_URL` | замечание линтера или упавший тест |
| `ml` | Python 3.12; 20 мин | `pip install -r ml/requirements.lock`, `pip install --no-deps -e ml`, `pip install pytest httpx`; в `ml/` — `python -m pytest -q` без трёх тестов на данных заказчика | упавший тест |
| `frontend` | Node 22; 10 мин | `npm ci`; `npm run gen:api` и `git diff --exit-code src/api/schema.d.ts`; `npm run typecheck`; `npm run build` | типы из `contracts/api_v1.openapi.json` не закоммичены, ошибка `tsc` или сборки |
| `contracts` | Python 3.12; 10 мин | `pip install -e . pyyaml`; `python scripts/export_contracts.py --strict`; `git diff --exit-code contracts/` и поиск незакоммиченных новых файлов в `contracts/` | `contracts/` разошлись с кодом или фикстура заглушки не выгружена (`--strict`) |
| `smoke` | после `backend`, `ml`, `frontend`; 30 мин | `.env` из `.env.example` с одноразовыми значениями `openssl rand`; `docker compose up -d --build --wait --wait-timeout 600`; `python3 scripts/smoke_compose.py`; при сбое — `docker compose logs --no-color --tail 200`; всегда — `docker compose down -v` | смоук не прошёл |

CI поднимает только `compose.yaml` с ML-заглушкой. `compose.real.yaml` и стенд в CI не
проверяются: бандла с данными заказчика там нет.
