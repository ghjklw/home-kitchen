"""Offline unit tests for interactive connector authentication flows.

Covers:
- Guided OIDC PKCE flow for Rema 1000 and Coop (S256 challenge, state pop, redirect_url parsing)
- Cookidoo Vorwerk/Cidaas OAuth login and no-password-stored invariant
- Trumf SMS OTP flow (start, verify, replay protection, session expiry)
- Secret redaction across all endpoints and manual connect fallback
Uses httpx.MockTransport for 100% offline testing with zero live credentials or networks.
"""

import base64
import hashlib
import json
import urllib.parse
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from home_kitchen.connector_auth import ConnectorAuthService
from home_kitchen.connector_store import (
    ConnectorStore,
    connector_credentials,
)
from home_kitchen.connector_store import (
    metadata as connector_metadata,
)
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


def create_test_app(
    sqlite_engine,
    cipher,
    mock_transport: httpx.BaseTransport | None = None,
    household_id: UUID | None = None,
    actor_id: str = "malo",
    auth_service: ConnectorAuthService | None = None,
):
    from types import SimpleNamespace

    hid = household_id or uuid4()
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
            Rema1000Integration(store, hid),
            CoopIntegration(store, hid),
            TrumfIntegration(store, hid),
            CookidooIntegration(store, hid),
        ]
    )

    service = auth_service or ConnectorAuthService(
        store=store,
        integrations=registry,
        household_id=hid,
        transport=mock_transport,
        settings=settings,
    )

    features = SimpleNamespace(
        integrations=registry,
        connector_store=store,
        connector_auth=service,
        household_id=hid,
        oda=MockOda(),
        settings=settings,
    )

    app = FastAPI()
    app.include_router(feature_router(features, basket=None))

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.actor_id = actor_id
        return await call_next(request)

    return app, service, store, hid


# =============================================================================
# 1. Guided OIDC PKCE Flow (Rema 1000 & Coop)
# =============================================================================


def test_pkce_start_generates_valid_s256_challenge(sqlite_engine, cipher):
    app, auth_service, _, _ = create_test_app(sqlite_engine, cipher)
    client = TestClient(app)

    response = client.post("/api/integrations/rema1000/pkce/start")
    assert response.status_code == 200
    data = response.json()

    assert data["provider_id"] == "rema1000"
    assert "state" in data and len(data["state"]) > 16
    assert "redirect_uri" in data
    assert "instructions" in data
    assert "authorize_url" in data

    parsed_url = urllib.parse.urlsplit(data["authorize_url"])
    assert parsed_url.netloc == "id.rema.no"
    assert parsed_url.path == "/connect/authorize"

    query = urllib.parse.parse_qs(parsed_url.query)
    assert query["response_type"] == ["code"]
    assert query["state"] == [data["state"]]
    assert query["code_challenge_method"] == ["S256"]
    challenge_in_url = query["code_challenge"][0]

    # Verify mathematically that the S256 challenge corresponds to the stored code_verifier
    stored_tx = auth_service._pkce_transactions[data["state"]]
    assert stored_tx.code_verifier is not None
    expected_digest = hashlib.sha256(stored_tx.code_verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(expected_digest).rstrip(b"=").decode("ascii")
    assert challenge_in_url == expected_challenge


def test_coop_pkce_start_endpoints(sqlite_engine, cipher):
    app, _, _, _ = create_test_app(sqlite_engine, cipher)
    client = TestClient(app)

    response = client.post(
        "/api/integrations/coop/pkce/start",
        json={"redirect_uri": "https://custom.app/callback"},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["provider_id"] == "coop"
    assert data["redirect_uri"] == "https://custom.app/callback"
    parsed_url = urllib.parse.urlsplit(data["authorize_url"])
    assert parsed_url.netloc == "login.coop.no"
    assert parsed_url.path == "/authorize"
    query = urllib.parse.parse_qs(parsed_url.query)
    assert query["redirect_uri"] == ["https://custom.app/callback"]


def test_pkce_complete_exchanges_code_and_stores_encrypted_tokens(sqlite_engine, cipher):
    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.path == "/connect/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "rema-access-token-xyz-123",
                    "refresh_token": "rema-refresh-token-abc-456",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, store, household_id = create_test_app(
        sqlite_engine, cipher, mock_transport=mock_transport
    )
    client = TestClient(app)

    # 1. Start PKCE flow
    start_resp = client.post("/api/integrations/rema1000/pkce/start")
    assert start_resp.status_code == 200
    state = start_resp.json()["state"]

    # 2. Complete PKCE flow with code + state
    complete_resp = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "auth-code-789", "state": state},
    )
    assert complete_resp.status_code == 200
    info = complete_resp.json()

    # Redaction check: Never return tokens in response
    assert info["id"] == "rema1000"
    assert info["status"] == "connected"
    assert info["auth_mode"] == "oauth_pkce"
    serialized_info = json.dumps(info)
    assert "rema-access-token" not in serialized_info
    assert "rema-refresh-token" not in serialized_info

    # Verify request sent to token endpoint
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert req.url.path == "/connect/token"
    body = urllib.parse.parse_qs(req.read().decode())
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["auth-code-789"]
    assert "code_verifier" in body

    # Verify encrypted storage in DB
    credentials = store.get_credentials(household_id, "rema1000")
    assert credentials is not None
    assert credentials["access_token"] == "rema-access-token-xyz-123"
    assert credentials["refresh_token"] == "rema-refresh-token-abc-456"

    # Verify expiration set
    meta = store.get_connection_metadata(household_id, "rema1000")
    assert meta is not None
    assert meta.expires_at is not None
    assert meta.expires_at > datetime.now(UTC)


