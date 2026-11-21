"""Application services compose feature ports, without depending on a transport."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic_ai import Agent

from home_kitchen.ai_workflow.service import AiWorkflowService
from home_kitchen.basket.adapters import (
    CanonicalIdentityCorrectionAdapter,
    CanonicalIdentityLookupAdapter,
    ProductOverviewSkuPreferenceAdapter,
)
from home_kitchen.basket.jobs import BasketMatchJobs
from home_kitchen.basket.matching import BasketMatchingService, OdaSearchAdapter
from home_kitchen.bootstrap import _build_purchase_adapter
from home_kitchen.catalogue.adapters import CatalogueReceiptMappingAdapter
from home_kitchen.catalogue.overview_repository import (
    MemoryProductOverviewRepository,
    PostgresProductOverviewRepository,
)
from home_kitchen.catalogue.overview_service import ProductOverviewService
from home_kitchen.catalogue.repository import PostgresFoodCatalogueRepository
from home_kitchen.catalogue.service import FoodCatalogueService
from home_kitchen.catalogue.sku_repository import PostgresCatalogueSkuRepository
from home_kitchen.catalogue.sku_service import CatalogueSkuService
from home_kitchen.connector_auth import ConnectorAuthService
from home_kitchen.connector_store import ConnectorStore, resolve_connector_cipher
from home_kitchen.context import history_context
from home_kitchen.cookidoo_plan_sync import CookidooPlanSyncService
from home_kitchen.cookidoo_recipes import CookidooCollectionProvider
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.home_assistant import (
    DEFAULT_HOME_ASSISTANT_TODO_ENTITY,
    HOME_ASSISTANT_NOT_CONFIGURED_ERROR,
    HomeAssistantNotConfiguredError,
    HomeAssistantQuickCaptureImportResult,
    HomeAssistantTodoClient,
    HomeAssistantTodoExportResult,
    format_store_trip_export_item,
)
from home_kitchen.integrations import (
    AIIntegration,
    CookidooIntegration,
    CoopIntegration,
    HomeAssistantIntegration,
    IntegrationRegistry,
    KassalIntegration,
    OdaIntegration,
    Rema1000Integration,
    TrumfIntegration,
)
from home_kitchen.inventory import InventoryService
from home_kitchen.models import RecipeCreate, ShoppingItem, ShoppingItemCreate
from home_kitchen.oda import OdaService
from home_kitchen.oda.cart import OdaCartService
from home_kitchen.planning import create_planning_proposal_service
from home_kitchen.purchase_sync import PurchaseHistorySyncService
from home_kitchen.purchasing import InStoreMode, OdaMode, PurchaseModes, StoreTripService
from home_kitchen.purchasing_reconciliation import TripCompletionService
from home_kitchen.recipe_feedback import RecipeFeedbackService
from home_kitchen.recipes import (
    EvidenceSnippet,
    RecipeDraft,
    RecipeDraftRequest,
    create_recipe_providers,
)
from home_kitchen.recipes.adapters.oda import OdaCollectionProvider
from home_kitchen.recipes.bootstrap import (
    create_collection_service,
    create_recipe_discovery_registry,
    create_recipe_image_service,
)
from home_kitchen.recipes.discovery import (
    DiscoveryContext,
    RecipeDiscoveryRequest,
    RecipeDiscoveryResponse,
)
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.recipes.photo_storage import PhotoRecipeService
from home_kitchen.recommendations import PlanDraft
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings
from home_kitchen.units import canonicalize_quantity


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
        self.catalogue_identities = kitchen.identities
        self.household_id = settings.entra_allowed_group or UUID(
            kitchen.store.ensure_setting("local_household_id", str(uuid4()))
        )
        if kitchen.household_id is None:
            kitchen.household_id = self.household_id
        if kitchen.settings is None:
            kitchen.settings = settings
        kitchen.store.photo_household_id = self.household_id
        self.catalogue_skus = CatalogueSkuService(
            self.catalogue, PostgresCatalogueSkuRepository(kitchen.store.engine)
        )
        self.history = HistoryRepository(kitchen.store.engine)
        self.recipe_feedback = RecipeFeedbackService(kitchen, self.history)
        self.oda = OdaService(kitchen.store.engine, settings)
        self.oda_cart = OdaCartService(self.oda)
        self.connector_store = ConnectorStore(
            kitchen.store.engine, resolve_connector_cipher(settings)
        )
        self.integrations = IntegrationRegistry(
            [
                OdaIntegration(self.oda),
                AIIntegration(settings),
                Rema1000Integration(self.connector_store, self.household_id),
                CoopIntegration(self.connector_store, self.household_id),
                TrumfIntegration(self.connector_store, self.household_id),
                CookidooIntegration(self.connector_store, self.household_id),
                KassalIntegration(self.connector_store, self.household_id, settings=settings),
                HomeAssistantIntegration(
                    self.connector_store, self.household_id, settings=settings
                ),
            ]
        )
        self.home_assistant_transport: httpx.BaseTransport | None = None
        self.home_assistant_client: HomeAssistantTodoClient | None = None
        self.connector_auth = ConnectorAuthService(
            store=self.connector_store,
            integrations=self.integrations,
            household_id=self.household_id,
            settings=settings,
        )
        self.inventory = InventoryService(kitchen, self.history)
        if kitchen.store.engine is not None:
            self.product_overview_repo = PostgresProductOverviewRepository(kitchen.store.engine)
        else:
            self.product_overview_repo = MemoryProductOverviewRepository()
        self.product_overview = ProductOverviewService(
            repository=self.product_overview_repo,
            identities=self.catalogue_identities,
            inventory=self.inventory,
            history=self.history,
            catalogue_skus=self.catalogue_skus,
            household_id=self.household_id,
        )

        self.basket_matching = BasketMatchingService(
            self.inventory,
            OdaSearchAdapter(self.oda),
            sku_preferences=ProductOverviewSkuPreferenceAdapter(self.product_overview),
            identity_lookup=CanonicalIdentityLookupAdapter(
                self.catalogue_identities, household_id=self.household_id
            ),
            identity_corrections=CanonicalIdentityCorrectionAdapter(
                self.catalogue_identities, household_id=self.household_id
            ),
        )
        self.basket_jobs = BasketMatchJobs(
            self.basket_matching, self.inventory, self.oda.search_cache_context
        )
        self.plan_proposals = create_planning_proposal_service(kitchen, self.history)
        self.store_trip = StoreTripService(self.inventory, self.history)
        self.trip_completion = TripCompletionService(self.inventory, self.history)
        self.purchase_modes = PurchaseModes([OdaMode(self.oda), InStoreMode()])
        self.purchase_sync = PurchaseHistorySyncService(
            engine=kitchen.store.engine,
            settings=settings,
            oda_service=self.oda,
            connector_store=self.connector_store,
            household_id=self.household_id,
            adapter_factory=_build_purchase_adapter,
            connector_auth=self.connector_auth,
            identity_service=self.catalogue_identities,
            receipt_mapper=CatalogueReceiptMappingAdapter(
                kitchen.store, self.catalogue_identities
            ),
        )
        self.recipe_providers = create_recipe_providers(settings, ai_agent=ai)
        self.recipe_photos = PhotoRecipeService(
            settings, self.household_id, kitchen.store.engine, household
        )
        self.recipe_images = create_recipe_image_service(kitchen, self.history)
        cookidoo_provider = CookidooCollectionProvider(self.connector_store, self.household_id)
        self.recipe_collections = create_collection_service(
            kitchen,
            self.history,
            providers={
                "oda": OdaCollectionProvider(self.oda),
                "cookidoo": cookidoo_provider,
            },
        )
        self.cookidoo_plan_sync = CookidooPlanSyncService(
            kitchen,
            self.history,
            self.connector_store,
            self.household_id,
            provider=cookidoo_provider,
        )
        self.recipe_discovery = create_recipe_discovery_registry(
            self.oda,
            context_provider=self.discovery_context,
        )
        self.ai_workflow = AiWorkflowService(
            kitchen=kitchen,
            settings=settings,
            household=household,
            recipe_providers=self.recipe_providers,
            recipe_discovery=self.recipe_discovery,
            history=self.history,
            inventory=self.inventory,
            catalogue_identities=self.catalogue_identities,
            purchase_sync=self.purchase_sync,
        )

    def purchase_history_context(self, *, limit: int = 25) -> Any:
        return self.ai_workflow.purchase_history_context(limit=limit)

    def evidence(self, query: str) -> list[EvidenceSnippet]:
        return self.ai_workflow.evidence(query)

    async def draft_recipe(self, provider_id: str, data: RecipeDraftRequest) -> RecipeDraft:
        return await self.ai_workflow.draft_recipe(provider_id, data)

    async def recommend_week(self, start: date, days: int = 7) -> PlanDraft:
        return await self.ai_workflow.recommend_week(start, days)

    def _resolve_home_assistant_client(self) -> HomeAssistantTodoClient:
        if self.home_assistant_client is not None:
            return self.home_assistant_client

        creds: dict[str, Any] | None = None
        try:
            creds = self.connector_store.get_credentials(self.household_id, "home_assistant")
        except RuntimeError as exc:
            raise HomeAssistantNotConfiguredError(str(exc)) from exc

        token = ""
        base_url = ""
        entity_id = ""
        if isinstance(creds, dict):
            raw_token = creds.get("api_key") or creds.get("token")
            if isinstance(raw_token, str):
                token = raw_token.strip()
            raw_url = creds.get("base_url")
            if isinstance(raw_url, str):
                base_url = raw_url.strip().rstrip("/")
            raw_entity = creds.get("entity_id")
            if isinstance(raw_entity, str):
                entity_id = raw_entity.strip()

        if not token and self.settings.home_assistant_token is not None:
            token = self.settings.home_assistant_token.get_secret_value().strip()
        if not base_url and self.settings.home_assistant_url is not None:
            base_url = str(self.settings.home_assistant_url).strip().rstrip("/")
        if not entity_id and self.settings.home_assistant_todo_entity is not None:
            entity_id = self.settings.home_assistant_todo_entity.strip()
        if not entity_id:
            entity_id = DEFAULT_HOME_ASSISTANT_TODO_ENTITY

        if not base_url or not token:
            raise HomeAssistantNotConfiguredError(HOME_ASSISTANT_NOT_CONFIGURED_ERROR)

        return HomeAssistantTodoClient(
            base_url=base_url,
            token=token,
            entity_id=entity_id,
            transport=self.home_assistant_transport,
        )

    def export_store_trip_to_home_assistant(
        self, start: date, end: date, *, reviewed: bool = False
    ) -> HomeAssistantTodoExportResult:
        if not reviewed:
            raise ValueError(
                "Explicit confirmation (reviewed=True) is required to export to Home Assistant"
            )
        if end < start:
            raise ValueError("end date must be on or after start date")

        client = self._resolve_home_assistant_client()
        trip = self.store_trip.review(start, end)
        items: list[str] = []
        for line in trip.lines:
            if line.checked or line.to_buy_quantity <= 0:
                continue
            remaining = line.to_buy_quantity - line.checked_quantity
            if remaining <= 0:
                continue
            items.append(format_store_trip_export_item(line.name, remaining, line.unit))

        result = client.export_remaining_items(items)
        return HomeAssistantTodoExportResult.model_validate(result)

    def _collect_existing_kitchen_item_names(
        self, *, start: date | None = None, end: date | None = None
    ) -> list[str]:
        if start is not None or end is not None:
            effective_start = start if start is not None else end
            effective_end = end if end is not None else start
            assert effective_start is not None and effective_end is not None
            if effective_end < effective_start:
                raise ValueError("end date must be on or after start date")
        else:
            effective_start = date.today()
            effective_end = effective_start + timedelta(days=6)

        existing: list[str] = []
        for capture in self.kitchen.list_items():
            if capture.completed:
                continue
            existing.append(capture.name)
            existing.append(
                format_store_trip_export_item(capture.name, capture.quantity, capture.unit)
            )
            canon_qty, canon_unit = canonicalize_quantity(capture.quantity, capture.unit)
            existing.append(format_store_trip_export_item(capture.name, canon_qty, canon_unit))

        shopping = self.kitchen.shopping_list(effective_start, effective_end, today=date.today())
        for line in shopping.lines:
            existing.append(line.name)
            existing.append(format_store_trip_export_item(line.name, line.quantity, line.unit))
            canon_qty, canon_unit = canonicalize_quantity(line.quantity, line.unit)
            existing.append(format_store_trip_export_item(line.name, canon_qty, canon_unit))
        for unquantified in shopping.unquantified:
            existing.append(unquantified.name)

        if getattr(self.store_trip, "engine", None) is not None:
            trip = self.store_trip.review(effective_start, effective_end)
            for trip_line in trip.lines:
                existing.append(trip_line.name)
                existing.append(
                    format_store_trip_export_item(
                        trip_line.name, trip_line.to_buy_quantity, trip_line.unit
                    )
                )
                existing.append(
                    format_store_trip_export_item(
                        trip_line.name, trip_line.required_quantity, trip_line.unit
                    )
                )
                remaining = trip_line.to_buy_quantity - trip_line.checked_quantity
                if remaining > 0:
                    existing.append(
                        format_store_trip_export_item(trip_line.name, remaining, trip_line.unit)
                    )

        return existing

    def preview_home_assistant_quick_captures(
        self, *, start: date | None = None, end: date | None = None
    ) -> HomeAssistantQuickCaptureImportResult:
        existing = self._collect_existing_kitchen_item_names(start=start, end=end)
        client = self._resolve_home_assistant_client()
        result = client.preview_quick_captures(existing)
        return HomeAssistantQuickCaptureImportResult.model_validate(result)

    def import_home_assistant_quick_captures(
        self,
        *,
        reviewed: bool = False,
        start: date | None = None,
        end: date | None = None,
    ) -> HomeAssistantQuickCaptureImportResult:
        existing = self._collect_existing_kitchen_item_names(start=start, end=end)
        client = self._resolve_home_assistant_client()
        result = client.import_quick_captures(existing, reviewed=reviewed)
        if not reviewed:
            return HomeAssistantQuickCaptureImportResult.model_validate(result)

        created_items: list[ShoppingItem] = []
        added: list[str] = []
        for candidate in result["candidates"]:
            created = self.kitchen.add_item(ShoppingItemCreate(name=candidate))
            created_items.append(created)
            added.append(created.name)

        if created_items and getattr(self.history, "engine", None) is not None:
            self.history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="domain_event",
                    message=(f"Imported {len(created_items)} quick capture(s) from Home Assistant"),
                    structured={
                        "action": "home_assistant_quick_capture_import",
                        "entity_id": result["entity_id"],
                        "added": added,
                        "skipped_existing": result["skipped_existing"],
                        "capture_ids": [str(item.id) for item in created_items],
                    },
                ),
                actor_id=history_context.get().actor_id,
            )

        return HomeAssistantQuickCaptureImportResult(
            reviewed=True,
            candidates=result["candidates"],
            added=added,
            created_items=created_items,
            skipped_existing=result["skipped_existing"],
            total_active_in_ha=result["total_active_in_ha"],
            entity_id=result["entity_id"],
        )

    def discovery_context(self, query: str = "") -> DiscoveryContext:
        return self.ai_workflow.discovery_context(query)

    async def discover_recipes(
        self, request: RecipeDiscoveryRequest, *, actor_id: str | None = None
    ) -> RecipeDiscoveryResponse:
        return await self.ai_workflow.discover_recipes(request, actor_id=actor_id)
