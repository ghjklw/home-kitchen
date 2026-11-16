"""Feature routes use application services also exposed through MCP."""

import base64
import binascii
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from home_kitchen.basket import BasketReview, ProductSnapshot, SelectedProduct
from home_kitchen.basket.jobs import BasketMatchJob
from home_kitchen.basket.matching import BasketMatchResult
from home_kitchen.basket.repository import BasketService
from home_kitchen.catalogue.identity import (
    AliasInput,
    AliasResolution,
    CanonicalProduct,
    CanonicalUnit,
    MapIngredientInput,
    ProductIdentityCreate,
    UnitIdentityCreate,
    UnmappedIngredient,
)
from home_kitchen.catalogue.models import FoodCatalogueItem
from home_kitchen.catalogue.sku_models import RetailerProductOption, RetailerProductSnapshot
from home_kitchen.features import KitchenFeatures
from home_kitchen.history import HistoryEntry, HistoryEntryCreate, RetrievedEvidence
from home_kitchen.integrations import IntegrationInfo
from home_kitchen.inventory import InventoryItem, InventoryItemCreate, PurchasePlan
from home_kitchen.models import MealCreate, Recipe
from home_kitchen.planning_proposals import ProposalConflict, ProposalRead
from home_kitchen.purchasing import PurchaseModeInfo, StoreTrip
from home_kitchen.purchasing_reconciliation import (
    CompletedTrip,
    TripCompletionCreate,
    TripCompletionReview,
)
from home_kitchen.recipe_feedback import RecipeFeedbackCreate
from home_kitchen.recipes import RecipeDraft, RecipeDraftRequest, RecipeProviderInfo
from home_kitchen.recipes.collections import CollectionJob, CollectionKind
from home_kitchen.recipes.discovery import RecipeDiscoveryRequest, RecipeDiscoveryResponse
from home_kitchen.recipes.images import (
    MAX_CONTINUATION_LENGTH,
    MAX_QUERY_LENGTH,
    MAX_RESULTS,
    RecipeImageSearchSession,
)
from home_kitchen.recipes.photo_storage import PhotoAcquisition, StoredPhoto
from home_kitchen.recipes.photos import MAX_PHOTO_BYTES

