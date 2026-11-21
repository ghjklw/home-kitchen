"""Application services compose feature ports, without depending on a transport."""

import json
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic_ai import Agent

from home_kitchen.basket.jobs import BasketMatchJobs
from home_kitchen.basket.matching import BasketMatchingService, OdaSearchAdapter
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
from home_kitchen.purchase_sync import ProviderAdapter, PurchaseHistorySyncService
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
    DiscoveryContext,
    MarmitonDiscoveryProvider,
    MatpratDiscoveryProvider,
    OdaDiscoveryProvider,
    RecipeDiscoveryRegistry,
    RecipeDiscoveryRequest,
    RecipeDiscoveryResponse,
    build_discovery_context,
)
from home_kitchen.recipes.images import RecipeImageService
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.recipes.photo_storage import PhotoRecipeService
from home_kitchen.recommendations import PlanDraft, build_planner, recommend_plan
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings
from home_kitchen.units import canonicalize_quantity


def _build_purchase_adapter(
    provider: str, account_id: str, creds: dict[str, Any]
) -> ProviderAdapter | None:
    if provider == "trumf":
        cookie = creds.get("session_cookie") or creds.get("cookie")
        if not isinstance(cookie, str) or not cookie.strip():
            return None
        from home_kitchen.trumf_history import TrumfHttpSource, TrumfPurchaseHistoryAdapter

        return TrumfPurchaseHistoryAdapter(TrumfHttpSource(cookie), account_id=account_id)
    if provider == "coop":
        token = creds.get("access_token") or creds.get("session_token")
        cookie = creds.get("session_cookie") or creds.get("cookie")
        token_str = token.strip() if isinstance(token, str) and token.strip() else None
        cookie_str = cookie.strip() if isinstance(cookie, str) and cookie.strip() else None
        if not token_str and not cookie_str:
            return None
        from home_kitchen.coop_history import CoopHttpSource, CoopPurchaseHistoryAdapter

        return CoopPurchaseHistoryAdapter(
            CoopHttpSource(access_token=token_str, session_cookie=cookie_str),
            account_id=account_id,
        )
    if provider == "rema1000":
        token = creds.get("access_token") or creds.get("session_token")
        if not isinstance(token, str) or not token.strip():
            return None
        from home_kitchen.rema1000_history import (
            Rema1000HttpSource,
            Rema1000PurchaseHistoryAdapter,
        )

        return Rema1000PurchaseHistoryAdapter(Rema1000HttpSource(token), account_id=account_id)
    return None


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

        def _sku_preferences(product_id: UUID) -> tuple[list[str], list[str]]:
            try:
                pref, exc = self.product_overview.get_preferences(product_id)
                return ([p.oda_product_id for p in pref], [e.oda_product_id for e in exc])
            except KeyError:
                return ([], [])

        def _resolve_product_id(name: str) -> UUID | None:
            for loc in ("nb", "en", "fr"):
                for ctx in ("recipe", "receipt", ""):
                    res = self.catalogue_identities.resolve("product", loc, ctx, name)
                    if res.status == "resolved" and len(res.candidates) == 1:
                        return res.candidates[0].id
            return None

        self.basket_matching = BasketMatchingService(
            self.inventory,
            OdaSearchAdapter(self.oda),
            preferences_provider=_sku_preferences,
            product_id_resolver=_resolve_product_id,
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
        )
        self.purchase_sync.identity_service = kitchen.identities
        self.purchase_sync.mapping_store = kitchen.store
        self.recipe_providers = create_recipe_providers(settings, ai_agent=ai)
        self.recipe_photos = PhotoRecipeService(
            settings, self.household_id, kitchen.store.engine, household
        )
        self.recipe_images = RecipeImageService(kitchen)
        cookidoo_provider = CookidooCollectionProvider(self.connector_store, self.household_id)
        self.recipe_collections = CollectionService(
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
        self.recipe_discovery = RecipeDiscoveryRegistry(
            [
                OdaDiscoveryProvider(self.oda),
                MarmitonDiscoveryProvider(),
                MatpratDiscoveryProvider(),
            ],
            context_provider=self.discovery_context,
        )

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

    def evidence(self, query: str) -> list[EvidenceSnippet]:
        # Explicit feedback/preferences and actual purchases remain relevant even
        # when their words do not appear in the current recipe request.
        recent = self.history.search(
            "", limit=8, kinds=["preference", "feedback", "recipe_rating", "purchase"]
        )
        matched = self.history.search(query[:1000], limit=8) if query.strip() else []
        entries = {entry.id: entry for entry in [*recent, *matched]}
        evidence = [self._history_snippet(entry) for entry in list(entries.values())[:12]]
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
        if not 1 <= days <= 14:
            raise ValueError("days must be between 1 and 14")
        recipes = self.kitchen.list_recipes()
        if not recipes:
            raise ValueError("Save a recipe before requesting a meal plan")
        existing_meals = self.kitchen.list_meals(start, start + timedelta(days=days - 1))
        evidence = self.evidence("meal preferences feedback purchases")
        planner = build_planner(self.settings)
        result = await recommend_plan(
            planner,
            self.household(),
            recipes,
            start,
            days,
            evidence_context="\n".join(item.model_dump_json() for item in evidence),
            existing_meals=existing_meals,
        )
        return result.model_copy(update={"evidence_ids": [item.evidence_id for item in evidence]})

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
        """One-way export of unchecked remaining StoreTrip items to Home Assistant."""
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
        """Preview active Home Assistant To-Do items not already in Kitchen, without mutating Kitchen."""
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
        """Explicitly import active Home Assistant To-Do items as additive Kitchen quick captures.

        Without reviewed=True, returns a preview without mutating Kitchen. With reviewed=True,
        creates additive quick captures without overwriting Kitchen's canonical store-trip or checked state.
        """
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
        return build_discovery_context(self.household(), evidence)

    async def discover_recipes(
        self, request: RecipeDiscoveryRequest, *, actor_id: str | None = None
    ) -> RecipeDiscoveryResponse:
        return await self.recipe_discovery.search(request, actor_id=actor_id)
