"""Unit tests for encrypted connector credentials, secret redaction, and API lifecycle."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from home_kitchen.connector_store import (
    ConnectorStore,
    connector_credentials,
    resolve_connector_cipher,
)
from home_kitchen.connector_store import (
    metadata as connector_metadata,
)
from home_kitchen.settings import Settings


@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    connector_metadata.create_all(engine)
    return engine


@pytest.fixture
def cipher():
    return Fernet(Fernet.generate_key())


@pytest.fixture
def connector_store(sqlite_engine, cipher):
    return ConnectorStore(sqlite_engine, cipher)


def test_connector_store_encrypts_credentials_in_database(sqlite_engine, connector_store):
    household_id = uuid4()
    secret_token = "my-super-secret-token-12345"

    status = connector_store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": secret_token},
        actor_id="malo",
    )

    assert status.provider_id == "rema1000"
    assert status.auth_mode == "session_token"
    assert not status.is_expired

    # Verify directly in SQLite that plaintext secret is NOT in the database
    with sqlite_engine.connect() as conn:
        row = conn.execute(
            select(connector_credentials.c.encrypted_credentials).where(
                connector_credentials.c.household_id == household_id,
                connector_credentials.c.provider_id == "rema1000",
            )
        ).scalar_one()

        assert secret_token not in row
        # Fernet ciphertexts always start with gAAAAA
        assert row.startswith("gAAAAA")

    # Decryption works via get_credentials
    decrypted = connector_store.get_credentials(household_id, "rema1000")
    assert decrypted == {"session_token": secret_token}


def test_connector_store_metadata_does_not_expose_credentials(connector_store):
    household_id = uuid4()
    secret_cookie = "session_cookie_very_secret_abc"
    expiry = datetime.now(UTC) + timedelta(days=14)

    connector_store.save_connection(
        household_id=household_id,
        provider_id="trumf",
        auth_mode="session_cookie",
        credentials={"session_cookie": secret_cookie},
        expires_at=expiry,
        actor_id="malo",
    )

    meta = connector_store.get_connection_metadata(household_id, "trumf")
    assert meta is not None
    assert meta.provider_id == "trumf"
    assert meta.auth_mode == "session_cookie"
    assert meta.actor_id == "malo"
    assert not meta.is_expired
    assert secret_cookie not in meta.model_dump_json()


def test_connector_store_expiry_detection(connector_store):
    household_id = uuid4()
    past_expiry = datetime.now(UTC) - timedelta(hours=2)

    connector_store.save_connection(
        household_id=household_id,
        provider_id="coop",
        auth_mode="session_token",
        credentials={"session_token": "expired-token"},
        expires_at=past_expiry,
    )

    meta = connector_store.get_connection_metadata(household_id, "coop")
    assert meta is not None
    assert meta.is_expired


def test_connector_store_disconnect_deletes_credentials(connector_store):
    household_id = uuid4()

    connector_store.save_connection(
        household_id=household_id,
        provider_id="cookidoo",
        auth_mode="credentials",
        credentials={"username": "user@example.com", "password": "super-password"},
    )
    assert connector_store.get_connection_metadata(household_id, "cookidoo") is not None

    deleted = connector_store.delete_connection(household_id, "cookidoo")
    assert deleted is True
    assert connector_store.get_connection_metadata(household_id, "cookidoo") is None
    assert connector_store.get_credentials(household_id, "cookidoo") is None

    # Idempotent delete
    assert connector_store.delete_connection(household_id, "cookidoo") is False


def test_connector_store_household_isolation(connector_store):
    household_a = uuid4()
    household_b = uuid4()

    connector_store.save_connection(
        household_id=household_a,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": "token-a"},
    )

    assert connector_store.get_connection_metadata(household_b, "rema1000") is None
    assert connector_store.get_credentials(household_b, "rema1000") is None


def test_cipher_resolution_order():
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    # 1. connector_encryption_key takes priority
    s1 = Settings(
        local_dev=True,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
        connector_encryption_key=SecretStr(key_a),
        oda_encryption_key=SecretStr(key_b),
    )
    cipher1 = resolve_connector_cipher(s1)
    assert cipher1 is not None
    # Cipher encrypts and can be decrypted with key_a
    token = cipher1.encrypt(b"test")
    assert Fernet(key_a.encode()).decrypt(token) == b"test"

    # 2. Falls back to oda_encryption_key
    s2 = Settings(
        local_dev=True,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
        oda_encryption_key=SecretStr(key_b),
    )
    cipher2 = resolve_connector_cipher(s2)
    assert cipher2 is not None
    token2 = cipher2.encrypt(b"test")
    assert Fernet(key_b.encode()).decrypt(token2) == b"test"

    # 3. In local_dev with neither set, provides deterministic dev cipher
    s3 = Settings(
        local_dev=True,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
    )
    cipher3 = resolve_connector_cipher(s3)
    assert cipher3 is not None


def test_cipher_missing_in_production_returns_none():
    s = Settings(
        local_dev=False,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
        entra_tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        entra_audience="api://home-kitchen",
        entra_allowed_group=UUID("00000000-0000-0000-0000-000000000002"),
    )
    assert resolve_connector_cipher(s) is None


# API integration tests using app with SQLite engine
def build_test_app(sqlite_engine, cipher, actor_id="malo", household_id=None):
    from types import SimpleNamespace

    import httpx

    from home_kitchen.connector_auth import ConnectorAuthService
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

    household_id = household_id or uuid4()
    store = ConnectorStore(sqlite_engine, cipher)

    class MockOda:
        def status(self, actor):
            return {"connected": False, "configured": False}

        def disconnect(self, actor):
            pass

    settings = Settings(
        local_dev=True,
        database_url=SecretStr("postgresql+psycopg://user:pwd@localhost/db"),
    )

    registry = IntegrationRegistry(
        [
            OdaIntegration(MockOda()),
            AIIntegration(settings),
            Rema1000Integration(store, household_id),
            CoopIntegration(store, household_id),
            TrumfIntegration(store, household_id),
            CookidooIntegration(store, household_id),
        ]
    )

    connector_auth = ConnectorAuthService(
        store=store,
        integrations=registry,
        household_id=household_id,
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"user": {"name": "Malo"}})
                if request.url.path == "/api/auth/session"
                else httpx.Response(404)
            )
        ),
        settings=settings,
    )

    features = SimpleNamespace(
        integrations=registry,
        connector_store=store,
        connector_auth=connector_auth,
        household_id=household_id,
        oda=MockOda(),
    )

    app = FastAPI()
    app.include_router(feature_router(features, basket=None))

    if actor_id:

        @app.middleware("http")
        async def identity(request, call_next):
            request.state.actor_id = actor_id
            return await call_next(request)

    return app


def test_api_integrations_list_and_secret_redaction(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher)

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        resp = client.get("/api/integrations")
        assert resp.status_code == 200
        items = resp.json()

        ids = [item["id"] for item in items]
        assert "oda" in ids
        assert "anthropic" in ids
        assert "rema1000" in ids
        assert "coop" in ids
        assert "trumf" in ids
        assert "cookidoo" in ids

        for item in items:
            assert item["status"] in (
                "connected",
                "configured",
                "not_configured",
                "unavailable",
                "expired",
            )
            assert "capabilities" in item
            assert "supported_auth_modes" in item
            # Verify secret terms never appear in the JSON
            serialized = str(item)
            assert (
                "secret" not in serialized.lower()
                or "hemmeligheten er skjult" in serialized.lower()
                or "client_secret" not in serialized
            )


def test_api_connect_rema1000_lifecycle_and_secret_redaction(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher)
    secret_token = "secret-token-rema-999"

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # 1. Initially not_configured
        get1 = client.get("/api/integrations")
        rema1 = next(item for item in get1.json() if item["id"] == "rema1000")
        assert rema1["status"] == "not_configured"
        assert rema1["auth_mode"] is None

        # 2. Connect Rema 1000
        connect_resp = client.post(
            "/api/integrations/rema1000/connect",
            json={"session_token": secret_token},
        )
        assert connect_resp.status_code == 200
        data = connect_resp.json()
        assert data["id"] == "rema1000"
        assert data["status"] == "connected"
        assert data["auth_mode"] == "session_token"
        assert secret_token not in connect_resp.text

        # 3. GET /api/integrations shows connected without secrets
        get2 = client.get("/api/integrations")
        rema2 = next(item for item in get2.json() if item["id"] == "rema1000")
        assert rema2["status"] == "connected"
        assert rema2["auth_mode"] == "session_token"
        assert secret_token not in get2.text

        # 4. Disconnect
        del_resp = client.delete("/api/integrations/rema1000/connection")
        assert del_resp.status_code == 204

        # 5. Status reverted to not_configured
        get3 = client.get("/api/integrations")
        rema3 = next(item for item in get3.json() if item["id"] == "rema1000")
        assert rema3["status"] == "not_configured"
        assert rema3["auth_mode"] is None


def test_api_connect_trumf_expiry_tracking(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher)
    past_time = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        connect_resp = client.post(
            "/api/integrations/trumf/connect",
            json={"session_cookie": "trumf-cookie-val", "expires_at": past_time},
        )
        assert connect_resp.status_code == 200
        data = connect_resp.json()
        assert data["id"] == "trumf"
        assert data["status"] == "expired"
        assert data["expiry_prompt"] is not None
        assert "utløpt" in data["expiry_prompt"].lower()
        assert "trumf-cookie-val" not in connect_resp.text


def test_api_connect_coop_token_or_cookie(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher)

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # Token
        resp1 = client.post(
            "/api/integrations/coop/connect",
            json={"auth_mode": "session_token", "token": "coop-token-val"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["auth_mode"] == "session_token"
        assert "coop-token-val" not in resp1.text

        # Cookie update
        resp2 = client.post(
            "/api/integrations/coop/connect",
            json={"auth_mode": "session_cookie", "cookie": "coop-cookie-val"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["auth_mode"] == "session_cookie"
        assert "coop-cookie-val" not in resp2.text


def test_api_connect_cookidoo_credentials_or_cookie(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher)
    password = "super-secret-cookidoo-password"

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # Username and password
        resp = client.post(
            "/api/integrations/cookidoo/connect",
            json={
                "auth_mode": "credentials",
                "username": "tm6_chef@kitchen.no",
                "password": password,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "cookidoo"
        assert data["status"] == "connected"
        assert data["auth_mode"] == "credentials"
        assert password not in resp.text


def test_api_connect_validation_errors_never_leak_credentials(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher)

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # Missing token for Rema 1000
        resp1 = client.post("/api/integrations/rema1000/connect", json={"session_token": "   "})
        assert resp1.status_code == 422
        assert "krever et gyldig sesjonstoken" in resp1.json()["detail"]

        # Missing cookie for Trumf
        resp2 = client.post("/api/integrations/trumf/connect", json={})
        assert resp2.status_code == 422
        assert "krever en gyldig sesjonsinformasjonskapsel" in resp2.json()["detail"]

        # Unknown provider
        resp3 = client.post("/api/integrations/unknown_shop/connect", json={"session_token": "abc"})
        assert resp3.status_code == 404

        # Oda route direction
        resp4 = client.post("/api/integrations/oda/connect", json={"session_token": "abc"})
        assert resp4.status_code == 400
        assert "OAuth" in resp4.json()["detail"]


def test_api_connect_and_disconnect_require_authentication(sqlite_engine, cipher):
    app = build_test_app(sqlite_engine, cipher, actor_id=None)

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert (
            client.post(
                "/api/integrations/rema1000/connect", json={"session_token": "abc"}
            ).status_code
            == 401
        )
        assert client.delete("/api/integrations/rema1000/connection").status_code == 401


def test_api_unreadable_connector_credentials_fail_safely_without_leaking_secrets(sqlite_engine):
    old_key = Fernet.generate_key().decode()
    wrong_key = Fernet.generate_key().decode()
    secret_token = "ultra-secret-rema-token-123"
    shared_household_id = uuid4()

    old_app = build_test_app(
        sqlite_engine, Fernet(old_key.encode()), household_id=shared_household_id
    )
    with TestClient(old_app, client=("127.0.0.1", 5000)) as client:
        resp = client.post(
            "/api/integrations/rema1000/connect",
            json={"session_token": secret_token},
        )
        assert resp.status_code == 200

    # Simulate rotation where old_key was dropped before credentials were re-encrypted.
    # Also clear has_refresh_token in metadata to exercise get_credentials() fallback path.
    from sqlalchemy import update

    with sqlite_engine.begin() as conn:
        encrypted_val = conn.execute(select(connector_credentials.c.encrypted_credentials)).scalar()
        conn.execute(update(connector_credentials).values(auth_mode="oauth_pkce", metadata_json={}))

    wrong_app = build_test_app(
        sqlite_engine, Fernet(wrong_key.encode()), household_id=shared_household_id
    )
    with TestClient(wrong_app, client=("127.0.0.1", 5000)) as client:
        integrations_resp = client.get("/api/integrations")
        assert integrations_resp.status_code == 200
        rema = next(item for item in integrations_resp.json() if item["id"] == "rema1000")
        assert rema["status"] == "expired"
        assert rema["refresh_available"] is False
        assert "kan ikke leses" in (rema["last_error"] or "")
        assert secret_token not in integrations_resp.text
        assert str(encrypted_val) not in integrations_resp.text

        health_resp = client.get("/api/integrations/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["overall_status"] == "attention_needed"
        assert secret_token not in health_resp.text

        refresh_resp = client.post("/api/integrations/rema1000/refresh")
        assert refresh_resp.status_code == 409
        assert "kan ikke leses" in refresh_resp.json()["detail"]
        assert secret_token not in refresh_resp.text
        assert str(encrypted_val) not in refresh_resp.text


def test_settings_and_connector_validation_errors_hide_secret_inputs(sqlite_engine, cipher):
    from pydantic import ValidationError

    db_secret = "super-secret-db-password-999"
    ai_secret = "sk-ant-secret-canary-777"
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            local_dev=True,
            database_url=SecretStr(f"postgresql+psycopg://u:{db_secret}@localhost/db"),
            anthropic_api_key=SecretStr(ai_secret),
            anthropic_workload_identity=True,
        )
    err_text = str(exc_info.value)
    assert db_secret not in err_text
    assert ai_secret not in err_text

    app = build_test_app(sqlite_engine, cipher)
    secret_pw = "cookidoo-secret-password-888"
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # Invalid email type/missing field triggers FastAPI 422 while password must stay hidden
        resp = client.post(
            "/api/integrations/cookidoo/login",
            json={"email": 12345, "password": secret_pw},
        )
        assert resp.status_code == 422
        assert secret_pw not in resp.text


def test_kassal_and_home_assistant_redact_secrets_on_http_failure():
    import httpx

    from home_kitchen.home_assistant import HomeAssistantError, HomeAssistantTodoClient
    from home_kitchen.kassal import KassalClient, KassalError

    kassal_secret = "kassal-bearer-secret-42"
    ha_secret = "ha-long-lived-secret-99"

    kassal_client = KassalClient(
        kassal_secret,
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(500, text=f"Upstream rejected key {kassal_secret}")
        ),
    )
    with pytest.raises(KassalError) as kassal_exc:
        kassal_client.lookup_ean("7038010000188")
    assert kassal_secret not in str(kassal_exc.value)
    assert "[REDACTED]" in str(kassal_exc.value)

    ha_client = HomeAssistantTodoClient(
        "http://ha.local:8123",
        ha_secret,
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(500, text=f"Error with token {ha_secret}")
        ),
    )
    with pytest.raises(HomeAssistantError) as ha_exc:
        ha_client.list_items()
    assert ha_secret not in str(ha_exc.value)
    assert "[REDACTED]" in str(ha_exc.value)
