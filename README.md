# Москоллектор — сервис диспетчера

Хакатон ЛЦТ-2026, задача №8: прогноз отказов и рисков в инженерных коллекторах и рабочее
место диспетчера. Что делает команда до сдачи 29.09 и почему — в
[плане команды](docs/superpowers/plans/2026-09-25-team-plan-to-submission.md).

**Статус: рабочий продуктовый контур.** Backend хранит прогнозы, события, решения,
исходы, заявки, уведомления и аудит; поддерживает реальный импорт справочников и
идемпотентную загрузку СМВУ. Без бандла сервис запускается с ML в режиме `stub` и
синтетическим справочником. Ограничения ML-сценариев описаны в
`ml/reports/ML_PRODUCTION_STATUS.md`, числа качества — в реестре
`ml/reports/SUBMISSION_METRICS.md`.

**Сопроводительная документация** для сдачи — [docs/submission/](docs/submission/):
обзор, архитектура, обработка данных, ML, сборка и установка, безопасность, библиотеки,
сложные места кода и трассировка требований ТЗ. Что сделано по задачам плана и что нет —
[docs/MVP_TASKS.md](docs/MVP_TASKS.md), раздел «Состояние на 28.09».

## Быстрый запуск

Нужен Docker с Compose v2. Данные заказчика не нужны.

```bash
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
# вписать в .env DEMO_PASSWORD, SECRET_KEY, INTEGRATION_API_KEY
docker compose up --build
```

`SECRET_KEY` и `INTEGRATION_API_KEY` — любые длинные случайные строки, например
`python -c "import secrets; print(secrets.token_hex(32))"`.

Откройте <http://localhost:8000>. Пользователи — `dispatcher`, `technician`, `analyst`,
`manager`, `admin`, пароль у всех — `DEMO_PASSWORD` из `.env`. Swagger —
<http://localhost:8000/docs>. В Swagger сначала выполните `POST /api/v1/auth/login`:
ответ установит HttpOnly cookie, которую браузер будет отправлять автоматически. Токен
в JSON и ручная подстановка JWT не требуются. Машинные клиенты используют `X-API-Key`.
Сквозной сценарий через Swagger и жизненный цикл заявки — в [docs/openapi.md](docs/openapi.md).

После первого старта журнал прогнозов пуст: seed создаёт пользователей, справочник причин
решений и синтетический справочник объектов, но не прогнозы. С `compose.real.yaml` seed
вместо синтетики сверяет таблицы с настоящими справочниками из `<бандл>/Materials`
(`backend/app/seed.py`). Прогнозы даёт дневной прогон:

- `python scripts/preload_demo.py` — очищает журнал выданного окна, проходит недельную
  очередь за понедельники с 2026-01-05, дневной расчёт за 2026-06-01…2026-06-29 и засевает
  эмулированные решения по факту (`source=emulated`); заявки окна идут за решениями:
  подтверждена → в работе → выполнена, отменена или остаётся черновиком; повторный запуск
  даёт то же состояние;
- или один день — `POST /api/v1/admin/run-daily` с телом `{"asof": "2026-06-29"}` под
  `admin` (через Swagger).

Проверка контура целиком — `python scripts/smoke_compose.py`: вход, статус, прогон,
журнал, карточка, решение, фронт, поток; при сбое код выхода 1. `smoke_compose.py` и
`preload_demo.py` написаны на stdlib и запускаются без venv, нужен только Python 3.12.

Сервисы compose:

| Сервис | Что это | Порт |
|---|---|---|
| `db` | PostgreSQL 16, том `pgdata` | наружу не открыт |
| `ml` | ML-сервис C1, по умолчанию `ML_MODE=stub` | 8001, только внутри сети compose |
| `api` | API C2 и собранный фронт | 8000 |

`docker compose down` останавливает сервисы, `docker compose down -v` ещё и стирает БД.

## Запуск с реальными моделями

