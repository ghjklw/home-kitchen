"""Central, secret-free descriptions of independently implemented connections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from home_kitchen.oda.service import OdaService
from home_kitchen.settings import Settings

if TYPE_CHECKING:
    from home_kitchen.connector_store import ConnectorStore


class IntegrationInfo(BaseModel):
    id: str
    name: str
    status: Literal["connected", "configured", "not_configured", "unavailable", "expired"]
    auth_mode: str | None = None
    supported_auth_modes: list[str]
    capabilities: list[str]
    detail: str
    management_path: str = "/#settings"
    expires_at: datetime | None = None
    expiry_prompt: str | None = None


class IntegrationProvider(Protocol):
    def describe(self, actor_id: str | None) -> IntegrationInfo: ...


class IntegrationRegistry:
    def __init__(self, providers: list[IntegrationProvider]):
        self.providers = providers

    def describe(self, actor_id: str | None) -> list[IntegrationInfo]:
        return [provider.describe(actor_id) for provider in self.providers]

    def describe_one(self, provider_id: str, actor_id: str | None) -> IntegrationInfo:
        for provider in self.providers:
            info = provider.describe(actor_id)
            if info.id == provider_id:
                return info
        raise KeyError(f"Integration provider '{provider_id}' not found")


class OdaIntegration:
    def __init__(self, oda: OdaService):
        self.oda = oda

    def describe(self, actor_id: str | None) -> IntegrationInfo:
        state = self.oda.status(actor_id or "")
        return IntegrationInfo(
            id="oda",
            name="Oda",
            status="connected"
            if state["connected"]
            else "configured"
            if state["configured"]
            else "not_configured",
            auth_mode="delegated_user_pkce" if state["connected"] else None,
            supported_auth_modes=["delegated_user_pkce"],
            capabilities=[
                "product_search",
                "recipe_collections",
                "cart_review",
                "confirmed_cart_additions",
            ],
            detail="Personal recipes and shopping require your Oda consent. Checkout and payment stay at Oda.",
        )


class AIIntegration:
    def __init__(self, settings: Settings):
        self.settings = settings

    def describe(self, actor_id: str | None) -> IntegrationInfo:
        mode = (
            "federation"
            if self.settings.anthropic_workload_identity
            else "api_key"
            if self.settings.anthropic_api_key
            else None
        )
        return IntegrationInfo(
            id="anthropic",
            name="Recipe assistant",
            status="configured" if self.settings.ai_configured else "not_configured",
            auth_mode=mode,
            supported_auth_modes=["federation", "api_key"],
            capabilities=["source_adaptation", "photo_transcription", "meal_recommendations"],
            detail="Configured by the operator. Federation is preferred where supported; configuration does not prove a successful model call.",
        )


class StoredConnectorIntegration:
    def __init__(
        self,
        provider_id: str,
        name: str,
        supported_auth_modes: list[str],
        capabilities: list[str],
        detail: str,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
        default_expiry_prompt: str | None = None,
    ):
        self.provider_id = provider_id
        self.name = name
        self.supported_auth_modes = supported_auth_modes
        self.capabilities = capabilities
        self.detail = detail
        self.store = store
        self.household_id = household_id
        self.default_expiry_prompt = default_expiry_prompt

    def describe(self, actor_id: str | None) -> IntegrationInfo:
        status: Literal["connected", "configured", "not_configured", "unavailable", "expired"] = (
            "not_configured"
        )
        auth_mode: str | None = None
        expires_at: datetime | None = None
        expiry_prompt: str | None = None

        if self.store is not None and self.household_id is not None:
            conn_status = self.store.get_connection_metadata(self.household_id, self.provider_id)
            if conn_status is not None:
                auth_mode = conn_status.auth_mode
                expires_at = conn_status.expires_at
                if conn_status.is_expired:
                    status = "expired"
                    expiry_prompt = (
                        self.default_expiry_prompt
                        or f"Sesjonen for {self.name} har utløpt. Koble til på nytt."
                    )
                else:
                    status = "connected"
                    if expires_at is not None:
                        diff = expires_at - datetime.now(UTC)
                        diff_days = diff.total_seconds() / 86400.0
                        if 0 <= diff_days <= 7:
                            days = max(1, int(diff_days) + 1)
                            expiry_prompt = (
                                f"Sesjonen utløper om {days} {'dag' if days == 1 else 'dager'}."
                            )

        return IntegrationInfo(
            id=self.provider_id,
            name=self.name,
            status=status,
            auth_mode=auth_mode,
            supported_auth_modes=self.supported_auth_modes,
            capabilities=self.capabilities,
            detail=self.detail,
            expires_at=expires_at,
            expiry_prompt=expiry_prompt,
        )


class Rema1000Integration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="rema1000",
            name="Rema 1000",
            supported_auth_modes=["oauth_pkce", "session_token"],
            capabilities=["purchase_history", "receipt_sync"],
            detail="Koble til Rema 1000 Æ med sesjonstoken for automatisk synkronisering av kjøpshistorikk og rabatter.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Sesjonen for Rema 1000 har utløpt. Oppdater sesjonstokenet for å fortsette synkronisering.",
        )


class CoopIntegration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="coop",
            name="Coop",
            supported_auth_modes=["oauth_pkce", "session_token", "session_cookie"],
            capabilities=["purchase_history", "receipt_sync"],
            detail="Koble til Coop med sesjonstoken eller informasjonskapsel for automatisk kvitterings- og kjøpshistorikk.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Sesjonen for Coop har utløpt. Koble til på nytt med et gyldig token eller informasjonskapsel.",
        )


class TrumfIntegration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="trumf",
            name="Trumf",
            supported_auth_modes=["interactive_otp", "session_cookie"],
            capabilities=["purchase_history", "receipt_sync"],
            detail="Koble til Trumf med sesjonsinformasjonskapsel for automatisk kvitterings- og kjøpshistorikk.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Sesjonen for Trumf har utløpt. Oppdater informasjonskapselen for å fortsette synkronisering.",
        )


class CookidooIntegration(StoredConnectorIntegration):
    def __init__(
        self,
        store: ConnectorStore | None = None,
        household_id: UUID | None = None,
    ):
        super().__init__(
            provider_id="cookidoo",
            name="Cookidoo / Thermomix TM6",
            supported_auth_modes=["oauth_password", "session_cookie", "credentials"],
            capabilities=["recipe_import", "meal_planning"],
            detail="Uoffisiell tilkobling til Cookidoo / TM6 for oppskriftsimport og ukeplansynkronisering.",
            store=store,
            household_id=household_id,
            default_expiry_prompt="Tilkoblingen til Cookidoo har utløpt. Koble til på nytt for å synkronisere oppskrifter.",
        )
