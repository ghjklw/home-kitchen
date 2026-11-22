#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

export KITCHEN_POSTGRES_PORT="${KITCHEN_POSTGRES_PORT:-55435}"
export KITCHEN_LOCAL_PORT="${KITCHEN_LOCAL_PORT:-18099}"
export KITCHEN_DATABASE_URL="postgresql+psycopg://kitchen:kitchen@127.0.0.1:${KITCHEN_POSTGRES_PORT}/kitchen"
export KITCHEN_LOCAL_DEV=true

docker compose up -d --wait db
uv sync --locked
uv run python -m alembic upgrade head

exec uv run python -m uvicorn home_kitchen.app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port "$KITCHEN_LOCAL_PORT" \
  --reload \
  --reload-dir src \
  --no-proxy-headers \
  --no-access-log