def test_pkce_complete_parses_redirect_url_query_and_fragment(sqlite_engine, cipher):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "coop-token-999",
                "refresh_token": "coop-refresh-888",
                "expires_in": 7200,
            },
        )

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, store, household_id = create_test_app(
        sqlite_engine, cipher, mock_transport=mock_transport
    )
    client = TestClient(app)

    # Query param parsing
    start_resp = client.post("/api/integrations/coop/pkce/start")
    state = start_resp.json()["state"]

    redirect_url = f"https://login.coop.no/oauth/callback?code=query-code-42&state={state}"
    resp1 = client.post(
        "/api/integrations/coop/pkce/complete",
        json={"redirect_url": redirect_url},
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "connected"
    assert store.get_credentials(household_id, "coop")["access_token"] == "coop-token-999"

    # Fragment param parsing (e.g. #code=...&state=...)
    start_resp2 = client.post("/api/integrations/coop/pkce/start")
    state2 = start_resp2.json()["state"]

    fragment_redirect_url = f"https://login.coop.no/oauth/callback#code=frag-code-99&state={state2}"
    resp2 = client.post(
        "/api/integrations/coop/pkce/complete",
        json={"redirect_url": fragment_redirect_url},
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "connected"


def test_pkce_security_replay_and_state_mismatch_rejection(sqlite_engine, cipher):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    mock_transport = httpx.MockTransport(mock_handler)
    app, auth_service, _, household_id = create_test_app(
        sqlite_engine, cipher, mock_transport=mock_transport
    )
    client = TestClient(app)

    start_resp = client.post("/api/integrations/rema1000/pkce/start")
    state = start_resp.json()["state"]

    # 1. State mismatch / unknown state rejected
    unknown_resp = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "code1", "state": "completely-unknown-state"},
    )
    assert unknown_resp.status_code == 400

    # 2. Missing code or state rejected
    missing_resp = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": None, "state": None},
    )
    assert missing_resp.status_code == 422

    # 3. Provider mismatch rejected (start on rema1000, complete on coop)
    provider_mismatch = client.post(
        "/api/integrations/coop/pkce/complete",
        json={"code": "code1", "state": state},
    )
    assert provider_mismatch.status_code == 400

    # 4. Household mismatch rejected
    other_household = uuid4()
    app2, _, _, _ = create_test_app(
        sqlite_engine,
        cipher,
        mock_transport=mock_transport,
        household_id=other_household,
        auth_service=auth_service,
    )
    client2 = TestClient(app2)
    start_resp_hh = client.post("/api/integrations/rema1000/pkce/start")
    state_hh = start_resp_hh.json()["state"]
    # Attempt complete with client from other household
    other_hh_resp = client2.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "code1", "state": state_hh},
    )
    assert other_hh_resp.status_code == 403

    # 5. Successful complete works once
    start_resp_ok = client.post("/api/integrations/rema1000/pkce/start")
    state_ok = start_resp_ok.json()["state"]
    ok_resp = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "code1", "state": state_ok},
    )
    assert ok_resp.status_code == 200

    # 6. Replay rejection: Second attempt with same state fails (single-use atomic pop)
    replay_resp = client.post(
        "/api/integrations/rema1000/pkce/complete",
        json={"code": "code1", "state": state_ok},
    )
    assert replay_resp.status_code == 400


