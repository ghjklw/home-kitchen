"""Tests for shared household multi-account connectors, migration 0027, refresh, and purchase sync."""

from __future__ import annotations

import json
import runpy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from home_kitchen.connector_auth import ConnectorAuthService
from home_kitchen.connector_store import ConnectorStore
from home_kitchen.connector_store import metadata as connector_metadata
from home_kitchen.feature_api import feature_router
from home_kitchen.integrations import (
    AIIntegration,
    CookidooIntegration,
    CoopIntegration,
    IntegrationRegistry,
    OdaIntegration,
    Rema1000Integration,
    TrumfIntegration,
)
from home_kitchen.oda.repository import OdaRepository
from home_kitchen.oda.repository import metadata as oda_metadata
from home_kitchen.purchase_sync import (
    ProviderOrderRecord,
    ProviderPage,
    PurchaseHistorySyncService,
    checkpoint,
)
from home_kitchen.purchase_sync import (
    metadata as purchase_metadata,
)
from home_kitchen.settings import Settings


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    connector_metadata.create_all(engine)
    purchase_metadata.create_all(engine)
    oda_metadata.create_all(engine)
    return engine


@pytest.fixture
def cipher():
    return Fernet(Fernet.generate_key())


def build_multi_account_app(
    sqlite_engine,
    cipher,
    *,
    mock_transport: httpx.BaseTransport | None = None,
    actor_header: bool = True,
):
    hid = uuid4()
    store = ConnectorStore(sqlite_engine, cipher)
    settings = Settings(
        local_dev=True,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
    )

    class MockOda:
        configured = False

        def status(self, actor):
            return {"connected": False, "configured": False}

        def disconnect(self, actor):
            pass

    registry = IntegrationRegistry(
        [
            OdaIntegration(MockOda()),
            AIIntegration(settings),
            Rema1000Integration(store, hid),
            CoopIntegration(store, hid),
            TrumfIntegration(store, hid),
            CookidooIntegration(store, hid),
        ]
    )
    auth_service = ConnectorAuthService(
        store=store,
        integrations=registry,
        household_id=hid,
        transport=mock_transport,
        settings=settings,
    )
    features = SimpleNamespace(
        integrations=registry,
        connector_store=store,
        connector_auth=auth_service,
        household_id=hid,
        oda=MockOda(),
        settings=settings,
    )

    app = FastAPI()
    app.include_router(feature_router(features, basket=None))

    @app.middleware("http")
    async def actor_middleware(request, call_next):
        request.state.actor_id = (
            request.headers.get("x-test-actor", "malo") if actor_header else "malo"
        )
        return await call_next(request)

    return app, store, auth_service, hid


def test_alembic_migration_0027_structure_and_head():
    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0027_multi_account_connectors.py"
    )
    assert migration_path.exists()

    migration = runpy.run_path(str(migration_path))
    assert migration["revision"] == "0027_multi_account_connectors"
    assert len(migration["revision"]) <= 32
    assert migration["down_revision"] == "0026_recipe_metadata"

    with (
        patch.object(migration["op"], "add_column") as mock_add,
        patch.object(migration["op"], "drop_column") as mock_drop,
        patch.object(migration["op"], "drop_constraint") as mock_drop_c,
        patch.object(migration["op"], "create_unique_constraint") as mock_create_c,
        patch.object(migration["op"], "execute") as mock_exec,
    ):
        migration["upgrade"]()
        added_cols = [call[0][1].name for call in mock_add.call_args_list]
        assert added_cols == [
            "account_id",
            "account_label",
            "connected_by_actor_id",
            "updated_by_actor_id",
        ]
        mock_drop_c.assert_called_once_with(
            "uq_household_connector_provider",
            "household_connector_credentials",
            type_="unique",
        )
        mock_create_c.assert_called_once_with(
            "uq_household_connector_provider_account",
            "household_connector_credentials",
            ["household_id", "provider_id", "account_id"],
        )
        assert mock_exec.call_count == 1

        mock_drop_c.reset_mock()
        mock_create_c.reset_mock()
        mock_exec.reset_mock()

        migration["downgrade"]()
        assert mock_exec.call_count == 1
        mock_drop_c.assert_called_once_with(
            "uq_household_connector_provider_account",
            "household_connector_credentials",
            type_="unique",
        )
        mock_create_c.assert_called_once_with(
            "uq_household_connector_provider",
            "household_connector_credentials",
            ["household_id", "provider_id"],
        )
        dropped_cols = [call[0][1] for call in mock_drop.call_args_list]
        assert dropped_cols == [
            "updated_by_actor_id",
            "connected_by_actor_id",
            "account_label",
            "account_id",
        ]

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0027_multi_account_connectors"]


