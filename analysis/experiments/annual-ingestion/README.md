# Годовая история: PR #4 в раскладке монорепозитория

PR #4 (ветка `feature/annual-ingestion`, последний коммит `31a1af8` от 16.09.2026) добавлял в прежний `backend/app` два модуля. `ingest.py` импортировал распакованные годовые CSV в DuckDB, одна транзакция на файл, источник опознавался по SHA-256. `features.py` выгружал суточные признаки по окнам 7 и 30 дней в parquet. Ни импортёр, ни хранилище в монорепозиторий не перенесены. Здесь лежат свидетельства полного прогона, соответствие свойств импортёра модулям `main` и карта колонок для переноса PR #5–#9.

## Результат полного прогона PR #4

Оба JSON скопированы из ветки PR без изменений (`git show origin/feature/annual-ingestion:analysis/<файл>`):

- [ingest_report.json](ingest_report.json) — счётчики по восьми годовым файлам и SHA-256 распакованных CSV. В `analysis/archive_manifest.json` и `zip_identity.json` есть только размеры и CRC32 архивов, хешей самих CSV на `main` больше нигде нет. У 2019 года стоит `"skipped": true`: импортёр вернул отчёт из прошлого прогона, потому что файл с тем же SHA-256 уже был принят.
- [warehouse_verification.json](warehouse_verification.json) — итог сверки хранилища с `analysis/annual_profiles.json`. Отсюда число 6 266 826 752 байта, которое решение D8 плана ([docs/superpowers/plans/2026-09-25-team-plan-to-submission.md](../../../docs/superpowers/plans/2026-09-25-team-plan-to-submission.md), строка 99) округляет до 6,27 ГБ.

| Год | Строк CSV | Заголовков | Точных повторов | Событий | Неоднозначных ID |
|---|---:|---:|---:|---:|---:|
| 2019 | 20 993 328 | 0 | 1 491 | 20 991 837 | 0 |
| 2020 | 28 172 398 | 0 | 0 | 28 172 398 | 0 |
| 2021 | 53 698 948 | 0 | 0 | 53 698 948 | 775 099 |
| 2022 | 43 483 149 | 0 | 0 | 43 483 149 | 667 317 |
| 2023 | 31 937 125 | 0 | 562 449 | 31 374 676 | 226 852 |
| 2024 | 49 023 883 | 0 | 0 | 49 023 883 | 0 |
| 2025 | 55 541 797 | 1 | 0 | 55 541 796 | 0 |
| 2026 | 30 695 389 | 0 | 0 | 30 695 389 | 0 |
| Всего | 313 546 017 | 1 | 563 940 | 312 982 076 | 1 669 268 |

Отклонённых строк ноль. «Неоднозначный ID» — один `ид_события` у нескольких разных строк файла. По годам эти числа совпадают с `conflicting_ids` в `analysis/deep_audit.json`, а их сумма 1 669 268 — с числом событий, которые удаляла прежняя дедупликация по одному ID (комментарий в `ml/src/mkl/ingest.py:107-118`). Суточных агрегатов 4 273 083 по 12 627 каналам.

## Почему хранилище не перенесено

По решению D8 второе хранилище DuckDB на 6,27 ГБ отклонено: оно дублирует parquet приёма строка в строку. `ml/scripts/verify_ingest.py` это подтверждает. На локальной копии `data/interim` (файлы от 20.09.2026) годовые `events_year=2019…2026.parquet` содержат по каждому году ровно столько событий, сколько `accepted_rows` в `ingest_report.json`. В `daily_channel.parquet` 4 273 083 строки по 12 627 каналам, сумма `n_events` — 312 982 076. Это `daily_channel_source_rows`, `observed_channels` и `accepted_rows` из `warehouse_verification.json`. Результат лежит в [ml/reports/ingest_reconciliation.json](../../../ml/reports/ingest_reconciliation.json).

## Чем на main покрыто каждое свойство импортёра

| Свойство PR #4 | На `main` |
|---|---|
| Повторы схлопываются только при совпадении всех шести полей, повторный ID с другим содержимым сохраняется | `SELECT DISTINCT` по кортежу в `ml/src/mkl/ingest.py:119-127`; тесты в `ml/tests/test_ingest.py` |
| Встроенный заголовок исключается | фильтр `ид_события <> 'ид_события'`, `ml/src/mkl/ingest.py:127` |
| Результат не зависит от прогона | `ml/scripts/check_reproducible.py`, `test_ingest_is_deterministic_across_runs` |
| Сверка «строки = события + повторы + заголовки + отклонённые» | `ml/scripts/verify_ingest.py`, код возврата 1 при расхождении |
| Карантин строк с неизвестным «тревожное» | приём не менялся: `ml/src/mkl/ingest.py:124` превращает любое значение кроме t/true в false. Проверку делает `verify_ingest.py`: значение вне {t, f, true, false} в профиле исходника — ошибка `unknown_alarm_values`, число тревог в parquet должно лежать между «t минус точные повторы» и «t» |
| Отказ по неизвестному «тревожное» и SHA-256 строки в онлайн-приёме | `backend/app/services/ingest.py:50` (`_parse_alarm`, исключение `invalid_alarm` на строке 58), `:73` (`row_hash`), счётчики партии в `ingest_batches` |
| Окна 7 и 30 дней без будущего | `add_rolling_windows`, `ml/src/mkl/features/base.py:41` |
| Транзакция на файл, пропуск уже принятого по SHA-256, пояс источника в журнале | аналога нет. Приём перезаписывает `events_year=Y.parquet` целиком, а `mkl run` пересобирает стадию, если вход новее выхода (`ml/src/mkl/pipeline.py:73`) |