MAX_PHOTO_JSON_BYTES = ((MAX_PHOTO_BYTES + 2) // 3) * 4 + 16_384


class PhotoUpload(BaseModel):
    mime_type: str = Field(max_length=64)
    data_base64: str = Field(min_length=4, max_length=((MAX_PHOTO_BYTES + 2) // 3) * 4)
    book_title: str | None = Field(default=None, max_length=300)
    page: str | None = Field(default=None, max_length=40)


class CollectionPreviewRequest(BaseModel):
    kinds: list[CollectionKind] = Field(min_length=1, max_length=2)


class CollectionImportRequest(BaseModel):
    selected_ids: list[str] = Field(min_length=1, max_length=1000)
    reviewed: bool = False


class RecipeImageSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    limit: int = Field(default=MAX_RESULTS, ge=1, le=MAX_RESULTS)
    continuation: str | None = Field(default=None, max_length=MAX_CONTINUATION_LENGTH)


class RecipeImageSelectionRequest(BaseModel):
    search_id: UUID
    provider_id: str = Field(min_length=1, max_length=80)
    reviewed: bool = False


class BasketSelectionRequest(BaseModel):
    start: date
    end: date
    selection: SelectedProduct


class BasketBatchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    need_id: UUID
    candidate_id: str = Field(min_length=1, max_length=200)
    packages: int = Field(gt=0, le=99)
    confirm_identity: bool = False


class BasketBatchSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    job_id: str = Field(min_length=1, max_length=64)
    selections: list[BasketBatchSelection] = Field(min_length=1, max_length=100)


class MatchJobRequest(BaseModel):
    start: date
    end: date
    page: int = Field(default=1, ge=1, le=50)
    size: int = Field(default=5, ge=1, le=5)
    force_refresh: bool = False
    need_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)


class MealPlanProposalCreate(BaseModel):
    meals: list[MealCreate] = Field(min_length=1, max_length=90)


class ExplicitReview(BaseModel):
    reviewed: bool = False


class TripCheck(BaseModel):
    checked: bool


def feature_router(features: KitchenFeatures, basket: BasketService) -> APIRouter:
    router = APIRouter(prefix="/api")

    def authenticated_actor(request: Request) -> str:
        actor = getattr(request.state, "actor_id", None)
        if not actor:
            raise HTTPException(401, "Household authentication required")
        return actor

    @router.post("/recipe-photos", status_code=201)
    async def upload_recipe_photo(request: Request, response: Response) -> PhotoAcquisition:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(415, "Photo upload requires JSON with base64 image data")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_PHOTO_JSON_BYTES:
                raise HTTPException(413, "Photo upload exceeds the supported size")
            chunks.append(chunk)
        try:
            data = PhotoUpload.model_validate(json.loads(b"".join(chunks)))
            raw = base64.b64decode(data.data_base64, validate=True)
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
            raise HTTPException(422, "Photo upload data is invalid") from None
        if not raw or len(raw) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "Photo must contain between 1 byte and 8 MiB")
        return await features.recipe_photos.create(
            raw, data.mime_type, actor, book_title=data.book_title, page=data.page
        )

    @router.get("/recipe-photos/{photo_id}")
    async def get_recipe_photo(photo_id: UUID, request: Request, response: Response) -> Response:
        authenticated_actor(request)
        photo: StoredPhoto = await features.recipe_photos.get(photo_id)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return Response(
            photo.data,
            media_type=photo.mime_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.delete("/recipe-photos/{photo_id}")
    async def discard_recipe_photo(
        photo_id: UUID, request: Request, response: Response
    ) -> dict[str, bool]:
        response.headers["Cache-Control"] = "no-store"
        deleted = await features.recipe_photos.discard(photo_id, authenticated_actor(request))
        return {"deleted": deleted}

    @router.post("/recipe-photos/{photo_id}/redraft")
    async def redraft_recipe_photo(
        photo_id: UUID, request: Request, response: Response
    ) -> PhotoAcquisition:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_photos.redraft(photo_id, authenticated_actor(request))

    @router.post("/recipe-collections", status_code=202)
    async def preview_recipe_collections(
        data: CollectionPreviewRequest, request: Request, response: Response
    ) -> CollectionJob:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_collections.start_preview(
            authenticated_actor(request), data.kinds
        )

    @router.get("/recipe-collections/{job_id}")
    async def get_recipe_collection_job(
        job_id: UUID, request: Request, response: Response
    ) -> CollectionJob:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_collections.get(authenticated_actor(request), job_id)

    @router.post("/recipe-collections/{job_id}/import", status_code=202)
    async def import_recipe_collection(
        job_id: UUID, data: CollectionImportRequest, request: Request, response: Response
    ) -> CollectionJob:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_collections.start_import(
            authenticated_actor(request), job_id, data.selected_ids, data.reviewed
        )

    @router.post("/recipe-collections/{job_id}/cancel")
    async def cancel_recipe_collection(
        job_id: UUID, request: Request, response: Response
    ) -> CollectionJob:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_collections.cancel(authenticated_actor(request), job_id)

    @router.post("/recipe-images/search")
    async def search_recipe_images(
        data: RecipeImageSearchRequest, request: Request, response: Response
    ) -> RecipeImageSearchSession:
        response.headers["Cache-Control"] = "no-store"
        return await features.recipe_images.search(
            authenticated_actor(request),
            data.query,
            limit=data.limit,
            continuation=data.continuation,
        )

    @router.post("/recipes/{recipe_id}/image")
    def select_recipe_image(
        recipe_id: UUID,
        data: RecipeImageSelectionRequest,
        request: Request,
        response: Response,
    ) -> Recipe:
        response.headers["Cache-Control"] = "no-store"
        return features.recipe_images.select(
            authenticated_actor(request),
            recipe_id,
            data.search_id,
            data.provider_id,
            reviewed=data.reviewed,
        )

    @router.post("/basket/match-jobs", status_code=202)
    async def start_match_job(
        data: MatchJobRequest, request: Request, response: Response
    ) -> BasketMatchJob:
        response.headers["Cache-Control"] = "no-store"
        return await features.basket_jobs.start(authenticated_actor(request), **data.model_dump())

    @router.get("/basket/match-jobs/{job_id}")
    async def get_match_job(job_id: str, request: Request, response: Response) -> BasketMatchJob:
        response.headers["Cache-Control"] = "no-store"
        return await features.basket_jobs.get(authenticated_actor(request), job_id)

    @router.post("/meal-plan-proposals", status_code=201)
    def propose_meals(
        data: MealPlanProposalCreate, request: Request, response: Response
    ) -> ProposalRead:
        response.headers["Cache-Control"] = "no-store"
        try:
            return features.plan_proposals.create(authenticated_actor(request), data.meals)
        except ProposalConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get("/meal-plan-proposals/{proposal_id}")
    def read_meal_proposal(proposal_id: UUID, request: Request, response: Response) -> ProposalRead:
        response.headers["Cache-Control"] = "no-store"
        return features.plan_proposals.get(proposal_id, authenticated_actor(request))

    @router.post("/meal-plan-proposals/{proposal_id}/confirm")
    def confirm_meal_proposal(
        proposal_id: UUID, data: ExplicitReview, request: Request, response: Response
    ) -> ProposalRead:
        response.headers["Cache-Control"] = "no-store"
        try:
            return features.plan_proposals.confirm(
                proposal_id, authenticated_actor(request), reviewed=data.reviewed
            )
        except ProposalConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @router.get("/integrations")
    def integrations(request: Request, response: Response) -> list[IntegrationInfo]:
        response.headers["Cache-Control"] = "no-store"
        return features.integrations.describe(authenticated_actor(request))

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

    @router.get("/catalogue/foods")
    def find_foods(
        q: str = Query(default="", max_length=200), limit: int = Query(default=10, ge=1, le=50)
    ) -> list[FoodCatalogueItem]:
        return features.catalogue.autocomplete(q, limit)

    @router.get("/catalogue/foods/{food_id}/products")
    def food_products(food_id: UUID, response: Response) -> list[RetailerProductOption]:
        response.headers["Cache-Control"] = "no-store"
        return features.catalogue_skus.list_options(features.household_id, food_id)

    @router.post("/catalogue/identities/products", status_code=201)
    def create_canonical_product(data: ProductIdentityCreate) -> CanonicalProduct:
        return features.catalogue_identities.create_product(data.canonical_name, data.variant)

    @router.post("/catalogue/identities/units", status_code=201)
    def create_canonical_unit(data: UnitIdentityCreate) -> CanonicalUnit:
        return features.catalogue_identities.create_unit(data.canonical_name)

    @router.get("/catalogue/aliases/{kind}")
    def resolve_canonical_alias(
        kind: str,
        alias: str = Query(min_length=1, max_length=200),
        locale: str = Query(min_length=2, max_length=20),
        context: str = Query(default="", max_length=100),
    ) -> AliasResolution:
        if kind not in {"product", "unit"}:
            raise HTTPException(404, "Unknown identity kind")
        return features.catalogue_identities.resolve(kind, locale, context, alias)

    @router.post("/catalogue/aliases/{kind}/{identity_id}", status_code=201)
    def add_canonical_alias(kind: str, identity_id: UUID, data: AliasInput) -> Response:
        if kind not in {"product", "unit"}:
            raise HTTPException(404, "Unknown identity kind")
        features.catalogue_identities.add_alias(
            kind, data.locale, data.context, data.alias, identity_id
        )
        return Response(status_code=201)

    @router.put("/catalogue/aliases/{kind}/{identity_id}/review")
    def correct_canonical_alias(kind: str, identity_id: UUID, data: AliasInput) -> Response:
        if kind not in {"product", "unit"}:
            raise HTTPException(404, "Unknown identity kind")
        features.catalogue_identities.correct_alias(
            kind, data.locale, data.context, data.alias, identity_id
        )
        return Response(status_code=204)

    @router.get("/catalogue/unmapped-ingredients")
    @router.get("/catalogue/identities/unmapped")
    def list_unmapped_ingredients(
        recipe_id: UUID | None = None,
    ) -> list[UnmappedIngredient]:
        try:
            return features.kitchen.list_unmapped_ingredients(recipe_id=recipe_id)
        except KeyError:
            raise HTTPException(404, "Recipe not found") from None

    @router.post("/catalogue/unmapped-ingredients/map")
    @router.post("/catalogue/identities/unmapped/map")
    def map_unmapped_ingredient(data: MapIngredientInput) -> Recipe:
        if data.canonical_product_id is None and data.canonical_unit_id is None:
            raise HTTPException(
                422, "Either canonical_product_id or canonical_unit_id must be provided"
            )
        try:
            return features.kitchen.map_unmapped_ingredient(
                recipe_id=data.recipe_id,
                ingredient_index=data.ingredient_index,
                canonical_product_id=data.canonical_product_id,
                canonical_unit_id=data.canonical_unit_id,
                add_alias=data.add_alias,
                locale=data.locale,
                context=data.context,
            )
        except KeyError:
            raise HTTPException(404, "Recipe not found") from None
        except IndexError:
            raise HTTPException(404, "Ingredient index out of range") from None
        except ValueError as err:
            raise HTTPException(422, str(err)) from err

    @router.post("/catalogue/foods/{food_id}/products", status_code=201)
    def remember_food_product(
        food_id: UUID, data: RetailerProductSnapshot, request: Request
    ) -> RetailerProductOption:
        actor = getattr(request.state, "actor_id", None)
        if not actor:
            raise HTTPException(401, "Household authentication required")
        return features.catalogue_skus.remember_selection(
            features.household_id, food_id, data, actor_id=actor
        )

    @router.put("/catalogue/products/{option_id}/preferred")
    def prefer_food_product(option_id: UUID, request: Request) -> RetailerProductOption:
        actor = getattr(request.state, "actor_id", None)
        if not actor:
            raise HTTPException(401, "Household authentication required")
        return features.catalogue_skus.set_preferred(
            features.household_id, option_id, actor_id=actor
        )

    @router.delete("/catalogue/products/{option_id}/preferred")
    def unprefer_food_product(option_id: UUID, request: Request) -> RetailerProductOption:
        actor = getattr(request.state, "actor_id", None)
        if not actor:
            raise HTTPException(401, "Household authentication required")
        return features.catalogue_skus.unset_preferred(
            features.household_id, option_id, actor_id=actor
        )

    @router.get("/store-trip/completion-review")
    def review_trip_completion(start: date, end: date, response: Response) -> TripCompletionReview:
        response.headers["Cache-Control"] = "no-store"
        return features.trip_completion.review(start, end)

    @router.post("/store-trip/completions", status_code=201)
    def complete_trip(
        data: TripCompletionCreate, request: Request, response: Response
    ) -> CompletedTrip:
        if not getattr(request.state, "actor_id", None):
            raise HTTPException(401, "Household authentication required")
        response.headers["Cache-Control"] = "no-store"
        return features.trip_completion.complete(data)

    @router.get("/store-trip/completions")
    def completed_trips(
        response: Response, limit: int = Query(default=30, ge=1, le=100)
    ) -> list[CompletedTrip]:
        response.headers["Cache-Control"] = "no-store"
        return features.trip_completion.list(limit)

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

    @router.put("/basket/selections/batch")
    async def select_products_batch(
        data: BasketBatchSelectionRequest, request: Request, response: Response
    ) -> BasketReview:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        if data.end < data.start:
            raise HTTPException(422, "end date must be on or after start date")
        references = [(item.need_id, item.candidate_id) for item in data.selections]
        if len({need_id for need_id, _ in references}) != len(references):
            raise HTTPException(422, "Select at most one product for each purchase need")
        if len(references) > 100:
            raise HTTPException(422, "Select no more than 100 products to save")
        if len(set(references)) != len(references):
            raise HTTPException(422, "Do not select a product more than once for a need")
        try:
            identity_confirmations = {
                (item.need_id, item.candidate_id)
                for item in data.selections
                if item.confirm_identity
            }
            resolve = features.basket_jobs.resolve_batch_candidates
            if identity_confirmations:
                resolved = await resolve(
                    actor,
                    data.job_id,
                    data.start,
                    data.end,
                    references,
                    identity_confirmations=identity_confirmations,
                )
            else:
                resolved = await resolve(actor, data.job_id, data.start, data.end, references)
            selections = []
            evidence = {}
            for item in data.selections:
                matched_need, candidate, observed_at = resolved[(item.need_id, item.candidate_id)]
                if (
                    candidate.pack_quantity is None
                    or candidate.pack_unit is None
                    or candidate.pack_metadata_status != "unverified"
                ):
                    raise ValueError("A selected product has unknown package details.")
                price = None
                if candidate.price is not None:
                    try:
                        price = Decimal(candidate.price)
                    except InvalidOperation:
                        price = None
                selections.append(
                    SelectedProduct(
                        need_id=item.need_id,
                        product=ProductSnapshot(
                            provider=candidate.provider,
                            product_id=candidate.product_id,
                            name=candidate.name,
                            brand=candidate.brand,
                            url=candidate.product_url,
                            image_url=candidate.media.image_url if candidate.media else None,
                            pack_quantity=candidate.pack_quantity,
                            pack_unit=candidate.pack_unit,
                            price=price,
                            currency="NOK",
                            available=candidate.available,
                            observed_at=observed_at,
                        ),
                        packages=item.packages,
                    )
                )
                evidence[item.need_id] = matched_need
            return await run_in_threadpool(
                basket.select_batch,
                data.start,
                data.end,
                selections,
                evidence,
            )
        except KeyError:
            raise HTTPException(404, "Product match job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.delete("/basket/selections/{need_id}/{product_id}")
    def remove_selection(need_id: UUID, product_id: str, start: date, end: date) -> BasketReview:
        return basket.remove(start, end, need_id, product_id)

    return router
