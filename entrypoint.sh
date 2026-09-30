#!/usr/bin/env sh
set -e

echo "[entrypoint] applying database migrations..."
alembic upgrade head

echo "[entrypoint] starting backend..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