def test_pkce_unsupported_providers_rejected(sqlite_engine, cipher):
    app, _, _, _ = create_test_app(sqlite_engine, cipher)
    client = TestClient(app)

    # Trumf and Cookidoo do not use PKCE
    assert client.post("/api/integrations/trumf/pkce/start").status_code == 400
    assert client.post("/api/integrations/cookidoo/pkce/start").status_code == 400
    assert client.post("/api/integrations/unknown_foo/pkce/start").status_code == 404


# =============================================================================
# 2. Cookidoo Interactive OAuth2 Login (Vorwerk/Cidaas)
# =============================================================================


def test_cookidoo_login_success_and_no_password_stored_invariant(sqlite_engine, cipher):
    received_requests = []
    plain_password = "my-ultra-secret-plain-cookidoo-password-12345"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        received_requests.append(request)
        if request.url.path == "/token-srv/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "cidaas-bearer-access-token-9988",
                    "refresh_token": "cidaas-bearer-refresh-token-7766",
                    "token_type": "Bearer",
                    "expires_in": 86400,
                },
            )
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, store, household_id = create_test_app(
        sqlite_engine, cipher, mock_transport=mock_transport
    )
    client = TestClient(app)

    response = client.post(
        "/api/integrations/cookidoo/login",
        json={
            "email": "chef@example.com",
            "password": plain_password,
            "country": "no",
        },
    )
    assert response.status_code == 200
    info = response.json()
    assert info["id"] == "cookidoo"
    assert info["status"] == "connected"
    assert info["auth_mode"] == "oauth_password"

    # Redaction check: Tokens and password NOT in HTTP response
    resp_str = json.dumps(info)
    assert plain_password not in resp_str
    assert "cidaas-bearer-access-token" not in resp_str
    assert "cidaas-bearer-refresh-token" not in resp_str

    # Request validation: sent to Cidaas token endpoint
    assert len(received_requests) == 1
    req = received_requests[0]
    assert req.method == "POST"
    assert req.url.path == "/token-srv/token"
    data = urllib.parse.parse_qs(req.read().decode())
    assert data["grant_type"] == ["password"]
    assert data["username"] == ["chef@example.com"]
    assert data["password"] == [plain_password]

    # Stored credentials check
    creds = store.get_credentials(household_id, "cookidoo")
    assert creds is not None
    assert creds["access_token"] == "cidaas-bearer-access-token-9988"
    assert creds["refresh_token"] == "cidaas-bearer-refresh-token-7766"
    assert creds["country"] == "no"

    # ABSOLUTE INVARIANT: The raw password is NEVER stored anywhere
    assert "password" not in creds
    for val in creds.values():
        assert val != plain_password

    # Inspect raw SQLite database row directly to confirm password is not in DB
    with sqlite_engine.connect() as conn:
        row = conn.execute(
            select(
                connector_credentials.c.encrypted_credentials,
                connector_credentials.c.metadata_json,
            ).where(
                connector_credentials.c.household_id == household_id,
                connector_credentials.c.provider_id == "cookidoo",
            )
        ).one()
        enc_creds, meta = row
        # Raw password must not be in ciphertext or plaintext metadata
        assert plain_password not in enc_creds
        assert plain_password not in json.dumps(meta)