def test_downgrade_sql_keeps_at_most_one_row_per_household_provider(sqlite_engine):
    hid = uuid4()
    now = datetime.now(UTC)
    with sqlite_engine.begin() as conn:
        for idx, acc_id in enumerate(("default", "acc_partner")):
            conn.execute(
                text(
                    "INSERT INTO household_connector_credentials "
                    "(id, household_id, provider_id, account_id, auth_mode, encrypted_credentials, created_at, updated_at) "
                    "VALUES (:id, :hid, :pid, :aid, 'session_token', 'enc', :ts, :ts)"
                ),
                {
                    "id": str(uuid4()).replace("-", ""),
                    "hid": str(hid).replace("-", ""),
                    "pid": "rema1000",
                    "aid": acc_id,
                    "ts": (now + timedelta(minutes=idx)).isoformat(),
                },
            )
        conn.execute(
            text(
                """
                DELETE FROM household_connector_credentials
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY household_id, provider_id
                                   ORDER BY CASE WHEN account_id = 'default' THEN 0 ELSE 1 END,
                                            updated_at DESC,
                                            id DESC
                               ) AS rn
                        FROM household_connector_credentials
                    ) ranked
                    WHERE rn = 1
                )
                """
            )
        )
        remaining = conn.execute(
            text("SELECT account_id FROM household_connector_credentials")
        ).fetchall()
    assert [r[0] for r in remaining] == ["default"]


def test_connector_store_multi_account_and_actor_tracking(sqlite_engine, cipher):
    store = ConnectorStore(sqlite_engine, cipher)
    hid = uuid4()

    s1 = store.upsert_account_credentials(
        hid,
        "rema1000",
        "session_token",
        {"session_token": "tok-malo"},
        account_label="Malo",
        actor_id="actor-malo",
    )
    assert s1.account_id == "default"
    assert s1.account_label == "Malo"
    assert s1.connected_by_actor_id == "actor-malo"
    assert s1.updated_by_actor_id == "actor-malo"

    s2 = store.upsert_account_credentials(
        hid,
        "rema1000",
        "session_token",
        {"session_token": "tok-partner"},
        account_id="new",
        account_label="Partner",
        actor_id="actor-partner",
    )
    assert s2.account_id.startswith("acc_")
    assert s2.account_id != s1.account_id
    assert s2.account_label == "Partner"
    assert s2.connected_by_actor_id == "actor-partner"

    # Updating s1 as a different actor preserves connected_by_actor_id & label
    s1_updated = store.upsert_account_credentials(
        hid,
        "rema1000",
        "session_token",
        {"session_token": "tok-malo-new"},
        account_id=s1.account_id,
        actor_id="actor-partner",
    )
    assert s1_updated.account_label == "Malo"
    assert s1_updated.connected_by_actor_id == "actor-malo"
    assert s1_updated.updated_by_actor_id == "actor-partner"

    all_rema = store.list_credentials(hid, "rema1000")
    assert len(all_rema) == 2
    assert store.get_credentials(hid, "rema1000", account_id=s1.account_id) == {
        "session_token": "tok-malo-new"
    }
    assert store.get_credentials(hid, "rema1000", account_id=s2.account_id) == {
        "session_token": "tok-partner"
    }

    assert store.delete_account_credentials(hid, "rema1000", s1.account_id) is True
    assert len(store.list_credentials(hid, "rema1000")) == 1
    assert store.get_credentials(hid, "rema1000") == {"session_token": "tok-partner"}


