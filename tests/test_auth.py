from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from home_kitchen.app import create_app
from home_kitchen.auth import AccessControl
from home_kitchen.settings import Settings

TENANT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
GROUP = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
AUDIENCE = "api://kitchen-test"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


@pytest.fixture
def signing_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def production_settings(path, **overrides):
    return Settings(
        database_url=path,
        entra_tenant_id=TENANT,
        entra_audience=AUDIENCE,
        entra_allowed_group=GROUP,
        **overrides,
    )


def token(private_key, **overrides):
    now = datetime.now(UTC)
    claims = {
        "sub": "local-test-user",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "aud": AUDIENCE,
        "iss": ISSUER,
        "tid": str(TENANT),
        "scp": "Kitchen.Access profile",
        "groups": [str(GROUP)],
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "local-test-key"})


def test_jwt_accepts_only_expected_tenant_audience_issuer_expiry_group_and_scope(
    test_database_url, signing_keys
):
    private_key, public_key = signing_keys
    access = AccessControl(production_settings(test_database_url))
    access.jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
    )

    assert access.verify(token(private_key))
    denied_claims = [
        {"tid": str(UUID(int=1))},
        {"aud": "api://wrong"},
        {"iss": "https://issuer.invalid"},
        {"exp": datetime.now(UTC) - timedelta(minutes=1)},
        {"groups": [str(UUID(int=2))]},
        {"scp": "profile"},
    ]
    for claims in denied_claims:
        assert access.verify(token(private_key, **claims)) is False


def test_production_denies_unauthenticated_static_api_and_mcp(test_database_url):
    app = create_app(production_settings(test_database_url))
    with TestClient(app, client=("203.0.113.20", 5000)) as client:
        for path in ("/", "/static/app.js", "/api/recipes", "/mcp/"):
            response = client.get(path)
            assert response.status_code == 401, path


def test_untrusted_proxy_headers_cannot_authenticate(test_database_url):
    app = create_app(production_settings(test_database_url, trusted_proxy_cidrs=["127.0.0.1/32"]))
    headers = {
        "x-auth-request-user": "spoofed@example.test",
        "x-auth-request-groups": str(GROUP),
        "x-forwarded-for": "127.0.0.1",
    }
    with TestClient(app, client=("203.0.113.20", 5000)) as client:
        assert client.get("/api/recipes", headers=headers).status_code == 401


def test_trusted_proxy_reads_but_mutations_need_same_origin_and_mcp_needs_bearer(test_database_url):
    app = create_app(production_settings(test_database_url, trusted_proxy_cidrs=["127.0.0.1/32"]))
    headers = {
        "x-auth-request-user": "member@example.test",
        "x-auth-request-groups": str(GROUP),
    }
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/recipes", headers=headers).status_code == 200
        assert (
            client.put("/api/household", headers=headers, json={"name": "Changed"}).status_code
            == 401
        )
        same_origin = {**headers, "origin": "https://testserver"}
        assert (
            client.put("/api/household", headers=same_origin, json={"name": "Changed"}).status_code
            == 200
        )
        assert client.get("/mcp/", headers=headers).status_code == 401


def test_local_dev_still_requires_loopback_peer(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    with TestClient(app, client=("203.0.113.20", 5000)) as client:
        assert client.get("/api/recipes").status_code == 401
        assert client.get("/api/recipes", headers={"host": "localhost"}).status_code == 401
    loopback_app = create_app(Settings(local_dev=True, database_url=test_database_url))
    with TestClient(loopback_app, client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/recipes").status_code == 200
