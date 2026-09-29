# Москоллектор — сервис диспетчера

Хакатон ЛЦТ-2026, задача №8: прогноз отказов и рисков в инженерных коллекторах и рабочее
место диспетчера. Что делает команда до сдачи 29.09 и почему — в
[плане команды](docs/superpowers/plans/2026-09-25-team-plan-to-submission.md).

Сервис ведёт пять сценариев прогноза: `sensor_link` (голова A_link, отказ датчика),
`equipment_diag` (правило D, износ), `guard_weekly` (недельная охранная очередь),
`fire_risk` (голова B, пожарный риск) и `flood_risk` (голова E, подтопление) — словарь
`scenario` в `contracts/vocabularies.json`. Backend хранит прогнозы, события, решения,
исходы, заявки, уведомления и аудит, импортирует настоящие справочники и не дублирует
повторно загруженные события СМВУ. Числа качества и ограничения сценариев — в реестре
`ml/reports/SUBMISSION_METRICS.md`, раздел «Что говорим прямо».

Сервис запускается в одном из двух режимов:

- без бандла — ML-сервис в режиме `stub` отвечает по синтетическому справочнику
  (раздел «Быстрый запуск»);
- с бандлом `bundle-20260928-5` — ML-сервис в режиме `real` считает на моделях, обученных
  на данных заказчика (раздел «Запуск с реальными моделями»).

**Сопроводительная документация** для сдачи — [docs/submission/](docs/submission/):
обзор, архитектура, обработка данных, ML, сборка и установка, безопасность, библиотеки,
сложные места кода и трассировка требований ТЗ. Что сделано по задачам плана и что нет —
[docs/MVP_TASKS.md](docs/MVP_TASKS.md), раздел «Состояние на 28.09».

## Быстрый запуск

Режим `stub`. Нужен Docker с Compose v2; бандл и данные заказчика не нужны. Ответы ML
строит `ml/src/mkl/product_stub.py` по синтетическому справочнику
`contracts/synthetic_reference.json`.

```bash
cp .env.example .env        # PowerShell: Copy-Item .env.example .env
# вписать в .env DEMO_PASSWORD, SECRET_KEY, INTEGRATION_API_KEY
docker compose up --build
```

`SECRET_KEY` и `INTEGRATION_API_KEY` — любые длинные случайные строки, например
`python -c "import secrets; print(secrets.token_hex(32))"`.

Откройте <http://localhost:8000>. Пользователи — `dispatcher`, `technician`, `analyst`,
`manager`, `admin`, пароль у всех — `DEMO_PASSWORD` из `.env`. Swagger —
<http://localhost:8000/docs>. В Swagger сначала выполните `POST /api/v1/auth/login`: ответ
ставит HttpOnly cookie, дальше браузер отправляет её сам, и подставлять JWT вручную не
нужно. Машинные клиенты передают `INTEGRATION_API_KEY` в заголовке `X-API-Key`. Сквозной
сценарий через Swagger и жизненный цикл заявки — в [docs/openapi.md](docs/openapi.md).

После первого старта журнал прогнозов пуст: seed создаёт пользователей, справочник причин
решений и синтетический справочник объектов, но не прогнозы. Прогнозы даёт дневной прогон:

- `python scripts/preload_demo.py` очищает журнал выданного за окно, считает недельную
  очередь за понедельники с 2026-01-05 и дневной расчёт за 2026-06-01…2026-06-29, затем
  засевает эмулированные решения по факту (`source=emulated`). Заявки окна идут за
  решениями: подтверждена → в работе → выполнена, отменена или остаётся черновиком.
  Повторный запуск приводит журнал к тому же состоянию;
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

Режим `real`. Нужны бандл C4 версии `bundle-20260928-5` (раздел 4 плана) и 7z (`7z`, `7za`
или `7zz`). В бандле 16 обязательных файлов, которые читают ML-сервис в режиме `real`,
дообучение, `scripts/replay.py` и seed backend: модели A_link, B и E; фичестор `sensor`,
`object` и `segment`; v2-кэш охранной очереди; справочник каналов; суточная панель и
эпизоды — факт для `/outcomes` и порог правила D; групповые отказы для дообучения;
события 2026 года; `configs/features.yaml`; `reports/intrusion_eventtime_v2_build.json`;
справочники объектов и каналов в `Materials/`. Кто читает каждый файл — словарь `REQUIRED`
в `ml/scripts/build_bundle.py`; датированные модели `models/{голова}@*.pkl` приходят как
необязательные.