def test_api_multi_account_lifecycle_pkce_trumf_and_delete(sqlite_engine, cipher):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token" or request.url.path == "/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": f"access-{request.url.host}",
                    "refresh_token": f"refresh-{request.url.host}",
                    "expires_in": 86400 * 30,
                },
            )
        return httpx.Response(404)

    app, store, _, hid = build_multi_account_app(
        sqlite_engine, cipher, mock_transport=httpx.MockTransport(mock_handler)
    )
    client = TestClient(app)

    # Connect two Rema 1000 accounts via PKCE from different household actors
    s1 = client.post(
        "/api/integrations/rema1000/pkce/start",
        json={"account_label": "Malo"},
        headers={"x-test-actor": "user-malo"},
    ).json()["state"]
    r1 = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "c1", "state": s1},
        headers={"x-test-actor": "user-malo"},
    )
    assert r1.status_code == 200

    s2 = client.post(
        "/api/integrations/rema1000/pkce/start",
        json={"account_label": "Partner", "add_account": True},
        headers={"x-test-actor": "user-partner"},
    ).json()["state"]
    r2 = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "c2", "state": s2},
        headers={"x-test-actor": "user-partner"},
    )
    assert r2.status_code == 200

    # Connect two Trumf accounts via PUT /api/integrations/trumf and POST /connect
    t1 = client.put(
        "/api/integrations/trumf",
        json={"session_cookie": "trumf-cookie-1", "account_label": "Trumf Malo"},
        headers={"x-test-actor": "user-malo"},
    )
    assert t1.status_code == 200
    t2 = client.post(
        "/api/integrations/trumf/connect",
        json={
            "session_cookie": "trumf-cookie-2",
            "account_label": "Trumf Partner",
            "add_account": True,
        },
        headers={"x-test-actor": "user-partner"},
    )
    assert t2.status_code == 200

    # Inspect GET /api/integrations
    items = {item["id"]: item for item in client.get("/api/integrations").json()}
    rema = items["rema1000"]
    assert rema["connected"] is True
    assert rema["status"] == "connected"
    assert rema["auth_type"] == "oauth_pkce"
    assert rema["token_status"] == "connected"
    assert rema["token_expires_at"] is not None
    assert rema["account_count"] == 2
    assert len(rema["accounts"]) == 2
    assert [a["account_label"] for a in rema["accounts"]] == ["Malo", "Partner"]
    assert [a["connected_by_actor_id"] for a in rema["accounts"]] == ["user-malo", "user-partner"]
    assert all(a["refresh_available"] is True for a in rema["accounts"])

    trumf = items["trumf"]
    assert trumf["account_count"] == 2
    assert [a["account_label"] for a in trumf["accounts"]] == ["Trumf Malo", "Trumf Partner"]
    assert all(a["refresh_available"] is True for a in trumf["accounts"])
    assert "trumf-cookie-1" not in json.dumps(items)
    assert "trumf-cookie-2" not in json.dumps(items)

    # Delete a specific Rema account
    acc2_id = rema["accounts"][1]["account_id"]
    del_one = client.delete(f"/api/integrations/rema1000/accounts/{acc2_id}")
    assert del_one.status_code == 204
    assert client.delete(f"/api/integrations/rema1000/accounts/{acc2_id}").status_code == 404

    items_after = {item["id"]: item for item in client.get("/api/integrations").json()}
    assert items_after["rema1000"]["account_count"] == 1
    assert items_after["rema1000"]["accounts"][0]["account_label"] == "Malo"

    # Delete all Trumf accounts via DELETE /api/integrations/trumf
    del_all_trumf = client.delete("/api/integrations/trumf")
    assert del_all_trumf.status_code == 204
    items_final = {item["id"]: item for item in client.get("/api/integrations").json()}
    assert items_final["trumf"]["account_count"] == 0
    assert items_final["trumf"]["connected"] is False


