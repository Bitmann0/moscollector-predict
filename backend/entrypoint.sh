#!/bin/sh
# Старт контейнера api. Живое.
# Порядок из спецификации каркаса: миграция -> seed (при SEED_DEMO=1) -> uvicorn.
# Seed создаёт пользователей, reason_codes и синтетический справочник; прогнозов
# не создаёт: их даёт POST /api/v1/admin/run-daily или scripts/preload_demo.py.
set -e
cd /app/backend

alembic upgrade head

case "${SEED_DEMO:-1}" in
  1|true|True|yes) python -m app.seed ;;
esac

# X-Forwarded-* принимаются только от адресов из FORWARDED_ALLOW_IPS.
# Локально это 127.0.0.1; compose.stand.yaml открывает их для Caddy.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