def test_cookidoo_login_invalid_credentials_returns_401(sqlite_engine, cipher):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_credentials"})

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, store, household_id = create_test_app(
        sqlite_engine, cipher, mock_transport=mock_transport
    )
    client = TestClient(app)

    response = client.post(
        "/api/integrations/cookidoo/login",
        json={"email": "wrong@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert store.get_credentials(household_id, "cookidoo") is None


def test_cookidoo_login_validation_errors(sqlite_engine, cipher):
    app, _, _, _ = create_test_app(sqlite_engine, cipher)
    client = TestClient(app)

    # Empty email
    resp1 = client.post(
        "/api/integrations/cookidoo/login",
        json={"email": "   ", "password": "password"},
    )
    assert resp1.status_code == 422

    # Empty password
    resp2 = client.post(
        "/api/integrations/cookidoo/login",
        json={"email": "user@example.com", "password": "  "},
    )
    assert resp2.status_code == 422


# =============================================================================
# 3. Trumf Interactive Phone / SMS OTP Flow
# =============================================================================


def test_trumf_otp_start_and_verify_flow(sqlite_engine, cipher):
    otp_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        otp_requests.append(request)
        if request.url.path == "/api/auth/otp/send":
            return httpx.Response(200, json={"status": "sent"})
        if request.url.path == "/api/auth/otp/verify":
            payload = json.loads(request.read().decode())
            if payload.get("otp") == "654321":
                resp = httpx.Response(200, json={"session_cookie": "trumf-cookie-val-abc-xyz"})
                resp.headers["set-cookie"] = "trumf_session=trumf-cookie-val-abc-xyz; Path=/"
                return resp
            return httpx.Response(401, json={"error": "invalid_otp"})
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, store, household_id = create_test_app(
        sqlite_engine, cipher, mock_transport=mock_transport
    )
    client = TestClient(app)

    # 1. Start OTP request
    phone_number = "+47 912 34 567"
    start_resp = client.post(
        "/api/integrations/trumf/login/start",
        json={"phone": phone_number},
    )
    assert start_resp.status_code == 200
    start_data = start_resp.json()
    assert "session_id" in start_data and len(start_data["session_id"]) > 16
    assert "detail" in start_data

    # Verify request sent to Trumf endpoint
    assert len(otp_requests) == 1
    assert otp_requests[0].url.path == "/api/auth/otp/send"
    assert json.loads(otp_requests[0].read().decode())["phone"] == phone_number

    # 2. Verify with wrong OTP
    session_id = start_data["session_id"]
    wrong_resp = client.post(
        "/api/integrations/trumf/login/verify",
        json={"session_id": session_id, "otp": "000000"},
    )
    assert wrong_resp.status_code == 401
    assert store.get_credentials(household_id, "trumf") is None

    # Restart session because single-use pop consumed it
    start_resp2 = client.post(
        "/api/integrations/trumf/login/start",
        json={"phone": phone_number},
    )
    session_id2 = start_resp2.json()["session_id"]

    # 3. Verify with correct OTP
    verify_resp = client.post(
        "/api/integrations/trumf/login/verify",
        json={"session_id": session_id2, "otp": "654321"},
    )
    assert verify_resp.status_code == 200
    info = verify_resp.json()
    assert info["id"] == "trumf"
    assert info["status"] == "connected"
    assert info["auth_mode"] == "interactive_otp"

    # Redaction check: Cookie value never exposed in response
    assert "trumf-cookie-val" not in json.dumps(info)

    # Verify encrypted storage in DB
    creds = store.get_credentials(household_id, "trumf")
    assert creds is not None
    assert creds["session_cookie"] == "trumf-cookie-val-abc-xyz"
    assert creds["phone"] == phone_number

    # Verify expiration (up to 12 months)
    meta = store.get_connection_metadata(household_id, "trumf")
    assert meta is not None
    assert meta.expires_at is not None
    assert meta.expires_at > datetime.now(UTC) + timedelta(days=300)


def test_trumf_otp_replay_and_security(sqlite_engine, cipher):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"session_cookie": "trumf-cookie"})

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, _, _ = create_test_app(sqlite_engine, cipher, mock_transport=mock_transport)
    client = TestClient(app)

    # Invalid phone
    assert (
        client.post("/api/integrations/trumf/login/start", json={"phone": " "}).status_code == 422
    )

    # Start session
    start_resp = client.post("/api/integrations/trumf/login/start", json={"phone": "+4712345678"})
    session_id = start_resp.json()["session_id"]

    # Unknown session_id rejected
    assert (
        client.post(
            "/api/integrations/trumf/login/verify",
            json={"session_id": "non-existent-session-id", "otp": "1234"},
        ).status_code
        == 400
    )

    # Verify once
    resp1 = client.post(
        "/api/integrations/trumf/login/verify",
        json={"session_id": session_id, "otp": "1234"},
    )
    assert resp1.status_code == 200

    # Replay rejected (session consumed)
    resp2 = client.post(
        "/api/integrations/trumf/login/verify",
        json={"session_id": session_id, "otp": "1234"},
    )
    assert resp2.status_code == 400