def test_api_refresh_all_and_single_account_and_background_loop(sqlite_engine, cipher):
    refresh_tokens_seen: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        refresh_tokens_seen.append(body)
        idx = len(refresh_tokens_seen)
        return httpx.Response(
            200,
            json={
                "access_token": f"fresh-acc-{idx}",
                "refresh_token": f"fresh-ref-{idx}",
                "expires_in": 3600,
            },
        )

    app, store, auth_service, hid = build_multi_account_app(
        sqlite_engine, cipher, mock_transport=httpx.MockTransport(mock_handler)
    )
    client = TestClient(app)

    a1 = store.save_connection(
        hid,
        "coop",
        "oauth_pkce",
        {"access_token": "old-1", "refresh_token": "ref-1"},
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        actor_id="user-1",
        account_label="Malo",
        metadata={"has_refresh_token": True},
    )
    a2 = store.save_connection(
        hid,
        "coop",
        "oauth_pkce",
        {"access_token": "old-2", "refresh_token": "ref-2"},
        expires_at=datetime.now(UTC) - timedelta(minutes=5),
        actor_id="user-2",
        account_label="Partner",
        add_account=True,
        metadata={"has_refresh_token": True},
    )

    # 1. Refresh only account 2 via ?account_id=...
    r_single = client.post(
        f"/api/integrations/coop/refresh?account_id={a2.account_id}",
        headers={"x-test-actor": "user-1"},
    )
    assert r_single.status_code == 200
    assert store.get_credentials(hid, "coop", a2.account_id)["access_token"] == "fresh-acc-1"
    assert store.get_credentials(hid, "coop", a1.account_id)["access_token"] == "old-1"
    # connected_by_actor_id stays user-2, updated_by_actor_id becomes user-1
    meta2 = store.get_connection_metadata(hid, "coop", a2.account_id)
    assert meta2 is not None
    assert meta2.connected_by_actor_id == "user-2"
    assert meta2.updated_by_actor_id == "user-1"
    assert meta2.account_label == "Partner"

    # 2. Refresh ALL accounts when account_id is omitted
    r_all = client.post("/api/integrations/coop/refresh", headers={"x-test-actor": "user-2"})
    assert r_all.status_code == 200
    assert store.get_credentials(hid, "coop", a1.account_id)["access_token"] == "fresh-acc-2"
    assert store.get_credentials(hid, "coop", a2.account_id)["access_token"] == "fresh-acc-3"

    # 3. Background loop refreshes multiple due accounts
    store.save_connection(
        hid,
        "coop",
        "oauth_pkce",
        {"access_token": "stale-1", "refresh_token": "ref-stale-1"},
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        actor_id="user-1",
        account_id=a1.account_id,
    )
    store.save_connection(
        hid,
        "coop",
        "oauth_pkce",
        {"access_token": "stale-2", "refresh_token": "ref-stale-2"},
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        actor_id="user-2",
        account_id=a2.account_id,
    )
    refreshed = auth_service.refresh_expiring_connections(margin_seconds=900)
    assert len(refreshed) == 2
    assert store.get_credentials(hid, "coop", a1.account_id)["access_token"].startswith(
        "fresh-acc-"
    )
    assert store.get_credentials(hid, "coop", a2.account_id)["access_token"].startswith(
        "fresh-acc-"
    )


