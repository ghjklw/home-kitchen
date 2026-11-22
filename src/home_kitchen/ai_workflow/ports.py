"""Protocol ports for AI workflow, recommendations, and evidence gathering."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from home_kitchen.context import PurchaseHistoryContext
from home_kitchen.recipes import EvidenceSnippet, RecipeDraft, RecipeDraftRequest
from home_kitchen.recipes.discovery import (
    DiscoveryContext,
    RecipeDiscoveryRequest,
    RecipeDiscoveryResponse,
)
from home_kitchen.recommendations import PlanDraft
from home_kitchen.week_chat import (
    InventoryReaderPort,
    PlanningHistoryPort,
    RecipeStatsReaderPort,
    WeekChatApplyRequest,
    WeekChatApplyResponse,
    WeekChatRequest,
    WeekChatResponse,
    WeekPlanningBackendPort,
)

__all__ = [
    "AiWorkflowPort",
    "EvidenceProviderPort",
    "InventoryReaderPort",
    "PlanningHistoryPort",
    "RecipeStatsReaderPort",
    "WeekPlanningBackendPort",
]


class EvidenceProviderPort(Protocol):
    """Port for gathering evidence snippets from history and inventory."""

    def evidence(self, query: str) -> list[EvidenceSnippet]: ...

    def purchase_history_context(self, *, limit: int = 25) -> PurchaseHistoryContext: ...


class AiWorkflowPort(Protocol):
    """Port for AI recipe drafting, weekly recommendations, chat planning, and discovery."""

    async def draft_recipe(self, provider_id: str, data: RecipeDraftRequest) -> RecipeDraft: ...

    async def recommend_week(self, start: date, days: int = 7) -> PlanDraft: ...

    async def chat_week(
        self, request: WeekChatRequest, *, actor_id: str | None = None
    ) -> WeekChatResponse: ...

    def apply_week_chat_changes(
        self, request: WeekChatApplyRequest, *, actor_id: str | None = None
    ) -> WeekChatApplyResponse: ...

    def discovery_context(self, query: str = "") -> DiscoveryContext: ...

    async def discover_recipes(
        self, request: RecipeDiscoveryRequest, *, actor_id: str | None = None
    ) -> RecipeDiscoveryResponse: ...
