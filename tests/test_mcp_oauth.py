import base64
import hashlib
import logging
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from fastmcp.server.auth.oauth_proxy import JTIMapping, UpstreamTokenSet
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select

from home_kitchen.app import create_app
from home_kitchen.context import history_context
from home_kitchen.mcp_auth import HouseholdAzureProvider
from home_kitchen.mcp_oauth_storage import oauth_records
from home_kitchen.settings import Settings

TENANT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GROUP = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
AUDIENCE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
BASE = "https://kitchen.example/mcp"


def oauth_settings(database_url, **changes):
    values = dict(
        database_url=database_url,
        entra_tenant_id=TENANT,
        entra_audience=AUDIENCE,
        entra_allowed_group=GROUP,
        mcp_oauth_enabled=True,
        mcp_public_url=BASE,
        mcp_client_secret="test-only-upstream-client-secret",
        mcp_signing_key="test-only-broker-signing-material-32-bytes",
        mcp_encryption_key=Fernet.generate_key().decode(),
    )
    values.update(changes)
    return Settings(**values)


def test_oauth_configuration_is_explicit_complete_and_canonical():
    url = "postgresql+psycopg://localhost/kitchen_test"
    assert not Settings(database_url=url, local_dev=True).mcp_oauth_enabled
    for changes in (
        {"mcp_oauth_enabled": False},
        {"mcp_client_secret": None},
        {"mcp_signing_key": "short"},
        {"mcp_encryption_key": "invalid"},
        {"mcp_public_url": "http://kitchen.example/mcp"},
        {"mcp_public_url": "https://user:password@kitchen.example/mcp"},
        {"mcp_public_url": "https://kitchen.example/mcp?redirect=evil"},
        {"mcp_public_url": "https://kitchen.example/arbitrary"},
        {"mcp_public_url": "https://kitchen.example:8443/mcp"},
        {"mcp_allowed_redirect_uris": []},
        {"mcp_allowed_redirect_uris": ["https://*.example/callback"]},
        {"mcp_allowed_redirect_uris": ["https://evil.example/*"]},
        {"local_dev": True},
        {"entra_audience": "api://wrong"},
    ):
        with pytest.raises(ValidationError):
            oauth_settings(url, **changes)


@pytest.fixture
def upstream_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def configure_offline_verifier(provider, private_key):
    public_pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    provider._token_validator = JWTVerifier(
        public_key=public_pem,
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithm="RS256",
        required_scopes=["Kitchen.Access"],
    )


async def broker_token(provider, private_key, **claim_changes):
    now = int(time.time())
    claims = dict(
        sub="household-member",
        tid=TENANT,
        aud=AUDIENCE,
        iss=ISSUER,
        iat=now - 1,
        exp=now + 300,
        groups=[GROUP],
        scp="Kitchen.Access",
    )
    claims.update(claim_changes)
    upstream = jwt.encode(claims, private_key, algorithm="RS256")
    reference = uuid4().hex
    await provider._upstream_token_store.put(
        reference,
        UpstreamTokenSet(
            upstream_token_id=reference,
            access_token=upstream,
            refresh_token=None,
            refresh_token_expires_at=None,
            expires_at=now + 300,
            token_type="Bearer",
            scope="Kitchen.Access",
            client_id="test-client",
            created_at=now,
        ),
        ttl=300,
    )
    await provider._jti_mapping_store.put(
        reference,
        JTIMapping(jti=reference, upstream_token_id=reference, created_at=now),
        ttl=300,
    )
    return provider.jwt_issuer.issue_access_token(
        client_id="test-client",
        scopes=["Kitchen.Access"],
        jti=reference,
    )


@pytest.mark.asyncio
async def test_broker_checks_real_signed_upstream_identity_on_every_use(
    test_database_url, upstream_key
):
    engine = create_engine(test_database_url)
    settings = oauth_settings(test_database_url)
    provider = HouseholdAzureProvider(settings, engine)
    provider.set_mcp_path("/")
    configure_offline_verifier(provider, upstream_key)
    try:
        accepted = await broker_token(provider, upstream_key)
        assert (await provider.verify_token(accepted)).claims["sub"] == "household-member"
        for changes in (
            {"tid": str(uuid4())},
            {"groups": []},
            {"groups": GROUP},
            {"groups": None, "_claim_names": {"groups": "src1"}},
            {"sub": ""},
            {"sub": None},
            {"iat": None},
            {"exp": None},
            {"iat": int(time.time()) + 300},
            {"nbf": int(time.time()) + 300},
            {"exp": int(time.time()) - 1},
            {"aud": str(uuid4())},
            {"iss": "https://wrong.example"},
            {"scp": "openid"},
        ):
            assert (
                await provider.verify_token(await broker_token(provider, upstream_key, **changes))
                is None
            )
        foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert await provider.verify_token(await broker_token(provider, foreign)) is None
        # Restarting the provider retains encrypted state and signing identity.
        restarted = HouseholdAzureProvider(settings, engine)
        restarted.set_mcp_path("/")
        configure_offline_verifier(restarted, upstream_key)
        assert await restarted.verify_token(accepted) is not None
        assert await provider.verify_token("not-a-token") is None
    finally:
        engine.dispose()


