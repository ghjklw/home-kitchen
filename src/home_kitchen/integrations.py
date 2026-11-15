"""Central, secret-free descriptions of independently implemented connections."""

from typing import Literal, Protocol

from pydantic import BaseModel

from home_kitchen.oda.service import OdaService
from home_kitchen.settings import Settings


class IntegrationInfo(BaseModel):
    id: str
    name: str
    status: Literal["connected", "configured", "not_configured", "unavailable"]
    auth_mode: str | None = None
    supported_auth_modes: list[str]
    capabilities: list[str]
    detail: str
    management_path: str = "/#settings"


class IntegrationProvider(Protocol):
    def describe(self, actor_id: str | None) -> IntegrationInfo: ...


class IntegrationRegistry:
    def __init__(self, providers: list[IntegrationProvider]):
        self.providers = providers

    def describe(self, actor_id: str | None) -> list[IntegrationInfo]:
        return [provider.describe(actor_id) for provider in self.providers]


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


class CookidooIntegration:
    def describe(self, actor_id: str | None) -> IntegrationInfo:
        return IntegrationInfo(
            id="cookidoo",
            name="Cookidoo / Thermomix TM6",
            status="unavailable",
            supported_auth_modes=[],
            capabilities=[],
            detail="No official supported third-party connection path is available, so Cookidoo and TM6 sync are unavailable.",
        )