В `bundle-20260928-5` 29 файлов, среди них модели A_link на рабочей точке 0,70 (PR #30),
модели пожарного риска B и подтопления E с датированными версиями и признаки участков
`segment.parquet`. В `-4` и более ранних версиях моделей B и E нет, и ML-сервис на
`/ready` ответит `missing_data` (так следует из `_real_ready` в
`ml/src/mkl/product_api.py`, запуском не проверялось). В `-1` нет и `Materials/`, поэтому
он не пройдёт проверку раскладки.

**1. Получить и проверить бандл.** Архив `bundle-20260928-5.7z` (2,0 ГБ, без пароля) лежит на
[Google Диске](https://drive.google.com/file/d/17Dh6tC8sPO0bWPf3ukHCuErg15_bSIg-/view?usp=sharing).
Скачайте его в браузере: для файлов больше 100 МБ Диск вместо файла отдаёт страницу с
предупреждением, поэтому `fetch_bundle` по этой ссылке архив не получит. SHA-256 архива:
`b50e2d11f4db53d7c3d2a5e2f423dac63ea576554e1263c7c8325d95d7885705`. Затем распакуйте и
проверьте его:

```bash
sh scripts/fetch_bundle.sh ~/Downloads/bundle-20260928-5.7z ./bundle
```

```powershell
powershell -File scripts\fetch_bundle.ps1 -Version $HOME\Downloads\bundle-20260928-5.7z -Dest .\bundle
```

Вместо пути к `.7z` можно передать URL, распакованный каталог (скрипт его только проверит)
или версию `bundle-YYYYMMDD-N`: тогда скрипт ищет `./<версия>.7z`, а если его нет, скачивает
`$BUNDLE_URL/<версия>.7z`. Если архив под паролем, 7z спросит его сам, а без терминала
возьмёт из `BUNDLE_PASSWORD`. Скрипт распаковывает архив в `<каталог>.partial`, сверяет
`MANIFEST.sha256` (в sh — `sha256sum -c`) и раскладку томов `compose.real.yaml`,
переносит результат в `<каталог>` и печатает строку `BUNDLE_DIR=…`. Впишите её в `.env`;
без неё compose берёт `./bundle`.

**2. Запустить.**

```bash
docker compose -f compose.yaml -f compose.real.yaml up -d --build
```

То же — `make up-real`. `compose.real.yaml` включает `ML_MODE=real` и монтирует бандл в
контейнеры `ml` и `api`. При старте api seed сверяет таблицы объектов и каналов с
настоящими справочниками из `<бандл>/Materials` и убирает синтетику (`backend/app/seed.py`).
Модели и фичестор грузятся дольше заглушки, поэтому `start_period` healthcheck ml — 180 с.
Backend сохраняет состояния `no_data`, `stale`, `empty_valid` и `error` как есть и не
подменяет их прошлой успешной выдачей.

**3. Наполнить и проверить.** Порядок тот же, что на стенде: история событий, журнал
прогнозов, смоук.

```bash
python scripts/replay.py --bulk     # история 2026-05-02…2026-06-29, нужен интерпретатор с duckdb
python scripts/preload_demo.py
python scripts/smoke_compose.py
```

История нужна журналу событий и динамике карточки прогноза; подробности и замеры — абзац
«События на стенде» в разделе «Стенд».

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

Запуск, загрузка истории, журнал прогнозов и смоук:

```bash
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml up -d --build
docker compose -f compose.yaml -f compose.real.yaml -f compose.stand.yaml \
  run --rm replay python /srv/scripts/replay.py --base-url http://api:8000 --bulk
python scripts/preload_demo.py --base-url https://$STAND_DOMAIN
python scripts/smoke_compose.py --base-url https://$STAND_DOMAIN
```

`$STAND_DOMAIN` в двух последних командах — переменная оболочки: compose берёт домен из
`.env`, а скрипты его оттуда не читают. Без `--read-only` смоук сам запускает run-daily за
29.06 и сохраняет решение диспетчера; на стенде, уже подготовленном для экспертов, его
запускают с `--read-only`.

События на стенде. Поток 30.06 даёт сервис `replay` (`scripts/replay.py --catch-up
--align-to-clock --loop`): на старте он отправляет события 00:00…сейчас без уведомлений,
дальше идёт в том же времени суток МСК, в полночь удаляет день и начинает заново. Журнал
событий мая–июня и динамику карточки прогноза (30 суток до `asof`) заполняет разовая
загрузка истории за 2026-05-02…2026-06-29 — вторая команда в блоке выше, тоже без
уведомлений.

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
`decisions` 338, `work_orders` 222, `outcomes` 475, `audit_log` 2 145
([логи](docs/submission/perf/backup_restore_0928.txt)). Норматив ТЗ на восстановление — 4 часа.

### Сборка бандла из датасета

Нужны датасет организаторов и ML-окружение (`make install-ml` или строка «Установка ML» в
разделе «Режим разработки»). Команды шагов 2–5 выполняются из `ml/`; `python` в них —
интерпретатор `.venv/bin/python` (Windows: `.venv\Scripts\python`).

1. Журналы `ext-journal-YYYY.csv` положить в `ml/data/raw/`, справочники
   `справочник_каналов_датчиков.csv` и `справочник_объектов_диспетчер.csv` — в
   `ml/Materials/`.
2. `python -m mkl.cli run` — приём, эпизоды, суточная панель, погода, фичестор, обучение
   A_link, B и E. Стадии перечислены в `ml/src/mkl/pipeline.py`; погода качается из сети,
   без сети её колонки остаются пустыми. Датированные модели для окна демо —
   `python scripts/train_latest.py A_link B E --train-window` (`ml/README.md`, «Модели для
   исторических дней: окно демо»).
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

## Эмуляция внешних систем и нагрузка

Журнала ОДС и учётной системы заявок в демо нет, их роль играют два скрипта на stdlib.
Пишут они с ключом `INTEGRATION_API_KEY`, а читают под демо-пользователем с паролем
`DEMO_PASSWORD`, потому что у роли integration нет права view. Их записи помечены
«эмуляция ОДС» и «эмуляция help desk».

```bash
python scripts/emulate_ods.py --day 2026-06-30 --count 20   # журнал ОДС за день; повтор не дублирует
python scripts/emulate_helpdesk.py --once                    # подтверждённые заявки — на шаг вперёд
python scripts/emulate_helpdesk.py --speed 60 --interval 5   # с задержками: час за минуту
```

Help desk переводит заявки только confirmed → in_progress → completed. Подтвердить
черновик и отменить заявку могут только роли с правом `work_order_manage`: диспетчер,
руководитель и admin.

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

## XML в API

JSON — формат по умолчанию. Журнал прогнозов, заявки, события, `/system/status` и
`/quality` отдают XML по заголовку `Accept: application/xml`; `POST /api/v1/ingest/events`
и `/ingest/ods-journal` принимают XML с `Content-Type: application/xml`. Правила
отображения — [docs/submission/02-architecture.md](docs/submission/02-architecture.md),
раздел 8 «Форматы JSON и XML»; схемы XSD — `contracts/xml/`.

```bash
# пачка событий из примера, ответ тоже в XML
curl -X POST "http://127.0.0.1:8000/api/v1/ingest/events?notify=false" \
  -H "X-API-Key: $INTEGRATION_API_KEY" -H "Content-Type: application/xml" \
  -H "Accept: application/xml" --data-binary @scripts/examples/ingest_events.xml
# журнал прогнозов в XML: у integration нет права view, читает демо-пользователь
curl -c /tmp/mk.cookies -H "Content-Type: application/json" \
  -d "{\"login\": \"dispatcher\", \"password\": \"$DEMO_PASSWORD\"}" \
  http://127.0.0.1:8000/api/v1/auth/login
curl -b /tmp/mk.cookies -H "Accept: application/xml" "http://127.0.0.1:8000/api/v1/forecasts?page_size=2"
```

## Вход через LDAP/AD

С заданным `LDAP_URL` api сначала проверяет пароль в корпоративном каталоге (simple bind,
библиотека `ldap3`), роль берёт из групп сотрудника, а при первом входе заводит его в
`users`. Демо-учётки продолжают работать при `LDAP_ALLOW_LOCAL=1`, это значение по
умолчанию. Коды отказа: неверный пароль — 401 `bad_credentials`, нет группы с ролью —
403 `no_role_in_directory`, каталог не ответил — 503 `directory_unavailable`.
Переменные — в `.env.example`, устройство и ограничения —
[docs/submission/07-security.md](docs/submission/07-security.md), раздел 5.

Тестовый каталог OpenLDAP (`osixia/openldap:1.5.0`, 373 МБ) поднимает override
`compose.ldap.yaml`. В нём по сотруднику на роль и один без роли: `ldap-dispatcher`,
`ldap-technician`, `ldap-analyst`, `ldap-manager`, `ldap-admin`, `ldap-norole`. Их
пароли лежат только в `deploy/ldap/test-directory.env.example`; это тестовые пароли,
реальных там нет.

```bash
docker compose -f compose.yaml -f compose.ldap.yaml up -d --build
```

api ищет сотрудника под служебной учётной записью `cn=readonly` и подключается к
каталогу через StartTLS. Сертификат каталога он проверяет по тестовому CA, который
выпускает `deploy/ldap/tls-init.sh`. Порты каталога 1389 (LDAP) и 1636 (LDAPS)
открыты только на 127.0.0.1. Правка `deploy/ldap/bootstrap.ldif` вступает в силу после
`docker compose -f compose.yaml -f compose.ldap.yaml down -v`.

Тесты против этого каталога в CI не входят, без `LDAP_TEST_URL` они пропускаются:

```bash
docker compose -f compose.yaml -f compose.ldap.yaml up -d --wait ldap
docker compose -f compose.yaml -f compose.ldap.yaml cp \
    ldap:/container/service/slapd/assets/certs/ca.crt state/ldap-ca.crt
LDAP_TEST_URL=ldap://localhost:1389 LDAP_TEST_TLS_URL=ldaps://localhost:1636 \
    LDAP_TEST_CA_FILE=state/ldap-ca.crt python -m pytest -q tests/test_directory_live.py
```

PowerShell: `$env:LDAP_TEST_URL="ldap://localhost:1389"` и так же две другие переменные,
затем `.venv\Scripts\python -m pytest -q tests/test_directory_live.py`. Без Docker
логику входа проверяют `tests/test_directory.py` на подменном каталоге ldap3
`MOCK_SYNC`; они идут в CI вместе с остальными тестами.

## Режим разработки

Python 3.12, Node 22. Для запуска без Docker добавьте в `.env` две строки. Compose их
переопределяет, так что `docker compose up` они не мешают:

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
backend/app/         API C2: config, db, models, security, directory (LDAP), audit, main
  routers/           маршруты по разделам C2, только права и параметры
  schemas/           pydantic-модели C2 и зеркало C1 (ml.py)
  services/          прикладная логика; сигнатуры — services/signatures.md
  migrations/        Alembic, миграции 0001–0003
  seed.py            пользователи, причины решений, сверка с настоящим справочником или синтетика
backend/entrypoint.sh  старт контейнера: миграция, seed, uvicorn
tests/               тесты backend
frontend/            React + Vite + TypeScript; src/pages — вход и 10 экранов
ml/                  ML-проект CAML целиком; продуктовый слой — src/mkl/product_*.py
contracts/           словари C3, схемы C1 и C2, фикстуры, синтетический справочник
scripts/             смоук, выгрузка контрактов, прелоад, проигрыватель, эмуляторы, нагрузка
deploy/Caddyfile     обратный прокси стенда
deploy/ldap/         тестовый каталог OpenLDAP: LDIF, тестовые пароли, выпуск сертификата
compose*.yaml        локальный запуск, реальные модели, стенд, тестовый каталог LDAP
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