# =============================================================================
# 4. Manual Connect Fallback Intact
# =============================================================================


def test_manual_connect_fallback_remains_functional(sqlite_engine, cipher):
    app, _, store, household_id = create_test_app(sqlite_engine, cipher)
    client = TestClient(app)

    # 1. Rema 1000 manual token fallback
    resp_rema = client.post(
        "/api/integrations/rema1000/connect",
        json={"session_token": "manual-rema-token-999"},
    )
    assert resp_rema.status_code == 200
    assert resp_rema.json()["status"] == "connected"
    assert (
        store.get_credentials(household_id, "rema1000")["session_token"] == "manual-rema-token-999"
    )

    # 2. Coop manual token/cookie fallback
    resp_coop = client.post(
        "/api/integrations/coop/connect",
        json={"session_cookie": "manual-coop-cookie-888"},
    )
    assert resp_coop.status_code == 200
    assert store.get_credentials(household_id, "coop")["session_cookie"] == "manual-coop-cookie-888"

    # 3. Trumf manual cookie fallback
    resp_trumf = client.post(
        "/api/integrations/trumf/connect",
        json={"session_cookie": "manual-trumf-cookie-777"},
    )
    assert resp_trumf.status_code == 200
    assert (
        store.get_credentials(household_id, "trumf")["session_cookie"] == "manual-trumf-cookie-777"
    )

    # 4. Cookidoo manual cookie fallback
    resp_cookidoo = client.post(
        "/api/integrations/cookidoo/connect",
        json={"session_cookie": "manual-cookidoo-cookie-666"},
    )
    assert resp_cookidoo.status_code == 200
    assert (
        store.get_credentials(household_id, "cookidoo")["session_cookie"]
        == "manual-cookidoo-cookie-666"
    )


# =============================================================================
# 5. Secret Redaction across /api/integrations
# =============================================================================


def test_get_integrations_redacts_all_secrets_after_interactive_logins(sqlite_engine, cipher):
    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/token" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "access_token": "secret-access-token-111",
                    "refresh_token": "secret-refresh-token-222",
                    "expires_in": 3600,
                },
            )
        if "/otp/verify" in request.url.path:
            return httpx.Response(200, json={"session_cookie": "secret-trumf-cookie-333"})
        return httpx.Response(200, json={"status": "ok"})

    mock_transport = httpx.MockTransport(mock_handler)
    app, _, _, _ = create_test_app(sqlite_engine, cipher, mock_transport=mock_transport)
    client = TestClient(app)

    # Connect Rema via PKCE
    s_rema = client.post("/api/integrations/rema1000/pkce/start").json()["state"]
    client.post("/api/integrations/rema1000/pkce/complete", json={"code": "c1", "state": s_rema})

    # Connect Cookidoo via Login
    client.post(
        "/api/integrations/cookidoo/login",
        json={"email": "chef@test.no", "password": "secret-password-xyz"},
    )

    # Connect Trumf via OTP
    s_trumf = client.post(
        "/api/integrations/trumf/login/start", json={"phone": "+4712345678"}
    ).json()["session_id"]
    client.post(
        "/api/integrations/trumf/login/verify",
        json={"session_id": s_trumf, "otp": "123456"},
    )

    # Query list of all integrations
    list_resp = client.get("/api/integrations")
    assert list_resp.status_code == 200
    all_integrations = list_resp.json()

    # Verify all 3 are marked connected
    status_map = {item["id"]: item for item in all_integrations}
    assert status_map["rema1000"]["status"] == "connected"
    assert status_map["rema1000"]["auth_mode"] == "oauth_pkce"
    assert status_map["cookidoo"]["status"] == "connected"
    assert status_map["cookidoo"]["auth_mode"] == "oauth_password"
    assert status_map["trumf"]["status"] == "connected"
    assert status_map["trumf"]["auth_mode"] == "interactive_otp"

    # Redaction test on the whole JSON
    raw_json = json.dumps(all_integrations)
    assert "secret-access-token" not in raw_json
    assert "secret-refresh-token" not in raw_json
    assert "secret-password-xyz" not in raw_json
    assert "secret-trumf-cookie" not in raw_json