Нужен бандл C4 (раздел 4 плана): 13 обязательных файлов, которые читают ML-сервис в режиме
`real`, дообучение, `scripts/replay.py` и seed backend. Это модель A_link, фичестор
`sensor` и `object`, v2-кэш охранной очереди, справочник каналов, метки для `/outcomes`,
события 2026 года, `configs/features.yaml`, `reports/intrusion_eventtime_v2_build.json` и
справочники объектов и каналов в `Materials/`. Список с местом чтения каждого файла —
словарь `REQUIRED` в `ml/scripts/build_bundle.py`; датированные модели A_link
`models/A_link@*.pkl` приходят как необязательные. Текущая версия — `bundle-20260928-3`
с моделями A_link на рабочей точке 0,70 (PR #30): с `-1` не пройдёт проверка раскладки
(нет `Materials/`), модели 0,50 из `-2` ML-сервис отвергает. Бандл
распространяется архивом `bundle-YYYYMMDD-N.7z` под паролем датасета организаторов
(решение D10), ссылка — в поле «Доп. материалы» формы сдачи.

**1. Получить и проверить бандл.**

```bash
sh scripts/fetch_bundle.sh ~/Downloads/bundle-20260928-3.7z ./bundle
```

```powershell
powershell -File scripts\fetch_bundle.ps1 -Version $HOME\Downloads\bundle-20260928-3.7z -Dest .\bundle
```

Вместо пути к `.7z` можно передать URL, распакованный каталог (скрипт его только проверит)
или версию `bundle-YYYYMMDD-N`: тогда скрипт ищет `./<версия>.7z`, а если его нет, скачивает
`$BUNDLE_URL/<версия>.7z`. Нужен 7z (`7z`, `7za` или `7zz`); пароль он спросит сам, без
терминала возьмёт из `BUNDLE_PASSWORD`. Скрипт распаковывает в `<каталог>.partial`,
сверяет `MANIFEST.sha256` (в sh — `sha256sum -c`) и раскладку томов
`compose.real.yaml`, переносит результат в `<каталог>` и печатает строку `BUNDLE_DIR=…`.
Впишите её в `.env`; без неё compose берёт `./bundle`.

**2. Запустить.**

```bash
docker compose -f compose.yaml -f compose.real.yaml up -d --build
```

Real-режим требует совместимый бандл моделей и признаков; без него используйте
`ML_MODE=stub`. Backend всегда сохраняет явные состояния `no_data`, `stale`,
`empty_valid` и `error`, не подменяя их прошлой успешной выдачей.

### Стенд

Стенд (PM-10) добавляет третий файл, `compose.stand.yaml`. Для `!reset` в нём нужен
Compose v2.24 или новее.

- Caddy с TLS на портах 80 и 443 для домена из `STAND_DOMAIN`: сертификат выпускается
  сам, DNS домена должен указывать на ВМ. Заголовки безопасности и журнал доступа в JSON —
  `deploy/Caddyfile`. Для проверки на своей машине подходит `STAND_DOMAIN=localhost`.
- Порт 8000 наружу закрыт, `DEMO_SETTINGS_LOCKED=1`, `COOKIE_SECURE=1`. После 10 неудачных
  входов с одного адреса на один логин api 5 минут отвечает 429.
- Сервис `replay` — поток событий 30.06 (ниже, «События на стенде»).
- Лимиты памяти: ml 6 ГБ, db 3 ГБ, api 2 ГБ, replay 1 ГБ, backup 512 МБ, caddy 256 МБ.
  Журналы контейнеров — по 10 МБ, 5 файлов на сервис.
- Сервис `backup` раз в сутки пишет `pg_dump -Fc` в `./state/backups/` и хранит 7
  последних файлов.

```bash
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml up -d --build
python scripts/smoke_compose.py --base-url https://$STAND_DOMAIN
```

Восстановление БД из бэкапа (api на время останавливаем, чтобы не писал в пустую схему):

```bash
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml stop api replay
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml exec -T db pg_restore --clean --if-exists -U moscollector -d moscollector < state/backups/<файл>.dump
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml start api replay
```

Замер 28.09 на машине разработчика (Docker Desktop, база после загрузки истории 02.05–29.06
и прелоада на моделях A_link 0,50, до PR #30): `pg_dump -Fc` — 74 с, файл 521 МБ;
`pg_restore` в чистый postgres:16 — 133 с без нагрузки и 309 с, пока параллельно шёл подсчёт
строк в исходной базе. После восстановления в базе `events` 10 277 666, `forecasts` 55 942,
`decisions` 338, `work_orders` 222, `outcomes` 475, `audit_log` 2 145: столько событий дала
загрузка истории, столько заявок — тот прелоад
([логи](docs/submission/perf/backup_restore_0928.txt)). Норматив ТЗ на восстановление — 4 часа.

### Сборка бандла из датасета

Нужны датасет организаторов и ML-окружение (`make install-ml` или строка «Установка ML» в
разделе «Режим разработки»). Команды шагов 2–5 выполняются из `ml/`; `python` в них —
интерпретатор `.venv/bin/python` (Windows: `.venv\Scripts\python`).

1. Журналы `ext-journal-YYYY.csv` положить в `ml/data/raw/`, справочники
   `справочник_каналов_датчиков.csv` и `справочник_объектов_диспетчер.csv` — в
   `ml/Materials/`.
2. `python -m mkl.cli run` — приём, эпизоды, суточная панель, погода, фичестор, обучение
   A_link. Стадии перечислены в `ml/src/mkl/pipeline.py`; погода качается из сети,
   без сети её колонки остаются пустыми.
3. `python scripts/build_intrusion_eventtime_labels.py` — v2-кэш охранной очереди.
4. Необязательно: `python scripts/normalize_maintenance_schedules.py --ppr <ППР.xlsx>
   --to <ТО.xlsx> --available-from YYYY-MM-DD` — графики ППР и ТО для контекста алертов
   A_link. Без них алерты идут со статусом контекста `schedule_not_loaded`.
5. `python scripts/build_bundle.py --out ../dist --version bundle-YYYYMMDD-N --archive`
   копирует нужные файлы в `dist/<версия>/`, пишет `MANIFEST.sha256` и упаковывает всё
   в `dist/<версия>.7z` с шифрованием имён файлов. Пароль 7z спросит сам или возьмёт из
   `BUNDLE_PASSWORD`. Если обязательного файла нет, скрипт перечисляет все недостающие и
   ничего не копирует. `--dry-run` только проверяет состав и печатает размеры.
6. Из корня репозитория проверить архив тем же путём, что пройдёт эксперт:
   `sh scripts/fetch_bundle.sh dist/<версия>.7z ./bundle`.

Время полной сборки шагов 1–5 из исходного 7z-архива датасета: **не замерено** (ML2-12,
прогон на отдельной Linux-ВМ).

События на стенде. Поток 30.06 даёт сервис `replay` (`scripts/replay.py --catch-up
--align-to-clock --loop`): на старте он отправляет события 00:00…сейчас без уведомлений,
дальше идёт в том же времени суток МСК, в полночь удаляет день и начинает заново. Журнал
событий мая–июня и динамику карточки прогноза (30 суток до `asof`) заполняет разовая
загрузка истории за 2026-05-02…2026-06-29, тоже без уведомлений:

```bash
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml \
  run --rm replay python /srv/scripts/replay.py --base-url http://api:8000 --bulk
```

Без Docker — `python scripts/replay.py --bulk` в окружении с duckdb; если бандл не в
`./bundle`, путь к `events_year=2026.parquet` задаёт `--source`. В окне 10 277 666 строк за
58 дней: за 2026-06-01 событий в бандле нет. Прерванную загрузку можно продолжить с
`--from <день>` или запустить заново: принятые строки api посчитает дублями.

Замер 28.09 на машине разработчика (api на SQLite, Windows, 6 дней, 1,03 млн строк):
5 422 строки/с в первый день, 3 258 — в шестой; скорость падает с ростом таблицы.
Повтор уже загруженного дня — 12 582 строки/с. На PostgreSQL в compose (Docker Desktop)
всё окно — 10 277 666 строк — загрузилось за 2 684 с, 3 829 строк/с, ошибок 0
(`docs/submission/06-build-install.md`, «История событий»). На Linux-ВМ стенда скорость
не замерялась.

## Эмуляция внешних систем и нагрузка

Журнала ОДС и учётной системы заявок в демо нет, их роль играют два скрипта на stdlib.
Пишут они с ключом `INTEGRATION_API_KEY`, читают под демо-пользователем с паролем
`DEMO_PASSWORD`: у роли integration нет права view. Их записи помечены «эмуляция ОДС»
и «эмуляция help desk».

```bash
python scripts/emulate_ods.py --day 2026-06-30 --count 20   # журнал ОДС за день; повтор не дублирует
python scripts/emulate_helpdesk.py --once                    # подтверждённые заявки — на шаг вперёд
python scripts/emulate_helpdesk.py --speed 60 --interval 5   # с задержками: час за минуту
```

Help desk переводит заявки только confirmed → in_progress → completed: подтвердить
черновик и отменить заявку может лишь диспетчер или руководитель.

Нагрузочный тест (ТЗ §11: 20 пользователей) — `scripts/load_test/locustfile.py`. locust
ставится отдельно, в зависимости проекта он не входит:

```bash
pip install locust
mkdir -p data/load_test
DEMO_PASSWORD=... locust -f scripts/load_test/locustfile.py --host http://127.0.0.1:8000 \
    --users 20 --spawn-rate 2 --run-time 10m --headless --csv data/load_test/run
```

p50 и p95 каждого запроса — колонки «50%» и «95%» в `data/load_test/run_stats.csv`,
число ошибок — «Failure Count» там же, тексты ошибок — в `run_failures.csv`. Прогон
только читает; `LOAD_MUTATIONS=1` добавляет решения диспетчера. Прогон 28.09 на машине
разработчика: медиана ответа 64 мс, p95 400 мс, одна ошибка из 4 339 запросов
([docs/submission/08-performance.md](docs/submission/08-performance.md), «Нагрузка»).

## Режим разработки

Python 3.12, Node 22. Для запуска без Docker добавьте в `.env` две строки — compose их
переопределяет, так что они не мешают `docker compose up`:

```
DATABASE_URL=sqlite:///./data/dev.db
ML_URL=http://localhost:8001
```

Команды выполняются из корня репозитория: backend читает `.env` из текущего каталога.

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
| Compose | `make up`, `make smoke`, `make down` | `docker compose up -d --build`, `python scripts\smoke_compose.py`, `docker compose down` |

Три теста ML исключены: им нужны данные заказчика, которых нет ни в git, ни в CI.

## Раскладка репозитория

```
backend/app/         API C2: config, db, models, security, audit, main
  routers/           маршруты по разделам C2, только права и параметры
  schemas/           pydantic-модели C2 и зеркало C1 (ml.py)
  services/          прикладная логика; сигнатуры — services/signatures.md
  migrations/        Alembic, одна миграция 0001
  seed.py            пользователи, причины решений, сверка с настоящим справочником или синтетика
backend/entrypoint.sh  старт контейнера: миграция, seed, uvicorn
tests/               тесты backend
frontend/            React + Vite + TypeScript; src/pages — вход и 9 экранов
ml/                  ML-проект CAML целиком; продуктовый слой — src/mkl/product_*.py
contracts/           словари C3, схемы C1 и C2, фикстуры, синтетический справочник
scripts/             смоук, выгрузка контрактов, прелоад, проигрыватель, эмуляторы, нагрузка
deploy/Caddyfile     обратный прокси стенда
compose*.yaml        локальный запуск, реальные модели, стенд
docs/                план команды, спецификация каркаса, архитектура, владельцы, статус задач
  submission/        сопроводительная документация для сдачи
analysis/            аудит ТЗ и полного датасета
```

Схема сервисов и дневной цикл — в [docs/architecture.md](docs/architecture.md).

## Контракты

`contracts/` пересобирается из кода:

```bash
python scripts/export_contracts.py     # схемы C1 и C2 и фикстуры в contracts/fixtures/
cd frontend && npm run gen:api         # src/api/schema.d.ts из api_v1.openapi.json
```

Поменяли схему, словарь или контрактный ответ — перегенерируйте и закоммитьте результат в
том же PR. CI (job `contracts` и шаг `gen:api` в job `frontend`) падает, если после
пересборки `git diff` не пуст. Правила изменения — в [CONTRIBUTING.md](CONTRIBUTING.md),
раздел «Каркас и контракты».

## Кто за что отвечает

Что каждый делает до сдачи, к какому сроку и как понять, что готово: [docs/MVP_TASKS.md](docs/MVP_TASKS.md).

Путь → роль → ID задачи плана → контракт: [docs/OWNERSHIP.md](docs/OWNERSHIP.md). Правка
в чужом файле — через PR с ревью владельца.