def test_discovery_challenge_and_dcr_redirects(test_database_url):
    app = create_app(oauth_settings(test_database_url))
    with TestClient(
        app, base_url="https://kitchen.example", client=("203.0.113.20", 5000)
    ) as client:
        metadata = client.get("/.well-known/oauth-authorization-server/mcp").json()
        resource = client.get("/.well-known/oauth-protected-resource/mcp/").json()
        assert resource["resource"] == BASE + "/"
        assert resource["authorization_servers"] == [metadata["issuer"]] == [BASE]
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
        assert metadata["authorization_endpoint"] == BASE + "/authorize"
        denied = client.get(
            "/mcp/",
            headers={
                "x-auth-request-user": "spoofed",
                "x-auth-request-groups": GROUP,
            },
        )
        assert denied.status_code == 401
        assert "resource_metadata=" in denied.headers["www-authenticate"]
        assert client.get("/api/recipes").status_code == 401
        assert client.get("/").status_code == 401
        assert client.get("/.well-known/not-an-oauth-route").status_code == 401
        registration = dict(
            client_name="Test",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
        )
        for redirect in (
            "https://evil.example/callback",
            "https://claude.ai.evil.example/api/mcp/auth_callback",
        ):
            response = client.post(
                "/mcp/register", json={**registration, "redirect_uris": [redirect]}
            )
            assert response.status_code == 400
        for redirect in (
            "https://claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com/connector/oauth/test-connection",
        ):
            response = client.post(
                "/mcp/register", json={**registration, "redirect_uris": [redirect]}
            )
            assert response.status_code == 201
            assert response.json()["token_endpoint_auth_method"] == "none"
            assert not response.json().get("client_secret")
        for method in ("private_key_jwt", "client_secret_jwt", "unsupported"):
            rejected = client.post(
                "/mcp/register",
                json={
                    **registration,
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                    "token_endpoint_auth_method": method,
                },
            )
            assert rejected.status_code == 400
        public = client.post(
            "/mcp/register",
            json={
                **registration,
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "token_endpoint_auth_method": "none",
                "require_pkce": False,
            },
        ).json()
        authorize = {
            "client_id": public["client_id"],
            "response_type": "code",
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "scope": "Kitchen.Access",
            "state": "pkce-check",
        }
        for relaxed in ({}, {"code_challenge": "plain-value", "code_challenge_method": "plain"}):
            rejected = client.get(
                "/mcp/authorize", params={**authorize, **relaxed}, follow_redirects=False
            )
            assert rejected.status_code in (400, 302)
            assert "/consent" not in rejected.headers.get("location", "")
            if rejected.status_code == 302:
                assert "error=" in rejected.headers["location"]


def test_public_registration_limit_fails_safely(test_database_url, monkeypatch):
    app = create_app(oauth_settings(test_database_url))
    monkeypatch.setattr(app.state.mcp.auth.storage, "MAX_CLIENTS", 0)
    with TestClient(app, base_url="https://kitchen.example") as client:
        response = client.post(
            "/mcp/register",
            json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client_metadata"
        assert "registry is full" in response.json()["error_description"]


@pytest.mark.parametrize("stream_body", [False, True])
def test_oauth_request_bodies_are_bounded_before_persistence(test_database_url, stream_body):
    app = create_app(oauth_settings(test_database_url))
    engine = app.state.service.store.engine
    with TestClient(app, base_url="https://kitchen.example") as client:
        registration = client.post(
            "/mcp/register",
            json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
        ).json()
        with engine.begin() as connection:
            before = connection.scalar(select(func.count()).select_from(oauth_records))
        payload = urlencode(
            {
                "client_id": registration["client_id"],
                "response_type": "code",
                "scope": "Kitchen.Access",
                "code_challenge": "test-challenge",
                "state": "x" * (65 * 1024),
            }
        ).encode()
        for path in ("/authorize", "/consent", "/token", "/register"):
            body = iter([payload[:1024], payload[1024:]]) if stream_body else payload
            response = client.post(
                "/mcp" + path,
                content=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://kitchen.example",
                },
                follow_redirects=False,
            )
            assert response.status_code == 413
            if path in ("/token", "/register"):
                assert response.headers["access-control-allow-origin"] == "*"
        with engine.begin() as connection:
            assert connection.scalar(select(func.count()).select_from(oauth_records)) == before


