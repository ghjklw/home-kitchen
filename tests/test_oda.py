"""Protocol mocks: no Oda login, vault, live basket, or PostgreSQL is needed."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from home_kitchen.oda import OdaError, OdaService
from home_kitchen.oda.repository import connections, metadata, states
from home_kitchen.oda.service import (
    AS_METADATA,
    AUTHORIZE,
    ISSUER,
    PRM,
    REGISTER,
    RESOURCE,
    REVOKE,
    TOKEN,
)
from home_kitchen.oda_api import oda_router
from home_kitchen.settings import Settings

pytestmark = pytest.mark.mocked_provider


@pytest.fixture
def oda():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    metadata.create_all(engine)
    settings = Settings(
        local_dev=True,
        public_url="https://kitchen.example",
        oda_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    calls = []

    def transport(request):
        calls.append(request)
        if str(request.url) == PRM:
            return httpx.Response(
                200, json={"resource": RESOURCE, "authorization_servers": [ISSUER]}
            )
        if str(request.url) == AS_METADATA:
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE,
                    "token_endpoint": TOKEN,
                    "registration_endpoint": REGISTER,
                    "revocation_endpoint": REVOKE,
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                },
            )
        if str(request.url) == REGISTER:
            return httpx.Response(
                201,
                json={
                    "client_id": "registered-private-client",
                    "token_endpoint_auth_method": "none",
                },
            )
        if str(request.url) == TOKEN:
            body = parse_qs(request.content.decode())
            assert body["resource"] == [RESOURCE]
            if body["grant_type"] == ["refresh_token"]:
                assert body["refresh_token"] == ["refresh-private"]
                return httpx.Response(
                    200,
                    json={
                        "access_token": "renewed-private",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            assert body["code"] == ["authorization-private"]
            assert len(body["code_verifier"][0]) >= 43
            return httpx.Response(
                200,
                json={
                    "access_token": "access-private",
                    "refresh_token": "refresh-private",
                    "token_type": "Bearer",
                    "scope": "mcp",
                    "expires_in": 3600,
                },
            )
        if str(request.url) == REVOKE:
            return httpx.Response(200)
        raise AssertionError("Unexpected OAuth endpoint")

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        service = OdaService(engine, settings, http_client=client)
        yield service, calls, engine
    engine.dispose()


def begin(service, actor="alice"):
    url = service.connect(actor)
    query = parse_qs(urlparse(url).query)
    assert url.startswith(AUTHORIZE)
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [RESOURCE]
    return query["state"][0]


def connected(service):
    state = begin(service)
    service.callback("alice", state, "authorization-private")


def test_registration_only_on_explicit_connect_and_actor_state_replay(oda):
    service, calls, engine = oda
    assert service.status("alice") == {
        "configured": True,
        "connected": False,
        "detail": "Not connected",
    }
    assert calls == []
    state = begin(service)
    with pytest.raises(OdaError, match="Invalid or expired"):
        service.callback("bob", state, "authorization-private")
    service.callback("alice", state, "authorization-private")
    with pytest.raises(OdaError, match="Invalid or expired"):
        service.callback("alice", state, "authorization-private")
    assert service.status("alice")["connected"] is True
    assert service.status("bob")["connected"] is False
    assert len([call for call in calls if str(call.url) == TOKEN]) == 1
    with engine.connect() as connection:
        assert connection.execute(select(states)).all() == []


def test_expired_and_superseded_state_rejected(oda):
    service, _, engine = oda
    old = begin(service)
    current = begin(service)
    with pytest.raises(OdaError):
        service.callback("alice", old, "authorization-private")
    with engine.begin() as connection:
        connection.execute(
            states.update().values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(OdaError):
        service.callback("alice", current, "authorization-private")


def test_encrypted_persistence_refresh_rotation_and_explicit_disconnect(oda):
    service, calls, engine = oda
    state = begin(service)
    with engine.connect() as connection:
        stored = str(connection.execute(select(states)).all())
        assert "registered-private-client" not in stored and state not in stored
    service.callback("alice", state, "authorization-private")
    with engine.connect() as connection:
        raw = str(connection.execute(select(connections)).all())
    assert "access-private" not in raw and "refresh-private" not in raw
    assert "registered-private-client" not in raw
    with service.repository.locked_connection("alice") as (connection, token):
        token["expires_at"] = 0
        service.repository.update_connection(connection, "alice", token)
    assert service.access_token("alice") == "renewed-private"
    assert service.access_token("alice") == "renewed-private"
    refreshes = [
        call
        for call in calls
        if str(call.url) == TOKEN and b"grant_type=refresh_token" in call.content
    ]
    assert len(refreshes) == 1
    service.disconnect("alice")
    assert not service.status("alice")["connected"]
    assert len([call for call in calls if str(call.url) == REVOKE]) == 2


def test_unconfigured_and_settings_reject_unsafe_urls(oda):
    service, _, engine = oda
    unconfigured = OdaService(engine, Settings(local_dev=True), http_client=service.http)
    assert unconfigured.status("alice")["configured"] is False
    with pytest.raises(OdaError, match="not configured"):
        unconfigured.connect("alice")
    for url in (
        "http://kitchen.example",
        "https://user:pass@kitchen.example",
        "https://kitchen.example/path",
    ):
        with pytest.raises(ValidationError):
            Settings(local_dev=True, public_url=url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_local_http_callback_uses_the_configured_loopback_origin(oda, host):
    service, calls, _ = oda
    service.settings = Settings(local_dev=True, public_url=f"http://{host}:8000")
    state = begin(service)
    expected = f"http://{host}:8000/api/oda/callback"
    registration = next(call for call in calls if str(call.url) == REGISTER)
    assert json.loads(registration.content)["redirect_uris"] == [expected]
    assert service.redirect_uri == expected
    service.callback("alice", state, "authorization-private")
    token_request = next(call for call in calls if str(call.url) == TOKEN)
    assert parse_qs(token_request.content.decode())["redirect_uri"] == [expected]


@pytest.mark.parametrize(
    "url",
    [
        "http://kitchen.example:8000",
        "http://localhost.example:8000",
        "http://0.0.0.0:8000",
        "http://127.0.0.2:8000",
        "http://127.1:8000",
        "http://2130706433:8000",
        "http://[::]:8000",
        "http://localhost:0",
        "http://localhost:65536",
        "http://user:pass@localhost:8000",
        "http://localhost:8000/path",
        "http://localhost:8000?query=value",
        "http://localhost:8000#fragment",
    ],
)
def test_local_http_public_url_rejects_non_loopback_and_non_origin_urls(url):
    with pytest.raises(ValidationError):
        Settings(local_dev=True, public_url=url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_http_loopback_public_url_requires_local_dev(host):
    with pytest.raises(ValidationError, match="public_url"):
        Settings(
            local_dev=False,
            public_url=f"http://{host}:8000",
            entra_tenant_id="11111111-1111-1111-1111-111111111111",
            entra_audience="kitchen",
            entra_allowed_group="22222222-2222-2222-2222-222222222222",
        )


def test_rest_does_not_expose_oauth_secrets_and_requires_actor(oda):
    service, _, _ = oda
    app = FastAPI()
    app.include_router(oda_router(service))
    with TestClient(app) as client:
        assert client.get("/api/oda/status").status_code == 401
    app = FastAPI()
    app.include_router(oda_router(service))

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    with TestClient(app) as client:
        response = client.post("/api/oda/connect")
        assert set(response.json()) == {"authorization_url"}
        state = parse_qs(urlparse(response.json()["authorization_url"]).query)["state"][0]
        callback = client.get(
            "/api/oda/callback",
            params={"state": state, "code": "authorization-private"},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["referrer-policy"] == "no-referrer"
        failed = client.get(
            "/api/oda/callback",
            params={"state": "bad", "code": "sensitive-code" * 500},
            follow_redirects=False,
        )
        assert failed.status_code == 303
        assert failed.headers["location"] == "/?oda=failed"
        assert failed.headers["cache-control"] == "no-store"
        assert "sensitive-code" not in failed.text
        payload = client.get("/api/oda/status").text
        assert "access-private" not in payload and "refresh-private" not in payload
        assert client.delete("/api/oda/connection").status_code == 204


@pytest.mark.asyncio
async def test_mcp_readonly_allowlist_and_official_search_schema(oda):
    service, _, _ = oda
    connected(service)
    with pytest.raises(OdaError, match="not supported"):
        await service._read("alice", "add_to_cart", {"product_id": 1})
    tool = SimpleNamespace(
        name="product_search",
        annotations=SimpleNamespace(readOnlyHint=True),
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "required": ["queries"],
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 10}
            },
        },
    )
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=[tool]),
        call_tool=AsyncMock(return_value=SimpleNamespace(structured_content={"result": []})),
    )
    context = AsyncMock()
    context.__aenter__.return_value = client
    with patch("home_kitchen.oda.service.Client", return_value=context):
        assert await service.search_products("alice", [" milk "], size=3) == {"result": []}
        client.call_tool.assert_awaited_once_with(
            "product_search", {"queries": ["milk"], "page": 1, "size": 3}, raise_on_error=True
        )
        client.call_tool.return_value = SimpleNamespace(
            structured_content=None,
            content=[
                SimpleNamespace(
                    type="text",
                    text='{"result":[{"query":"milk","hasMore":false,"products":[{"id":123,"name":"Milk","description":"Whole milk, 1 L","price":"29.90","unitPrice":"29.90","unitName":"per L","url":"https://oda.com/no/products/123/","image":"unsupported"}]}]}',
                )
            ],
        )
        displayed = await service.search_products("alice", ["milk"])
        product = displayed["result"][0]["products"][0]
        assert product["description"] == "Whole milk, 1 L"
        assert product["unitName"] == "per L"
        assert "image" not in product and "fulfillment_quantity" not in product
        tool.annotations.readOnlyHint = False
        with pytest.raises(OdaError, match="read-only"):
            await service.search_products("alice", ["milk"])
        assert client.call_tool.await_count == 2


def test_failed_token_exchange_is_single_use_and_safe(oda):
    service, _, _ = oda
    state = begin(service)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"error_description": "sensitive-private-upstream"}
            )
        )
    ) as client:
        service.http = client
        with pytest.raises(OdaError) as caught:
            service.callback("alice", state, "authorization-private")
        assert "sensitive-private-upstream" not in str(caught.value)
        with pytest.raises(OdaError, match="Invalid or expired"):
            service.callback("alice", state, "authorization-private")


def test_disconnect_fences_callback_during_token_exchange(oda, monkeypatch):
    """Exact race: state consumed, Disconnect commits, then token HTTP returns."""
    service, calls, engine = oda
    state = begin(service)
    original_request = service.http.request
    intercepted = False

    def request(method, url, **kwargs):
        nonlocal intercepted
        if (
            str(url) == TOKEN
            and kwargs.get("data", {}).get("grant_type") == "authorization_code"
            and not intercepted
        ):
            intercepted = True
            # Callback's one-time state is already consumed at this boundary.
            with engine.connect() as connection:
                assert connection.execute(select(states)).all() == []
            service.disconnect("alice")
            assert not service.status("alice")["connected"]
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(service.http, "request", request)
    with pytest.raises(OdaError, match="cancelled"):
        service.callback("alice", state, "authorization-private")
    assert intercepted
    assert not service.status("alice")["connected"]
    with engine.connect() as connection:
        assert connection.execute(select(connections)).all() == []
        assert connection.execute(select(states)).all() == []
    revocations = [call for call in calls if str(call.url) == REVOKE]
    assert len(revocations) == 2
    assert {parse_qs(call.content.decode())["token"][0] for call in revocations} == {
        "access-private",
        "refresh-private",
    }


def test_disconnect_fences_connect_during_dynamic_registration(oda, monkeypatch):
    """DCR was started before Disconnect and must not save pending state later."""
    service, _, engine = oda
    original_request = service.http.request
    intercepted = False

    def request(method, url, **kwargs):
        nonlocal intercepted
        if str(url) == REGISTER and not intercepted:
            intercepted = True
            service.disconnect("alice")
        return original_request(method, url, **kwargs)

    monkeypatch.setattr(service.http, "request", request)
    with pytest.raises(OdaError, match="cancelled"):
        service.connect("alice")
    assert intercepted
    assert not service.status("alice")["connected"]
    with engine.connect() as connection:
        assert connection.execute(select(states)).all() == []
    # A subsequent explicit Connect is a new lifecycle, and remains usable.
    next_state = begin(service)
    service.callback("alice", next_state, "authorization-private")
    assert service.status("alice")["connected"]
