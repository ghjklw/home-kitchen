"""Explicit Anthropic authentication; Azure federation never falls back to host credentials."""

import os
from dataclasses import dataclass

from anthropic import AsyncAnthropic, WorkloadIdentityCredentials
from azure.identity import WorkloadIdentityCredential
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from home_kitchen.settings import Settings

CLAUDE_ENTRA_SCOPE = "api://claude-api-federation/.default"


class AzureAnthropicCredentials(WorkloadIdentityCredentials):
    """Anthropic refreshes its access token by requesting a fresh Entra assertion.

    The Anthropic SDK invokes synchronous credential callbacks in a worker thread
    for async requests. Azure's credential caches Entra tokens and rereads the
    rotating projected token when it needs to exchange again.
    """

    def __init__(self, settings: Settings):
        assert settings.azure_tenant_id and settings.azure_client_id
        assert settings.azure_federated_token_file
        assert settings.anthropic_federation_rule_id and settings.anthropic_organization_id
        assert settings.anthropic_service_account_id
        self.azure_credential = WorkloadIdentityCredential(
            tenant_id=str(settings.azure_tenant_id),
            client_id=str(settings.azure_client_id),
            token_file_path=str(settings.azure_federated_token_file),
        )
        super().__init__(
            identity_token_provider=self.entra_token,
            federation_rule_id=settings.anthropic_federation_rule_id,
            organization_id=str(settings.anthropic_organization_id),
            service_account_id=settings.anthropic_service_account_id,
            workspace_id=settings.anthropic_workspace_id,
        )

    def entra_token(self) -> str:
        return self.azure_credential.get_token(CLAUDE_ENTRA_SCOPE).token

    def close(self) -> None:
        try:
            self.azure_credential.close()
        finally:
            super().close()


@dataclass
class AIClientRuntime:
    client: AsyncAnthropic


def anthropic_model(settings: Settings) -> AnthropicModel:
    if not settings.ai_configured or not settings.ai_model:
        raise ValueError("Configure an AI model and API credential to use AI")
    runtime = settings._ai_runtime
    if not isinstance(runtime, AIClientRuntime):
        # The SDK also accepts arbitrary environment headers, which otherwise
        # could override even an explicitly selected authentication mode.
        ambient_headers = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
        if any(
            line.partition(":")[0].strip().casefold() in {"authorization", "x-api-key"}
            for line in ambient_headers.splitlines()
        ):
            raise ValueError("Configure Anthropic authentication through Kitchen settings only")
        credentials = (
            AzureAnthropicCredentials(settings) if settings.anthropic_workload_identity else None
        )
        try:
            # Explicit credentials suppress SDK API-key/profile auto-discovery.
            # Pin the endpoint so ambient SDK configuration cannot redirect assertions.
            client = AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value()
                if settings.anthropic_api_key
                else None,
                credentials=credentials,
                base_url="https://api.anthropic.com",
            )
        except Exception:
            if credentials is not None:
                credentials.close()
            raise
        runtime = AIClientRuntime(client)
        settings._ai_runtime = runtime
    return AnthropicModel(
        settings.ai_model, provider=AnthropicProvider(anthropic_client=runtime.client)
    )


async def close_ai_clients(settings: Settings) -> None:
    runtime = settings._ai_runtime
    settings._ai_runtime = None
    if isinstance(runtime, AIClientRuntime):
        await runtime.client.close()