def test_purchase_history_sync_iterates_all_accounts_and_deduplicates_receipts(
    sqlite_engine, cipher
):
    hid = uuid4()
    store = ConnectorStore(sqlite_engine, cipher)
    a1 = store.save_connection(
        hid,
        "rema1000",
        "oauth_pkce",
        {"access_token": "tok-1", "refresh_token": "ref-1"},
        account_label="Malo",
        actor_id="user-1",
    )
    a2 = store.save_connection(
        hid,
        "rema1000",
        "oauth_pkce",
        {"access_token": "tok-2", "refresh_token": "ref-2"},
        account_label="Partner",
        actor_id="user-2",
        add_account=True,
    )
    coop_a1 = store.save_connection(
        hid,
        "coop",
        "oauth_pkce",
        {"access_token": "coop-1", "refresh_token": "cref-1"},
        account_label="Coop Malo",
        actor_id="user-1",
    )
    trumf_a1 = store.save_connection(
        hid,
        "trumf",
        "session_cookie",
        {"session_cookie": "trumf-1"},
        account_label="Trumf Malo",
        actor_id="user-1",
    )

    # Also connect two Oda actors
    oda_repo = OdaRepository(sqlite_engine, cipher)
    for actor in ("oda-user-1", "oda-user-2"):
        gen = oda_repo.begin_connect(actor)
        assert oda_repo.save_connection(actor, {"access_token": f"tok-{actor}"}, gen)

    class MockRemaAdapter:
        def __init__(self, records):
            self.records = records

        def fetch_page(self, account_id, cursor):
            return ProviderPage(records=self.records, next_cursor="done", has_more=False)

    shared_receipt = ProviderOrderRecord(
        external_id="rema-rcpt-shared",
        revision_id="rev-1",
        source_updated_at=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        payload={"purchaseDate": "2026-05-01T09:55:00Z", "total": 120},
    )
    unique_a1 = ProviderOrderRecord(
        external_id="rema-rcpt-1",
        revision_id="rev-1",
        source_updated_at=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        payload={"purchaseDate": "2026-05-02T09:55:00Z", "total": 80},
    )
    unique_a2 = ProviderOrderRecord(
        external_id="rema-rcpt-2",
        revision_id="rev-1",
        source_updated_at=datetime(2026, 5, 3, 10, 0, tzinfo=UTC),
        payload={"purchaseDate": "2026-05-03T09:55:00Z", "total": 200},
    )

    def adapter_factory(provider: str, account_id: str, creds: dict):
        if provider == "rema1000" and account_id == a1.account_id:
            return MockRemaAdapter([shared_receipt, unique_a1])
        if provider == "rema1000" and account_id == a2.account_id:
            return MockRemaAdapter([shared_receipt, unique_a2])
        if provider == "coop" and account_id == coop_a1.account_id:
            return MockRemaAdapter(
                [
                    ProviderOrderRecord(
                        external_id="coop-rcpt-1",
                        revision_id="sha256:c1",
                        payload={"summary": {"purchaseDate": "2026-05-04T12:00:00Z"}},
                    )
                ]
            )
        if provider == "trumf" and account_id == trumf_a1.account_id:
            return MockRemaAdapter(
                [
                    ProviderOrderRecord(
                        external_id="trumf-rcpt-1",
                        revision_id="sha256:t1",
                        payload={"transaksjonsTidspunkt": "2026-05-05T14:00:00Z"},
                    )
                ]
            )
        return None

    fake_oda = SimpleNamespace(
        configured=True,
        repository=oda_repo,
        list_orders=AsyncMock(
            return_value={
                "hasMore": False,
                "orders": [
                    {
                        "orderNumber": "oda-shared-001",
                        "deliveryDate": "2026-05-06",
                        "currency": "NOK",
                        "grossAmount": 99.0,
                        "products": [],
                    }
                ],
            }
        ),
        track_order=AsyncMock(
            return_value={"orderNumber": "oda-shared-001", "status": "delivered"}
        ),
    )

    sync_service = PurchaseHistorySyncService(
        engine=sqlite_engine,
        oda_service=fake_oda,
        connector_store=store,
        household_id=hid,
        adapter_factory=adapter_factory,
    )

    res = sync_service.sync()
    assert res.results["rema1000"].status == "success"
    assert res.results["rema1000"].accounts_synced == 2
    assert res.results["coop"].status == "success"
    assert res.results["coop"].accounts_synced == 1
    assert res.results["trumf"].status == "success"
    assert res.results["trumf"].accounts_synced == 1
    assert res.results["oda"].status == "success"
    assert res.results["oda"].accounts_synced == 2

    # Checkpoints saved independently for both Rema accounts
    assert checkpoint(sqlite_engine, "rema1000", a1.account_id) == "done"
    assert checkpoint(sqlite_engine, "rema1000", a2.account_id) == "done"

    # Merged records deduplicate shared_receipt across Rema accounts (3 unique receipts, not 4!)
    rema_merged = sync_service.get_merged_records("rema1000")
    assert {r["external_id"] for r in rema_merged} == {
        "rema-rcpt-shared",
        "rema-rcpt-1",
        "rema-rcpt-2",
    }
    assert len(rema_merged) == 3

    # Oda shared order across two actors also deduplicates to 1 by external_id
    oda_merged = sync_service.get_merged_records("oda", deduplicate_by_external_id=True)
    assert [r["external_id"] for r in oda_merged] == ["oda-shared-001"]

    # Status reports merged deduplicated total_records across accounts
    status = sync_service.get_status()
    assert status.providers["rema1000"].coverage_window.total_records == 3
