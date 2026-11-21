import json
from types import SimpleNamespace
from uuid import UUID

import httpx2
import pytest
from anthropic import AsyncAnthropic, WorkloadIdentityCredentials
from azure.core.credentials import AccessToken
from pydantic import ValidationError

from home_kitchen import ai_credentials
from home_kitchen.ai import recipe_agent
from home_kitchen.ai_credentials import CLAUDE_ENTRA_SCOPE, anthropic_model, close_ai_clients
from home_kitchen.recommendations import build_planner
from home_kitchen.settings import Settings

pytestmark = pytest.mark.mocked_provider


@pytest.fixture
def wif_values():
    return {
        "local_dev": True,
        "ai_model": "claude-test",
        "anthropic_workload_identity": True,
        "anthropic_federation_rule_id": "fdr_test",
        "anthropic_organization_id": UUID(int=1),
        "anthropic_service_account_id": "svac_test",
        "anthropic_workspace_id": "wrkspc_test",
        "azure_tenant_id": UUID(int=2),
        "azure_client_id": UUID(int=3),
        "azure_federated_token_file": "/projected/azure-token",
    }


@pytest.mark.parametrize(
    "missing",
    [
        "anthropic_federation_rule_id",
        "anthropic_organization_id",
        "anthropic_service_account_id",
        "azure_tenant_id",
        "azure_client_id",
        "azure_federated_token_file",
    ],
)
def test_workload_identity_requires_complete_explicit_configuration(wif_values, missing):
    with pytest.raises(ValidationError, match="Workload identity requires"):
        Settings(**{**wif_values, missing: None})


def test_credential_modes_fail_closed(wif_values, monkeypatch):
    with pytest.raises(ValidationError, match="either Anthropic"):
        Settings(**wif_values, anthropic_api_key="test-key")
    with pytest.raises(ValidationError, match="explicitly"):
        Settings(**{**wif_values, "anthropic_workload_identity": False})
    with pytest.raises(ValidationError, match="absolute"):
        Settings(**{**wif_values, "azure_federated_token_file": "relative-token"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-must-not-be-used")
    settings = Settings(local_dev=True, ai_model="claude-test")
    assert settings.ai_configured is False
    with pytest.raises(ValueError, match="Configure"):
        anthropic_model(settings)
    with pytest.raises(ValidationError, match="blank"):
        Settings(local_dev=True, anthropic_api_key=" ")


@pytest.mark.asyncio
async def test_api_key_mode_shared_factory_and_cleanup(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unexpected.example")
    settings = Settings(
        local_dev=True, ai_model="claude-test", anthropic_api_key="explicit-test-key"
    )
    assert settings.ai_configured
    recipe = recipe_agent(settings)
    planner = build_planner(settings)
    recipe_client = recipe.model.provider.client
    assert planner.model.provider.client is recipe_client
    assert recipe_client.api_key == "explicit-test-key"
    assert recipe_client.auth_token is None
    assert recipe_client.credentials is None
    assert str(recipe_client.base_url).rstrip("/") == "https://api.anthropic.com"
    await close_ai_clients(settings)
    assert recipe_client.is_closed()
    assert settings._ai_runtime is None
    await close_ai_clients(settings)


@pytest.mark.asyncio
async def test_wif_exchanges_entra_token_refreshes_and_closes(wif_values, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-must-not-be-used")
    calls = []
    azure = SimpleNamespace(close=lambda: calls.append(("close",)))

    def get_token(scope):
        calls.append(("get_token", scope))
        return AccessToken(f"entra-access-token-{len(calls)}", 9999999999)

    azure.get_token = get_token

    def azure_factory(**kwargs):
        calls.append(("azure_init", kwargs))
        return azure

    monkeypatch.setattr(ai_credentials, "WorkloadIdentityCredential", azure_factory)
    exchanges = []

    def exchange(request):
        body = json.loads(request.content)
        exchanges.append(body)
        assert body["assertion"].startswith("entra-access-token-")
        assert body["federation_rule_id"] == "fdr_test"
        assert body["organization_id"] == str(UUID(int=1))
        assert body["service_account_id"] == "svac_test"
        assert body["workspace_id"] == "wrkspc_test"
        return httpx2.Response(
            200,
            json={
                "access_token": f"anthropic-access-token-{len(exchanges)}",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    headers = []

    def messages(request):
        headers.append(request.headers)
        assert "x-api-key" not in request.headers
        if len(headers) == 1:
            return httpx2.Response(
                401,
                json={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "Synthetic expired token",
                    },
                },
            )
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "test"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(exchange)) as exchange_http:
        original_init = WorkloadIdentityCredentials.__init__

        def credentials_init(self, **kwargs):
            original_init(self, **kwargs, http_client=exchange_http)

        monkeypatch.setattr(WorkloadIdentityCredentials, "__init__", credentials_init)
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(messages))
        monkeypatch.setattr(
            ai_credentials,
            "AsyncAnthropic",
            lambda **kwargs: AsyncAnthropic(
                **kwargs,
                http_client=http,
                max_retries=1,
            ),
        )
        settings = Settings(**wif_values)
        model = anthropic_model(settings)
        client = model.provider.client
        await client.messages.create(
            model="claude-test", max_tokens=10, messages=[{"role": "user", "content": "test"}]
        )
        assert len(exchanges) == 2
        assert [call for call in calls if call[0] == "get_token"] == [
            ("get_token", CLAUDE_ENTRA_SCOPE),
            ("get_token", CLAUDE_ENTRA_SCOPE),
        ]
        assert calls[0] == (
            "azure_init",
            {
                "tenant_id": str(UUID(int=2)),
                "client_id": str(UUID(int=3)),
                "token_file_path": "/projected/azure-token",
            },
        )
        assert headers[0]["authorization"] == "Bearer anthropic-access-token-1"
        assert headers[1]["authorization"] == "Bearer anthropic-access-token-2"
        await close_ai_clients(settings)
        assert calls[-1] == ("close",)
        assert http.is_closed


@pytest.mark.parametrize("header", ["X-Api-Key: ambient", "authorization: Bearer ambient"])
def test_ambient_authentication_headers_cannot_override_wif(wif_values, monkeypatch, header):
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", header)
    with pytest.raises(ValueError, match="Kitchen settings only"):
        anthropic_model(Settings(**wif_values))
