"""Feature routes use application services also exposed through MCP."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from home_kitchen.basket import BasketReview, SelectedProduct
from home_kitchen.basket.matching import BasketMatchResult
from home_kitchen.basket.repository import BasketService
from home_kitchen.features import KitchenFeatures
from home_kitchen.history import HistoryEntry, HistoryEntryCreate, RetrievedEvidence
from home_kitchen.inventory import InventoryItem, InventoryItemCreate, PurchasePlan
from home_kitchen.purchasing import PurchaseModeInfo, StoreTrip
from home_kitchen.recipe_feedback import RecipeFeedbackCreate
from home_kitchen.recipes import RecipeDraft, RecipeDraftRequest, RecipeProviderInfo
from home_kitchen.recipes.discovery import RecipeDiscoveryRequest, RecipeDiscoveryResponse


class BasketSelectionRequest(BaseModel):
    start: date
    end: date
    selection: SelectedProduct


class TripCheck(BaseModel):
    checked: bool


def feature_router(features: KitchenFeatures, basket: BasketService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/inventory")
    def inventory() -> list[InventoryItem]:
        return features.inventory.list()

    @router.put("/inventory")
    def set_inventory(data: InventoryItemCreate) -> InventoryItem:
        return features.inventory.upsert(data)

    @router.delete("/inventory/{item_id}", status_code=204)
    def remove_inventory(item_id: UUID) -> Response:
        features.inventory.remove(item_id)
        return Response(status_code=204)

    @router.get("/purchase-plan")
    def purchase_plan(start: date, end: date) -> PurchasePlan:
        return features.inventory.plan(start, end)

    @router.get("/purchase-modes")
    def purchase_modes() -> list[PurchaseModeInfo]:
        return features.purchase_modes.catalog()

    @router.get("/store-trip")
    def store_trip(start: date, end: date) -> StoreTrip:
        return features.store_trip.review(start, end)

    @router.put("/store-trip/items/{need_id}")
    def check_store_item(need_id: UUID, data: TripCheck, start: date, end: date) -> StoreTrip:
        return features.store_trip.check(start, end, need_id, data.checked)

    @router.post("/recipe-discovery")
    async def discover_recipes(
        request: Request, data: RecipeDiscoveryRequest, response: Response
    ) -> RecipeDiscoveryResponse:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_discovery.search(
            data, actor_id=getattr(request.state, "actor_id", None)
        )

    @router.get("/recipe-providers")
    def recipe_providers() -> list[RecipeProviderInfo]:
        return features.recipe_providers.catalog()

    @router.post("/recipe-drafts/{provider_id}")
    async def recipe_draft(provider_id: str, data: RecipeDraftRequest) -> RecipeDraft:
        try:
            return await features.draft_recipe(provider_id, data)
        except ValueError:
            raise
        except Exception:
            if provider_id == "ai":
                raise HTTPException(502, "AI draft unavailable; no recipe has been saved") from None
            raise

    @router.post("/recipes/{recipe_id}/feedback", status_code=201)
    def record_recipe_feedback(
        recipe_id: UUID, data: RecipeFeedbackCreate, request: Request
    ) -> HistoryEntry:
        return features.recipe_feedback.record(
            recipe_id,
            data,
            channel="app" if request.headers.get("sec-fetch-site") == "same-origin" else "api",
            actor_id=getattr(request.state, "actor_id", None),
        )

    @router.post("/interactions", status_code=201)
    def record_interaction(data: HistoryEntryCreate, request: Request) -> HistoryEntry:
        # The caller explicitly chooses what to preserve. No external chat is
        # collected automatically, and actor identity cannot be supplied in JSON.
        return features.history.record(
            data.model_copy(
                update={
                    "channel": "app"
                    if request.headers.get("sec-fetch-site") == "same-origin"
                    else "api"
                }
            ),
            actor_id=getattr(request.state, "actor_id", None),
        )

    @router.get("/interactions")
    def history(limit: int = Query(default=30, ge=1, le=100)) -> list[HistoryEntry]:
        return features.history.list(limit)

    @router.get("/knowledge/search")
    def search_knowledge(
        query: str = Query(default="", max_length=1000), limit: int = Query(default=8, ge=1, le=20)
    ) -> list[RetrievedEvidence]:
        return features.history.search(query, limit)

    @router.get("/basket")
    def review_basket(start: date, end: date) -> BasketReview:
        return basket.review(start, end)

    @router.get("/basket/matches")
    async def find_basket_products(
        request: Request,
        response: Response,
        start: date,
        end: date,
        page: int = Query(default=1, ge=1, le=50),
        size: int = Query(default=5, ge=1, le=5),
    ) -> BasketMatchResult:
        actor_id = getattr(request.state, "actor_id", None)
        if not actor_id:
            raise HTTPException(401, "Household authentication required")
        response.headers["Cache-Control"] = "no-store"
        return await features.basket_matching.find_products(
            actor_id, start, end, page=page, size=size
        )

    @router.put("/basket/selections")
    def select_product(data: BasketSelectionRequest) -> BasketReview:
        return basket.select(data.start, data.end, data.selection)

    @router.delete("/basket/selections/{need_id}/{product_id}")
    def remove_selection(need_id: UUID, product_id: str, start: date, end: date) -> BasketReview:
        return basket.remove(start, end, need_id, product_id)

    return router