Равенство проверяется так:

```bash
cd ml
python scripts/verify_ingest.py --require-all
```

Без `--require-all` отсутствующий год не считается ошибкой и попадает в `not_present`: в бандле C4 из событий есть только `events_year=2026.parquet` (`ml/scripts/build_bundle.py`). В ML-контейнере каталога `analysis/` нет, поэтому пути к аудиту там передаются явно: `--profiles`, `--audit`, `--interim`.

## Карта колонок для переноса PR #5–#9

Ветка PR #5 (`feature/history-browser`) содержит коммиты PR #4. В ветках PR #6–#9 (`feature/lead-time-validation`, `analysis/deep-data-model-audit`, `feature/episode-hourly-backtest`, `feature/availability-risk-backtest`) есть `backend/ml/from_warehouse.py` и `tests/test_warehouse_adapter.py`. Адаптер (у всех четырёх веток файл один и тот же, blob `5612f35`) читает из `events` колонки `event_time`, `local_date`, `channel_id`, `alarm`, `sensor_value` и `source_sha256`, а из `ingest_sources` — `sha256`, `filename` и `time_zone`, чтобы выбрать годовые файлы. На `main` те же данные подключаются как представления: `mkl.db.attach_events` (`ml/src/mkl/db.py:17`) и `attach_parquet` (`:24`).

| DuckDB PR #4 | parquet на `main` | Разница |
|---|---|---|
| `events.event_id` | `events_year=Y.parquet`: `event_id` | — |
| `events.channel_id` | `ch` | — |
| `events.event_time`, TIMESTAMPTZ, момент в UTC | `ts`, TIMESTAMP без зоны | `ts` — местное время из CSV, поэтому `event_time AT TIME ZONE 'Europe/Moscow'` из адаптера становится просто `ts`. Бэкенд читает время без зоны как Europe/Moscow: `backend/app/services/helpers.py:3`, `assume_msk` на строке 27 |
| `events.local_date` | `day` | — |
| `events.alarm` | `alarm` | PR: t/true/1 → true, f/false/0 → false, остальное в карантин. `main`: true только для t и true |
| `events.sensor_value` | `val_raw` | на `main` есть ещё `val_num` = `TRY_CAST(val_raw AS DOUBLE)` |
| `events.date_raw`, `time_raw`, `alarm_raw` | нет | исходные строки на `main` не хранятся |
| `events.source_row`, `occurrences`, `source_sha256` | нет | точные повторы схлопываются без счётчика |
| `daily_channels.channel_id`, `local_date` | `daily_channel.parquet`: `ch`, `day` | — |
| `daily_channels.event_count`, `alarm_count` | `n_events`, `n_alarms` | — |
| `daily_channels.first_event`, `last_event` | нет | `min(ts)`, `max(ts)` по (`ch`, `day`) из событий |
| `ingest_sources`: `sha256`, `filename`, `time_zone`, `report` | нет | год выбирается файлом `events_year=Y.parquet`, счётчики приёма — в `ml/reports/ingest_reconciliation.json` |

Признаки `daily_features.parquet` соотносятся с `ml/src/mkl/features/base.py` так:

| PR #4 | `main` | Разница |
|---|---|---|
| `events_previous_7d`, `events_previous_30d` | `n_events_w7`, `n_events_w30` | окно PR — [D−7, D−1], на `main` — [D−6, D] |
| `alarms_previous_30d` | `n_alarms_w30` | то же окно |
| `observed_days_previous_7d`, `_30d` | `n_active_days_w7`, `_w30` | то же окно; считаются только наблюдавшиеся дни, пропуски нулями не заполняются |
| `days_since_previous` | `prev_gap_days` | одинаково: дни от предыдущего наблюдавшегося дня канала |
| `median_events_previous_30d` | нет | ближе всего `n_events_mean_w30` и `n_events_std_w30` |
| `feature_available_at` | нет | окно включает сутки D, признак считается на их конец, метка смотрит в D+1…D+H (docstring `add_rolling_windows`) |

## Что не перенесено

- `analysis/features_report.json`. Его `catalog_sha256` (`fa550d4c…`) совпадает с `old_channels_sha256` в `analysis/catalog_update_audit.json`, то есть отчёт построен по старому справочнику. Число 264 558 канал-дней без типа к текущему справочнику не относится.
- Код и тесты импортёра и экспорта признаков. Свойства, которые на `main` нужны, перечислены в таблице выше. Идея `scripts/verify_warehouse.py` перенесена в `ml/scripts/verify_ingest.py`.
- `docs/annual-ingestion.md`. Его команды (`python -m app.ingest`, `python -m app.features`) и путь `data/warehouse` на `main` не существуют, документ заменён этим.

## Отложено

В docstring `build_daily_channel` (`ml/src/mkl/panel.py:14`) панель оценена в «~15 млн строк», а их 4 273 083. Исправление отложено до после сдачи: `panel.py` — вход стадии `panel` (`ml/src/mkl/pipeline.py:88`). После его правки `mkl run` сочтёт панель устаревшей и пересоберёт панель, фичестор и модели. Это почти вся полная сборка: по `ml/README.md` она идёт около полутора часов, из них на приём 6 минут.
