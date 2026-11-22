---
name: local-test-instance
description: Start the Home Kitchen app locally with its Docker PostgreSQL database for development or browser QA.
---

# Run Home Kitchen locally

Use this skill when asked to launch, restart or make the Home Kitchen app available for local development or browser QA.

## Start

1. Work from the Home Kitchen repository root.
2. Check whether the requested app port is already in use. Do not stop an unrelated process to free it. Set `KITCHEN_LOCAL_PORT` to another port when needed.
3. Run `./scripts/run-local.sh` in a persistent terminal session. It starts the bounded PostgreSQL Compose service, syncs locked dependencies, applies migrations and launches FastAPI on `127.0.0.1:18099` by default.
4. Confirm the app responds at `http://127.0.0.1:${KITCHEN_LOCAL_PORT:-18099}/readyz` and report the URL. Keep the server running when the user asked to run the app.

The script uses the local database at `127.0.0.1:${KITCHEN_POSTGRES_PORT:-55435}` and enables `KITCHEN_LOCAL_DEV`. This mode is intended for the loopback-bound local server. Never use it for a deployed instance.

## Stop or reset

- Stop FastAPI with Ctrl+C in the terminal session.
- Stop the database container with `docker compose stop db`. Its Docker volume remains available when restarted.
- `docker compose down` removes the container and preserves database data.
- `docker compose down -v` deletes the local database volume. Use it only when a database reset was requested.