@pytest.mark.asyncio
async def test_broker_tool_call_has_verified_actor_and_mcp_channel(test_database_url, upstream_key):
    app = create_app(oauth_settings(test_database_url))
    provider = app.state.mcp.auth
    configure_offline_verifier(provider, upstream_key)
    token = await broker_token(provider, upstream_key)

    @app.state.mcp.tool()
    def current_actor() -> dict:
        context = history_context.get()
        return {"actor": context.actor_id, "channel": context.channel}

    with TestClient(
        app, base_url="https://kitchen.example", client=("203.0.113.20", 5000)
    ) as client:
        response = client.post(
            "/mcp/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "current_actor", "arguments": {}},
            },
        )
        assert response.status_code == 200
        assert '"actor":"household-member"' in response.text
        assert '"channel":"mcp"' in response.text
    assert history_context.get().actor_id is None


@pytest.mark.parametrize("failure", [None, "household", "network"])
@pytest.mark.parametrize("request_scope", [None, "Kitchen.Access"])
def test_browser_oauth_consent_pkce_callback_and_one_use_code(
    test_database_url, upstream_key, monkeypatch, caplog, failure, request_scope
):
    app = create_app(oauth_settings(test_database_url))
    provider = app.state.mcp.auth
    configure_offline_verifier(provider, upstream_key)
    exchanges = []
    closed = []
    allowed_groups = [GROUP] if failure != "household" else []
    caplog.set_level(logging.DEBUG, logger="fastmcp")

    class OfflineEntraClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def refresh_token(self, **kwargs):
            assert kwargs["refresh_token"] == "test-only-refresh-token"
            assert f"api://{AUDIENCE}/Kitchen.Access" in kwargs["scope"]
            return await self.fetch_token(**kwargs)

        async def fetch_token(self, **kwargs):
            if failure == "network":
                raise RuntimeError("test-only-upstream-secret-error")
            exchanges.append(kwargs)
            now = int(time.time())
            return {
                "access_token": jwt.encode(
                    {
                        "sub": "household-member",
                        "tid": TENANT,
                        "aud": AUDIENCE,
                        "iss": ISSUER,
                        "iat": now - 1,
                        "exp": now + 300,
                        "groups": allowed_groups,
                        "scp": "Kitchen.Access",
                    },
                    upstream_key,
                    algorithm="RS256",
                ),
                "expires_in": 300,
                "refresh_token": "test-only-refresh-token",
            }

    monkeypatch.setattr("home_kitchen.mcp_auth.AsyncOAuth2Client", OfflineEntraClient)
    with TestClient(app, base_url="https://kitchen.example") as client:
        registration = client.post(
            "/mcp/register",
            json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        ).json()
        assert registration["token_endpoint_auth_method"] == "none"
        assert not registration.get("client_secret")
        _, pending_ttl = client.portal.call(
            lambda: provider.storage.ttl(
                registration["client_id"], collection=provider.storage.CLIENT_COLLECTION
            )
        )
        assert 0 < pending_ttl <= 24 * 3600
        verifier = "test-only-client-verifier-value-with-at-least-43-characters"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        response = client.get(
            "/mcp/authorize",
            params={
                "client_id": registration["client_id"],
                "response_type": "code",
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                **({"scope": request_scope} if request_scope is not None else {}),
                "state": "client-state",
                "resource": BASE + "/",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        consent_url = response.headers["location"]
        txn_id = parse_qs(urlsplit(consent_url).query)["txn_id"][0]
        page = client.get(consent_url)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        assert (
            client.post(
                "/mcp/consent",
                data={"txn_id": txn_id, "action": "approve", "csrf_token": "wrong"},
                headers={"Origin": "https://kitchen.example"},
            ).status_code
            == 400
        )
        consent_form = {"txn_id": txn_id, "action": "approve", "csrf_token": csrf}
        # A form token fetched by another browser cannot authorize this browser.
        outsider = TestClient(app, base_url="https://kitchen.example")
        try:
            assert (
                outsider.post(
                    "/mcp/consent",
                    data=consent_form,
                    headers={"Origin": "https://kitchen.example"},
                    follow_redirects=False,
                ).status_code
                == 400
            )
        finally:
            outsider.close()
        for headers in (
            {},
            {"Origin": "https://attacker.example"},
            {
                "Origin": "https://kitchen.example",
                "Cookie": "__Host-MCP_CONSENT_STATE=tampered",
            },
        ):
            assert (
                client.post(
                    "/mcp/consent",
                    data=consent_form,
                    headers=headers,
                    follow_redirects=False,
                ).status_code
                == 400
            )
        approved = client.post(
            "/mcp/consent",
            data=consent_form,
            headers={"Origin": "https://kitchen.example"},
            follow_redirects=False,
        )
        assert approved.status_code == 302
        upstream = parse_qs(urlsplit(approved.headers["location"]).query)
        assert upstream["redirect_uri"] == [BASE + "/auth/callback"]
        assert set(upstream["scope"][0].split()) == {
            f"api://{AUDIENCE}/Kitchen.Access",
            "openid",
            "profile",
            "email",
            "offline_access",
        }
        assert upstream["code_challenge_method"] == ["S256"]
        assert upstream["code_challenge"] != [challenge]
        callback = client.get(
            "/mcp/auth/callback",
            params={"code": "test-upstream-code", "state": upstream["state"][0]},
            follow_redirects=False,
        )
        if failure:
            assert callback.status_code == (403 if failure == "household" else 400)
            assert "location" not in callback.headers
            assert "test-only-upstream-secret-error" not in callback.text
            assert len(closed) == 1
            assert all(
                "test-only-upstream-secret-error" not in record.getMessage()
                for record in caplog.records
            )
            return
        assert callback.status_code == 302
        returned = parse_qs(urlsplit(callback.headers["location"]).query)
        assert returned["state"] == ["client-state"]
        assert exchanges[0]["redirect_uri"] == BASE + "/auth/callback"
        assert exchanges[0]["code_verifier"] != verifier
        exchange = {
            "grant_type": "authorization_code",
            "client_id": registration["client_id"],
            "code": returned["code"][0],
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "resource": BASE + "/",
            "code_verifier": verifier,
        }
        assert (
            client.post("/mcp/token", data={**exchange, "code_verifier": "wrong"}).status_code
            == 400
        )
        tokens = client.post("/mcp/token", data=exchange)
        assert tokens.status_code == 200
        assert tokens.json()["access_token"]
        assert tokens.json()["refresh_token"]
        _, authenticated_ttl = client.portal.call(
            lambda: provider.storage.ttl(
                registration["client_id"], collection=provider.storage.CLIENT_COLLECTION
            )
        )
        assert authenticated_ttl > 29 * 24 * 3600

        def use_bearer(value):
            return client.post(
                "/mcp/",
                headers={
                    "Authorization": f"Bearer {value}",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )

        assert use_bearer(tokens.json()["access_token"]).status_code == 200
        assert use_bearer(tokens.json()["refresh_token"]).status_code == 401
        payload = provider.jwt_issuer.verify_token(tokens.json()["access_token"])
        expired = provider.jwt_issuer.issue_access_token(
            registration["client_id"], ["Kitchen.Access"], payload["jti"], expires_in=-1
        )
        assert use_bearer(expired).status_code == 401
        assert client.post("/mcp/token", data=exchange).status_code == 400
        refresh_request = {
            "grant_type": "refresh_token",
            "client_id": registration["client_id"],
            "refresh_token": tokens.json()["refresh_token"],
            "resource": BASE + "/",
        }
        refreshed = client.post("/mcp/token", data=refresh_request)
        assert refreshed.status_code == 200
        assert refreshed.json()["refresh_token"] != tokens.json()["refresh_token"]
        assert client.post("/mcp/token", data=refresh_request).status_code == 400
        assert use_bearer(tokens.json()["access_token"]).status_code == 200
        assert use_bearer(refreshed.json()["refresh_token"]).status_code == 401
        assert len(closed) == 2
        allowed_groups.clear()
        denied_refresh = client.post(
            "/mcp/token",
            data={
                **refresh_request,
                "refresh_token": refreshed.json()["refresh_token"],
            },
        )
        assert denied_refresh.status_code == 400
        assert "access_token" not in denied_refresh.json()
        assert len(closed) == 3
        auth_logs = "\n".join(
            record.getMessage() for record in caplog.records if record.name.startswith("fastmcp")
        )
        for secret in (
            "test-upstream-code",
            returned["code"][0],
            tokens.json()["access_token"],
            tokens.json()["refresh_token"],
        ):
            assert secret not in auth_logs
