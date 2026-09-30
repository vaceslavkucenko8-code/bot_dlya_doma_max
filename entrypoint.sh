#!/usr/bin/env sh
set -e

# Amvera injects AMVERA=1 and mounts persistent storage at /data.  Keep an
# explicitly configured database untouched, but provide a durable standalone
# database when the application is deployed without a separate PostgreSQL
# project.
if [ "${AMVERA:-}" = "1" ] && [ -z "${DATABASE_URL:-}" ]; then
  export DATABASE_URL="sqlite:////data/domradar.sqlite3"
fi

mkdir -p /data/photos

echo "[entrypoint] applying database migrations..."
alembic upgrade head

echo "[entrypoint] starting backend..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
