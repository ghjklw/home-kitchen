"""Tests for UAT/production health, migration readiness, and image revision reporting."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from home_kitchen.app import create_app
from home_kitchen.service import KitchenService
from home_kitchen.settings import Settings
from home_kitchen.store import get_expected_alembic_head

REPO_ROOT = Path(__file__).resolve().parent.parent


class StubStore:
    def __init__(self, *, current_heads: tuple[str, ...] | None = None, db_error: bool = False):
        self.settings: dict[str, str] = {}
        self.photo_household_id = None
        self.engine = SimpleNamespace(dispose=lambda: None)
        self._current_heads = current_heads
        self._db_error = db_error

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def ensure_setting(self, key: str, value: str) -> str:
        if key not in self.settings:
            self.settings[key] = value
        return self.settings[key]

    def list(self, kind: str, model: type) -> list:
        return []

    def health(self) -> None:
        if self._db_error:
            raise OperationalError("SELECT 1", {}, Exception("postgresql://secret:pass@db/kitchen"))

    def migration_status(self, expected_head: str | None) -> tuple[bool, tuple[str, ...]]:
        if self._db_error:
            raise OperationalError("SELECT 1", {}, Exception("postgresql://secret:pass@db/kitchen"))
        heads = self._current_heads
        if heads is None:
            heads = (expected_head,) if expected_head is not None else ()
        return expected_head is not None and heads == (expected_head,), heads


def test_dockerfile_carries_build_revision_without_rebuilding_on_promotion():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG APP_REVISION=""' in dockerfile
    assert "KITCHEN_BUILD_REVISION=${APP_REVISION}" in dockerfile


def test_settings_normalizes_build_revision_and_release_channel():
    settings = Settings(
        database_url="postgresql+psycopg://localhost/kitchen_test",
        local_dev=True,
        build_revision="  ",
        release_channel=" UAT ",
    )
    assert settings.build_revision is None
    assert settings.release_channel == "uat"

    with pytest.raises(ValueError):
        Settings(
            database_url="postgresql+psycopg://localhost/kitchen_test",
            local_dev=True,
            release_channel="staging",
        )


def test_settings_forbids_local_dev_in_production_or_release_channels():
    for channel in ("production", "release"):
        with pytest.raises(ValueError, match="local_dev"):
            Settings(
                database_url="postgresql+psycopg://localhost/kitchen_test",
                local_dev=True,
                release_channel=channel,
            )

    for allowed_channel in ("local", "beta", "uat", None):
        cfg = Settings(
            database_url="postgresql+psycopg://localhost/kitchen_test",
            local_dev=True,
            release_channel=allowed_channel,
        )
        assert cfg.release_channel == allowed_channel


def test_healthz_and_readyz_allow_unauthenticated_probes_while_api_version_requires_auth():
    expected_head, head_count = get_expected_alembic_head()
    assert head_count == 1 and expected_head is not None

    store = StubStore(current_heads=(expected_head,))
    service = KitchenService(store=store)  # type: ignore[arg-type]
    settings = Settings(
        database_url="postgresql+psycopg://localhost/kitchen_test",
        entra_tenant_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        entra_audience="api://kitchen-test",
        entra_allowed_group=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        build_revision="a" * 40,
        release_channel="uat",
    )
    app = create_app(settings, service=service)
    with TestClient(app, client=("203.0.113.20", 5000)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {
            "status": "ready",
            "database": "ok",
            "migrations": "current",
            "alembic_head": expected_head,
        }
        assert client.get("/api/version").status_code == 401


def test_readyz_fails_closed_on_pending_migrations_or_db_error_without_leaking_secrets():
    service_stale = KitchenService(store=StubStore(current_heads=("0001_core",)))  # type: ignore[arg-type]
    app_stale = create_app(Settings(local_dev=True), service=service_stale)
    with TestClient(app_stale, client=("127.0.0.1", 5000)) as client:
        res = client.get("/readyz")
        assert res.status_code == 503
        body = res.json()
        assert body["status"] == "not_ready"
        assert body["migrations"] == "pending_or_divergent"
        assert body["current_heads"] == ["0001_core"]

    service_down = KitchenService(store=StubStore(db_error=True))  # type: ignore[arg-type]
    app_down = create_app(Settings(local_dev=True), service=service_down)
    with TestClient(app_down, client=("127.0.0.1", 5000)) as client:
        assert client.get("/healthz").status_code == 503
        res = client.get("/readyz")
        assert res.status_code == 503
        assert res.json() == {
            "status": "not_ready",
            "database": "unavailable",
            "migrations": "unknown",
        }
        assert "secret:pass" not in res.text


def test_api_version_exposes_revision_channel_and_alembic_head():
    expected_head, _ = get_expected_alembic_head()
    service = KitchenService(store=StubStore(current_heads=(expected_head,)))  # type: ignore[arg-type]
    settings = Settings(
        local_dev=True,
        build_revision="b" * 40,
        release_channel="uat",
    )
    app = create_app(settings, service=service)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        res = client.get("/api/version")
        assert res.status_code == 200
        payload = res.json()
        assert isinstance(payload["version"], str) and payload["version"]
        assert payload["channel"] == "uat"
        assert payload["revision"] == "b" * 40
        assert payload["alembic_head"] == expected_head


def test_readyz_against_migrated_postgresql(test_database_url):
    expected_head, _ = get_expected_alembic_head()
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        res = client.get("/readyz")
        assert res.status_code == 200
        assert res.json() == {
            "status": "ready",
            "database": "ok",
            "migrations": "current",
            "alembic_head": expected_head,
        }
