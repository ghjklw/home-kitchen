"""AI workflow service implementing recipe drafting, weekly recommendations, and discovery."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from home_kitchen.ai_workflow.ports import AiWorkflowPort
from home_kitchen.catalogue.identity import IdentityCorrectionCitation, IdentityCorrectionEvent
from home_kitchen.context import PurchaseHistoryContext, history_context
from home_kitchen.recipes import (
    EvidenceSnippet,
    RecipeContext,
    RecipeDraft,
    RecipeDraftRequest,
)
from home_kitchen.recipes.discovery import (
    DiscoveryContext,
    RecipeDiscoveryRequest,
    RecipeDiscoveryResponse,
)
from home_kitchen.recommendations import PlanDraft, build_planner, recommend_plan


class AiWorkflowService(AiWorkflowPort):
    """Orchestrates AI drafting, recommendations, and discovery using explicit ports."""

    def __init__(
        self,
        *,
        kitchen: Any,
        settings: Any,
        household: Any,
        recipe_providers: Any,
        recipe_discovery: Any,
        history: Any,
        inventory: Any,
        catalogue_identities: Any,
        purchase_sync: Any,
    ) -> None:
        self.kitchen = kitchen
        self.settings = settings
        self.household = household
        self.recipe_providers = recipe_providers
        self.recipe_discovery = recipe_discovery
        self.history = history
        self.inventory = inventory
        self.catalogue_identities = catalogue_identities
        self.purchase_sync = purchase_sync
        self.household_id = settings.entra_allowed_group or getattr(kitchen, "household_id", None)

    @staticmethod
    def _history_snippet(entry: Any) -> EvidenceSnippet:
        return EvidenceSnippet(
            evidence_id=str(entry.id),
            text=json.dumps(
                {
                    "kind": entry.kind,
                    "message": entry.message[:1000],
                    "structured": json.dumps(entry.structured, ensure_ascii=False)[:1000],
                    "recorded_at": entry.occurred_at.isoformat(),
                },
                ensure_ascii=False,
            ),
        )

    def purchase_history_context(self, *, limit: int = 25) -> PurchaseHistoryContext:
        sync_svc = getattr(self, "purchase_sync", None)
        if sync_svc is None or getattr(sync_svc, "engine", None) is None:
            return PurchaseHistoryContext()
        get_ctx = getattr(sync_svc, "get_bought_before_context", None)
        if callable(get_ctx):
            return get_ctx(limit=limit)
        return PurchaseHistoryContext()

    def evidence(self, query: str) -> list[EvidenceSnippet]:
        if getattr(self.history, "engine", None) is None:
            return []
        recent = self.history.search(
            "", limit=8, kinds=["preference", "feedback", "recipe_rating", "purchase"]
        )
        matched = self.history.search(query[:1000], limit=8) if query.strip() else []
        entries = {entry.id: entry for entry in [*recent, *matched]}
        evidence = [self._history_snippet(entry) for entry in list(entries.values())[:12]]
        stock = [item for item in self.inventory.list() if item.quantity > 0]
        evidence.extend(
            EvidenceSnippet(
                evidence_id=f"inventory:{item.id}",
                text=json.dumps(
                    {
                        "kind": "current_inventory",
                        "name": item.name,
                        "quantity": str(item.quantity),
                        "unit": item.unit,
                        "category": item.category,
                    },
                    ensure_ascii=False,
                ),
            )
            for item in stock[:8]
        )
        bought_ctx = self.purchase_history_context(limit=10)
        for idx, bought in enumerate(bought_ctx.bought_before):
            ref = bought.evidence_refs[0] if bought.evidence_refs else str(idx)
            evidence.append(
                EvidenceSnippet(
                    evidence_id=f"purchase_history:{bought.provider}:{ref}:{idx}",
                    text=json.dumps(
                        {
                            "kind": "bought_before",
                            "provider": bought.provider,
                            "product_name": bought.product_name,
                            "canonical_product_id": bought.canonical_product_id,
                            "purchase_count": bought.purchase_count,
                            "last_purchased_at": bought.last_purchased_at.isoformat()
                            if bought.last_purchased_at
                            else None,
                            "last_quantity": bought.last_quantity,
                            "unit": bought.unit,
                            "store_name": bought.store_name,
                            "provenance": bought.provenance,
                            "boundary": bought_ctx.boundary_note,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return evidence

    async def draft_recipe(self, provider_id: str, data: RecipeDraftRequest) -> RecipeDraft:
        if provider_id == "ai":
            data.require_ai_source()
        evidence = self.evidence(data.prompt or "") if provider_id == "ai" else []
        return await self.recipe_providers.draft(
            provider_id,
            data,
            RecipeContext(
                household=self.household(),
                knowledge=evidence,
                actor_id=history_context.get().actor_id,
            ),
        )

    async def recommend_week(self, start: date, days: int = 7) -> PlanDraft:
        if not 1 <= days <= 14:
            raise ValueError("days must be between 1 and 14")
        recipes = self.kitchen.list_recipes()
        if not recipes:
            raise ValueError("Save a recipe before requesting a meal plan")
        existing_meals = self.kitchen.list_meals(start, start + timedelta(days=days - 1))
        evidence = self.evidence("meal preferences feedback purchases")
        purchase_ctx = self.purchase_history_context(limit=25)
        raw_corrections = self.catalogue_identities.list_correction_events(
            household_id=self.household_id,
            limit=20,
        )
        correction_citations = [
            IdentityCorrectionCitation.from_event(ev)
            for ev in (raw_corrections if isinstance(raw_corrections, list) else [])
            if isinstance(ev, IdentityCorrectionEvent)
        ]
        planner = build_planner(self.settings)
        result = await recommend_plan(
            planner,
            self.household(),
            recipes,
            start,
            days,
            evidence_context="\n".join(item.model_dump_json() for item in evidence),
            existing_meals=existing_meals,
            identity_corrections=correction_citations,
            purchase_history_context=purchase_ctx,
        )
        return result.model_copy(
            update={
                "evidence_ids": [item.evidence_id for item in evidence],
                "identity_correction_citations": correction_citations,
            }
        )

    def discovery_context(self, query: str = "") -> DiscoveryContext:
        evidence = list(self.evidence(query or "recipe feedback preferences rating"))
        if getattr(self.history, "engine", None) is not None:
            recent_feedback = self.history.search(
                "", limit=8, kinds=["preference", "feedback", "recipe_rating"]
            )
            seen_ids = {item.evidence_id for item in evidence}
            for entry in recent_feedback:
                entry_id = str(entry.id)
                if entry_id not in seen_ids:
                    seen_ids.add(entry_id)
                    evidence.append(self._history_snippet(entry))
        from home_kitchen.recipes.discovery import build_discovery_context

        return build_discovery_context(self.household(), evidence)

    async def discover_recipes(
        self, request: RecipeDiscoveryRequest, *, actor_id: str | None = None
    ) -> RecipeDiscoveryResponse:
        return await self.recipe_discovery.search(request, actor_id=actor_id)
