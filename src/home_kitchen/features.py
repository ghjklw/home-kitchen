"""Application services compose feature ports, without depending on a transport."""

import json
from collections.abc import Callable
from datetime import date
from uuid import UUID, uuid4

from pydantic_ai import Agent

from home_kitchen.basket.jobs import BasketMatchJobs
from home_kitchen.basket.matching import BasketMatchingService, OdaSearchAdapter
from home_kitchen.catalogue.repository import PostgresFoodCatalogueRepository
from home_kitchen.catalogue.service import FoodCatalogueService
from home_kitchen.catalogue.sku_repository import PostgresCatalogueSkuRepository
from home_kitchen.catalogue.sku_service import CatalogueSkuService
from home_kitchen.context import history_context
from home_kitchen.history import HistoryRepository
from home_kitchen.integrations import (
    AIIntegration,
    CookidooIntegration,
    IntegrationRegistry,
    OdaIntegration,
)
from home_kitchen.inventory import InventoryService
from home_kitchen.models import RecipeCreate
from home_kitchen.oda import OdaService
from home_kitchen.oda.cart import OdaCartService
from home_kitchen.planning_proposals import PlanningProposalService
from home_kitchen.purchasing import InStoreMode, OdaMode, PurchaseModes, StoreTripService
from home_kitchen.purchasing_reconciliation import TripCompletionService
from home_kitchen.recipe_feedback import RecipeFeedbackService
from home_kitchen.recipes import (
    EvidenceSnippet,
    RecipeContext,
    RecipeDraft,
    RecipeDraftRequest,
    create_recipe_providers,
)
from home_kitchen.recipes.collections import CollectionService, OdaCollectionProvider
from home_kitchen.recipes.discovery import (
    MarmitonDiscoveryProvider,
    MatpratDiscoveryProvider,
    OdaDiscoveryProvider,
    RecipeDiscoveryRegistry,
)
from home_kitchen.recipes.images import RecipeImageService
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.recipes.photo_storage import PhotoRecipeService
from home_kitchen.recommendations import PlanDraft, build_planner, recommend_plan
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


class KitchenFeatures:
    def __init__(
        self,
        kitchen: KitchenService,
        settings: Settings,
        household: Callable[[], Household],
        ai: Agent[None, EditableRecipeDraft] | Agent[None, RecipeCreate] | None = None,
    ):
        self.kitchen = kitchen
        self.settings = settings
        self.household = household
        self.catalogue = FoodCatalogueService(PostgresFoodCatalogueRepository(kitchen.store.engine))
        self.household_id = settings.entra_allowed_group or UUID(
            kitchen.store.ensure_setting("local_household_id", str(uuid4()))
        )
        kitchen.store.photo_household_id = self.household_id
        self.catalogue_skus = CatalogueSkuService(
            self.catalogue, PostgresCatalogueSkuRepository(kitchen.store.engine)
        )
        self.history = HistoryRepository(kitchen.store.engine)
        self.recipe_feedback = RecipeFeedbackService(kitchen, self.history)
        self.oda = OdaService(kitchen.store.engine, settings)
        self.oda_cart = OdaCartService(self.oda)
        self.integrations = IntegrationRegistry(
            [OdaIntegration(self.oda), AIIntegration(settings), CookidooIntegration()]
        )
        self.inventory = InventoryService(kitchen, self.history)
        self.basket_matching = BasketMatchingService(self.inventory, OdaSearchAdapter(self.oda))
        self.basket_jobs = BasketMatchJobs(
            self.basket_matching, self.inventory, self.oda.search_cache_context
        )
        self.plan_proposals = PlanningProposalService(kitchen, self.history)
        self.store_trip = StoreTripService(self.inventory, self.history)
        self.trip_completion = TripCompletionService(self.inventory, self.history)
        self.purchase_modes = PurchaseModes([OdaMode(self.oda), InStoreMode()])
        self.recipe_providers = create_recipe_providers(settings, ai_agent=ai)
        self.recipe_photos = PhotoRecipeService(
            settings, self.household_id, kitchen.store.engine, household
        )
        self.recipe_images = RecipeImageService(kitchen)
        self.recipe_collections = CollectionService(
            kitchen, self.history, OdaCollectionProvider(self.oda)
        )
        self.recipe_discovery = RecipeDiscoveryRegistry(
            [
                OdaDiscoveryProvider(self.oda),
                MarmitonDiscoveryProvider(),
                MatpratDiscoveryProvider(),
            ]
        )

    def evidence(self, query: str) -> list[EvidenceSnippet]:
        # Explicit feedback/preferences and actual purchases remain relevant even
        # when their words do not appear in the current recipe request.
        recent = self.history.search(
            "", limit=8, kinds=["preference", "feedback", "recipe_rating", "purchase"]
        )
        matched = self.history.search(query[:1000], limit=8) if query.strip() else []
        entries = {entry.id: entry for entry in [*recent, *matched]}
        evidence = [
            EvidenceSnippet(
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
            for entry in list(entries.values())[:12]
        ]
        # Current counts complement historical feedback; they are explicit user
        # observations, never proof of freshness or automatic consumption.
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
        recipes = self.kitchen.list_recipes()
        if not recipes:
            raise ValueError("Save a recipe before requesting a meal plan")
        evidence = self.evidence("meal preferences feedback purchases")
        planner = build_planner(self.settings)
        result = await recommend_plan(
            planner,
            self.household(),
            recipes,
            start,
            days,
            evidence_context="\n".join(item.model_dump_json() for item in evidence),
        )
        return result.model_copy(update={"evidence_ids": [item.evidence_id for item in evidence]})
