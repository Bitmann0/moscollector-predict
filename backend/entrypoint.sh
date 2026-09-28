#!/bin/sh
# Старт контейнера api. Живое.
# Порядок из спецификации каркаса: миграция -> seed -> uvicorn.
# Seed создаёт пользователей, reason_codes и синтетический справочник; прогнозов
# не создаёт: их даёт POST /api/v1/admin/run-daily или scripts/preload_demo.py.
set -e
cd /app/backend

alembic upgrade head

# Seed запускается всегда: reason_codes и настройки нужны и при SEED_DEMO=0,
# а демо-пользователей и синтетический справочник seed создаёт только при SEED_DEMO=1.
python -m app.seed

# X-Forwarded-* принимаются только от адресов из FORWARDED_ALLOW_IPS.
# Локально это 127.0.0.1; compose.stand.yaml открывает их для Caddy.
# keep-alive 75 с — дольше, чем Caddy держит простаивающее соединение к api (60 с в
# deploy/Caddyfile). По умолчанию uvicorn закрывал его через 5 с, и запрос, ушедший в
# уже закрытое соединение, падал: 1 из 4 339 в нагрузочном прогоне 28.09.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 75 \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
