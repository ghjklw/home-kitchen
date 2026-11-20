"""Feature routes use application services also exposed through MCP."""

import base64
import binascii
import json
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from home_kitchen.basket import BasketReview, SelectedProduct
from home_kitchen.basket.application import (
    BatchSelectionInput,
    BatchSelectionValidationError,
    select_basket_products_batch,
)
from home_kitchen.basket.jobs import BackgroundMatchingHealth, BasketMatchJob
from home_kitchen.basket.matching import BasketMatchResult
from home_kitchen.basket.repository import BasketService
from home_kitchen.catalogue.identity import (
    AliasInput,
    AliasResolution,
    CanonicalProduct,
    CanonicalUnit,
    IdentityCorrectionEvent,
    MapIngredientInput,
    ProductIdentityCreate,
    UnitIdentityCreate,
    UnmappedIngredient,
)
from home_kitchen.catalogue.models import FoodCatalogueItem
from home_kitchen.catalogue.overview_models import ProductOverview, ProductPreferencesUpdate
from home_kitchen.catalogue.sku_models import RetailerProductOption, RetailerProductSnapshot
from home_kitchen.connector_auth import (
    ConnectorAuthError,
    ConnectorAuthService,
    CookidooLoginRequest,
    PKCECompleteRequest,
    PKCEStartRequest,
    PKCEStartResponse,
    RefreshRequest,
    TrumfOTPStartRequest,
    TrumfOTPStartResponse,
    TrumfOTPVerifyRequest,
)
from home_kitchen.features import KitchenFeatures
from home_kitchen.history import HistoryEntry, HistoryEntryCreate, RetrievedEvidence
from home_kitchen.integrations import IntegrationInfo
from home_kitchen.inventory import InventoryItem, InventoryItemCreate, PurchasePlan
from home_kitchen.models import MealCreate, Recipe
from home_kitchen.planning_proposals import ProposalConflict, ProposalRead
from home_kitchen.purchase_sync import (
    PurchaseHistoryStatusResponse,
    PurchaseHistorySyncRequest,
    PurchaseHistorySyncResponse,
)
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
    provider: Literal["oda", "cookidoo"] = "oda"
    kinds: list[CollectionKind] = Field(default_factory=list, max_length=3)


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


class BasketBatchSelection(BatchSelectionInput):
    pass


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


class AutoMapIngredientsInput(BaseModel):
    recipe_id: UUID | None = None
    batch_size: int = Field(default=30, ge=1, le=100)
    max_batches: int = Field(default=1, ge=1, le=20)


class AutoMapIngredientsResponse(BaseModel):
    processed_unique: int
    mapped_occurrences: int
    created_products: int
    created_aliases: int
    remaining_unmapped: int


class ConnectorConnectRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auth_mode: str | None = None
    session_token: SecretStr | None = None
    session_cookie: SecretStr | None = None
    token: SecretStr | None = None
    cookie: SecretStr | None = None
    api_key: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    raw_payload: SecretStr | None = None
    expires_at: datetime | None = None
    account_id: str | None = None
    account_label: str | None = None
    add_account: bool = False


def _extract_credentials(
    provider_id: str, data: ConnectorConnectRequest
) -> tuple[str, dict[str, str]]:
    if provider_id not in {"rema1000", "coop", "trumf", "cookidoo", "kassal"}:
        raise HTTPException(404, f"Ukjent leverandør: {provider_id}")

    auth_mode = data.auth_mode
    credentials: dict[str, str] = {}

    if provider_id == "kassal":
        key = data.api_key or data.token or data.session_token or data.raw_payload
        if not key or not key.get_secret_value().strip():
            raise HTTPException(422, "Kassal.app krever en gyldig API-nøkkel")
        auth_mode = "api_key"
        credentials["api_key"] = key.get_secret_value().strip()

    elif provider_id == "rema1000":
        token = data.session_token or data.token or data.raw_payload
        if not token or not token.get_secret_value().strip():
            raise HTTPException(422, "Rema 1000 krever et gyldig sesjonstoken")
        auth_mode = "session_token"
        credentials["session_token"] = token.get_secret_value().strip()

    elif provider_id == "trumf":
        cookie = (
            data.session_cookie
            or data.cookie
            or data.session_token
            or data.token
            or data.raw_payload
        )
        if not cookie or not cookie.get_secret_value().strip():
            raise HTTPException(422, "Trumf krever en gyldig sesjonsinformasjonskapsel")
        auth_mode = "session_cookie"
        credentials["session_cookie"] = cookie.get_secret_value().strip()

    elif provider_id == "coop":
        token = data.session_token or data.token
        cookie = data.session_cookie or data.cookie or data.raw_payload
        if auth_mode == "session_cookie" or (cookie and not token):
            if not cookie or not cookie.get_secret_value().strip():
                raise HTTPException(
                    422, "Coop krever en gyldig informasjonskapsel for denne innloggingsmetoden"
                )
            auth_mode = "session_cookie"
            credentials["session_cookie"] = cookie.get_secret_value().strip()
        else:
            cand = token or cookie
            if not cand or not cand.get_secret_value().strip():
                raise HTTPException(
                    422, "Coop krever et gyldig sesjonstoken eller informasjonskapsel"
                )
            auth_mode = "session_token"
            credentials["session_token"] = cand.get_secret_value().strip()

    elif provider_id == "cookidoo":
        if auth_mode == "credentials" or (data.username and data.password):
            if not data.username or not data.username.strip():
                raise HTTPException(422, "Cookidoo krever et brukernavn")
            if not data.password or not data.password.get_secret_value().strip():
                raise HTTPException(422, "Cookidoo krever et passord")
            auth_mode = "credentials"
            credentials["username"] = data.username.strip()
            credentials["password"] = data.password.get_secret_value().strip()
        else:
            cookie = data.session_cookie or data.cookie or data.raw_payload
            if not cookie or not cookie.get_secret_value().strip():
                raise HTTPException(
                    422, "Cookidoo krever en informasjonskapsel eller brukernavn/passord"
                )
            auth_mode = "session_cookie"
            credentials["session_cookie"] = cookie.get_secret_value().strip()

    return auth_mode, credentials


class IntegrationHealthSummary(BaseModel):
    overall_status: Literal["healthy", "degraded", "attention_needed"]
    checked_at: datetime
    integrations: list[IntegrationInfo] = Field(default_factory=list)
    background_matching: BackgroundMatchingHealth
    purchase_sync: PurchaseHistoryStatusResponse | None = None
    actionable_issues: list[str] = Field(default_factory=list)


def build_integration_health_summary(
    features: KitchenFeatures, actor_id: str
) -> IntegrationHealthSummary:
    """Aggregate secret-free connection, token, purchase sync, and matching freshness health."""
    integrations = features.integrations.describe(actor_id)
    issues: list[str] = []
    for item in integrations:
        if item.status == "expired" or item.token_status == "expired":
            issues.append(
                item.expiry_prompt
                or f"{item.name}: Tilkoblingen har utløpt. Forny eller koble til på nytt i Innstillinger."
            )
        elif item.last_error:
            issues.append(f"{item.name}: {item.last_error}")

    jobs = getattr(features, "basket_jobs", None)
    matching_health = (
        jobs.health_snapshot(actor_id)
        if jobs is not None and callable(getattr(jobs, "health_snapshot", None))
        else BackgroundMatchingHealth()
    )
    if matching_health.status in {"failed", "stale"} and matching_health.freshness_note:
        issues.append(matching_health.freshness_note)

    purchase_sync_svc = getattr(features, "purchase_sync", None)
    purchase_sync = (
        purchase_sync_svc.get_status(account_id=actor_id)
        if purchase_sync_svc is not None
        and callable(getattr(purchase_sync_svc, "get_status", None))
        else None
    )
    if purchase_sync is not None:
        for prov, status_info in purchase_sync.providers.items():
            if status_info.last_sync_error:
                issues.append(f"Kvitteringssynk ({prov}): {status_info.last_sync_error}")

    overall: Literal["healthy", "degraded", "attention_needed"] = "healthy"
    if (
        any(item.status == "expired" or item.token_status == "expired" for item in integrations)
        or matching_health.status == "failed"
    ):
        overall = "attention_needed"
    elif issues:
        overall = "degraded"

    return IntegrationHealthSummary(
        overall_status=overall,
        checked_at=datetime.now(UTC),
        integrations=integrations,
        background_matching=matching_health,
        purchase_sync=purchase_sync,
        actionable_issues=issues,
    )


def feature_router(features: KitchenFeatures, basket: BasketService) -> APIRouter:
    router = APIRouter(prefix="/api")

    def get_connector_auth() -> ConnectorAuthService:
        auth = getattr(features, "connector_auth", None)
        if isinstance(auth, ConnectorAuthService):
            return auth
        store = getattr(features, "connector_store", None)
        integrations = getattr(features, "integrations", None)
        household_id = getattr(features, "household_id", None)
        if store is None or integrations is None or household_id is None:
            raise HTTPException(
                status_code=503, detail="Tilkoblingsautentisering er ikke tilgjengelig"
            )
        return ConnectorAuthService(
            store=store,
            integrations=integrations,
            household_id=household_id,
            settings=getattr(features, "settings", None),
        )

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
            authenticated_actor(request), data.kinds, provider_id=data.provider
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

    @router.get("/integrations/health")
    def integrations_health(request: Request, response: Response) -> IntegrationHealthSummary:
        response.headers["Cache-Control"] = "no-store"
        return build_integration_health_summary(features, authenticated_actor(request))

    @router.post("/integrations/{provider_id}/connect")
    @router.put("/integrations/{provider_id}")
    def connect_integration(
        provider_id: str,
        data: ConnectorConnectRequest,
        request: Request,
        response: Response,
    ) -> IntegrationInfo:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        if provider_id == "oda":
            raise HTTPException(400, "Oda bruker OAuth PKCE-flyt via /api/oda/connect")
        if provider_id == "anthropic":
            raise HTTPException(
                400, "Oppskriftsassistenten konfigureres i miljøet av administratoren"
            )
        auth_mode, credentials = _extract_credentials(provider_id, data)
        if provider_id == "kassal":
            try:
                return get_connector_auth().connect_kassal_key(
                    household_id=features.household_id,
                    actor_id=actor,
                    api_key=credentials["api_key"],
                    account_id=data.account_id,
                    account_label=data.account_label,
                    add_account=data.add_account,
                )
            except ConnectorAuthError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
        if provider_id == "trumf":
            try:
                return get_connector_auth().connect_trumf_cookie(
                    household_id=features.household_id,
                    actor_id=actor,
                    raw_cookie=credentials["session_cookie"],
                    expires_at=data.expires_at,
                    account_id=data.account_id,
                    account_label=data.account_label,
                    add_account=data.add_account,
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from None
        try:
            features.connector_store.save_connection(
                household_id=features.household_id,
                provider_id=provider_id,
                auth_mode=auth_mode,
                credentials=credentials,
                expires_at=data.expires_at,
                actor_id=actor,
                account_id=data.account_id,
                account_label=data.account_label,
                add_account=data.add_account,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None

        try:
            return features.integrations.describe_one(provider_id, actor)
        except KeyError:
            raise HTTPException(404, f"Ukjent leverandør: {provider_id}") from None

    @router.delete("/integrations/{provider_id}/accounts/{account_id}", status_code=204)
    def disconnect_integration_account(
        provider_id: str,
        account_id: str,
        request: Request,
        response: Response,
    ) -> Response:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        if provider_id == "oda":
            features.oda.disconnect(actor)
            return Response(status_code=204)
        if provider_id not in {"rema1000", "coop", "trumf", "cookidoo"}:
            raise HTTPException(404, f"Ukjent leverandør: {provider_id}")
        deleted = features.connector_store.delete_account_credentials(
            features.household_id, provider_id, account_id
        )
        if not deleted:
            raise HTTPException(
                404, f"Ingen tilkoblet konto '{account_id}' funnet for '{provider_id}'."
            )
        return Response(status_code=204)

    @router.delete("/integrations/{provider_id}/connection", status_code=204)
    @router.delete("/integrations/{provider_id}", status_code=204)
    def disconnect_integration(
        provider_id: str,
        request: Request,
        response: Response,
        account_id: str | None = Query(default=None),
    ) -> Response:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        if provider_id == "oda":
            features.oda.disconnect(actor)
            return Response(status_code=204)
        if provider_id not in {"rema1000", "coop", "trumf", "cookidoo"}:
            raise HTTPException(404, f"Ukjent leverandør: {provider_id}")
        features.connector_store.delete_connection(
            features.household_id, provider_id, account_id=account_id
        )
        return Response(status_code=204)

    @router.post("/integrations/{provider_id}/pkce/start")
    def start_pkce_flow(
        provider_id: str,
        request: Request,
        response: Response,
        data: PKCEStartRequest | None = None,
        account_id: str | None = Query(default=None),
        account_label: str | None = Query(default=None),
        add_account: bool = Query(default=False),
    ) -> PKCEStartResponse:
        authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        return get_connector_auth().start_pkce(
            provider_id=provider_id,
            household_id=features.household_id,
            redirect_uri=data.redirect_uri if data else None,
            account_id=(data.account_id if data and data.account_id is not None else account_id),
            account_label=(
                data.account_label if data and data.account_label is not None else account_label
            ),
            add_account=bool((data and data.add_account) or add_account),
        )

    @router.post("/integrations/{provider_id}/pkce/complete")
    def complete_pkce_flow(
        provider_id: str,
        data: PKCECompleteRequest,
        request: Request,
        response: Response,
    ) -> IntegrationInfo:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        return get_connector_auth().complete_pkce(
            provider_id=provider_id,
            household_id=features.household_id,
            actor_id=actor,
            code=data.code,
            state=data.state,
            redirect_url=data.redirect_url,
            account_id=data.account_id,
            account_label=data.account_label,
            add_account=data.add_account,
        )

    @router.get("/integrations/{provider_id}/pkce/callback")
    def pkce_callback(
        provider_id: str,
        request: Request,
        code: str | None = None,
        state: str | None = None,
        account_id: str | None = None,
        account_label: str | None = None,
        add_account: bool = False,
    ) -> RedirectResponse:
        actor = authenticated_actor(request)
        get_connector_auth().complete_pkce(
            provider_id=provider_id,
            household_id=features.household_id,
            actor_id=actor,
            code=code,
            state=state,
            redirect_url=str(request.url),
            account_id=account_id,
            account_label=account_label,
            add_account=add_account,
        )
        return RedirectResponse(url="/#settings", status_code=302)

    @router.post("/integrations/cookidoo/login")
    def cookidoo_login(
        data: CookidooLoginRequest,
        request: Request,
        response: Response,
    ) -> IntegrationInfo:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        return get_connector_auth().login_cookidoo(
            household_id=features.household_id,
            actor_id=actor,
            email=data.email,
            password=data.password.get_secret_value(),
            country=data.country,
            account_id=data.account_id,
            account_label=data.account_label,
            add_account=data.add_account,
        )

    @router.post("/integrations/trumf/login/start")
    def trumf_login_start(
        data: TrumfOTPStartRequest,
        request: Request,
        response: Response,
    ) -> TrumfOTPStartResponse:
        authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        return get_connector_auth().start_trumf_otp(
            household_id=features.household_id,
            phone=data.phone,
            account_id=data.account_id,
            account_label=data.account_label,
            add_account=data.add_account,
        )

    @router.post("/integrations/trumf/login/verify")
    def trumf_login_verify(
        data: TrumfOTPVerifyRequest,
        request: Request,
        response: Response,
    ) -> IntegrationInfo:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        return get_connector_auth().verify_trumf_otp(
            household_id=features.household_id,
            actor_id=actor,
            session_id=data.session_id,
            otp=data.otp,
            account_id=data.account_id,
            account_label=data.account_label,
            add_account=data.add_account,
        )

    @router.post("/integrations/{provider_id}/refresh")
    def refresh_integration(
        provider_id: str,
        request: Request,
        response: Response,
        account_id: str | None = Query(default=None),
        data: RefreshRequest | None = None,
    ) -> IntegrationInfo:
        actor = authenticated_actor(request)
        response.headers["Cache-Control"] = "no-store"
        target_account_id = (
            account_id if account_id is not None else (data.account_id if data else None)
        )
        return get_connector_auth().refresh_connection(
            household_id=features.household_id,
            actor_id=actor,
            provider_id=provider_id,
            account_id=target_account_id,
        )

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

    @router.get("/purchase-history/status")
    def purchase_history_status(request: Request) -> PurchaseHistoryStatusResponse:
        actor = getattr(request.state, "actor_id", None) or "default"
        return features.purchase_sync.get_status(account_id=actor)

    @router.post("/purchase-history/sync")
    async def purchase_history_sync(
        request: Request,
        data: PurchaseHistorySyncRequest | None = None,
    ) -> PurchaseHistorySyncResponse:
        actor = getattr(request.state, "actor_id", None) or "default"
        provider = data.provider if data else None
        max_pages = data.max_pages if data else 50
        return await run_in_threadpool(
            features.purchase_sync.sync,
            account_id=actor,
            provider=provider,
            max_pages=max_pages,
        )

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

    @router.get("/catalogue/aliases/corrections")
    def list_canonical_alias_corrections(
        kind: str | None = Query(default=None),
        alias: str | None = Query(default=None, max_length=200),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[IdentityCorrectionEvent]:
        if kind is not None and kind not in {"product", "unit"}:
            raise HTTPException(404, "Unknown identity kind")
        return features.catalogue_identities.list_correction_events(
            kind=kind,
            alias=alias,
            household_id=features.household_id,
            limit=limit,
        )

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
    def correct_canonical_alias(
        kind: str, identity_id: UUID, data: AliasInput, request: Request
    ) -> Response:
        if kind not in {"product", "unit"}:
            raise HTTPException(404, "Unknown identity kind")
        actor = getattr(request.state, "actor_id", None) or "api"
        try:
            features.catalogue_identities.correct_alias(
                kind,
                data.locale,
                data.context,
                data.alias,
                identity_id,
                actor_id=actor,
                household_id=features.household_id,
                channel="api",
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return Response(status_code=204)

    @router.get("/catalogue/overview")
    def catalogue_overview(
        q: str | None = Query(default=None, max_length=200),
        locale: str = Query(default="nb", min_length=2, max_length=20),
    ) -> list[ProductOverview]:
        return features.product_overview.get_overview(query=q, locale=locale)

    @router.get("/catalogue/overview/{product_id}")
    def catalogue_product_overview(
        product_id: UUID,
        locale: str = Query(default="nb", min_length=2, max_length=20),
    ) -> ProductOverview:
        try:
            return features.product_overview.get_product_overview(product_id, locale=locale)
        except KeyError:
            raise HTTPException(404, "Canonical product not found") from None

    @router.get("/catalogue/products/{product_id}/preferences")
    def get_catalogue_product_preferences(product_id: UUID) -> dict[str, Any]:
        try:
            pref, exc = features.product_overview.get_preferences(product_id)
            return {"preferred_oda_skus": pref, "excluded_oda_skus": exc}
        except KeyError:
            raise HTTPException(404, "Canonical product not found") from None

    @router.put("/catalogue/products/{product_id}/preferences")
    def update_catalogue_product_preferences(
        product_id: UUID,
        data: ProductPreferencesUpdate,
        request: Request,
    ) -> ProductOverview:
        actor = getattr(request.state, "actor_id", None) or "local"
        try:
            return features.product_overview.update_preferences(product_id, data, actor_id=actor)
        except KeyError:
            raise HTTPException(404, "Canonical product not found") from None

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

    @router.post("/catalogue/unmapped-ingredients/auto-map")
    @router.post("/catalogue/identities/unmapped/auto-map")
    async def auto_map_unmapped_ingredients_route(
        request: Request,
        data: AutoMapIngredientsInput | None = None,
    ) -> AutoMapIngredientsResponse:
        from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_ingredients_async

        payload = data or AutoMapIngredientsInput()
        actor = getattr(request.state, "actor_id", None) or "system"
        settings = getattr(features, "settings", None) or getattr(
            features.kitchen, "settings", None
        )
        household_id = getattr(features, "household_id", None) or getattr(
            features.kitchen, "household_id", None
        )
        try:
            result = await auto_map_unmapped_ingredients_async(
                features.kitchen.store,
                settings,
                household_id=household_id,
                actor_id=actor,
                recipe_id=payload.recipe_id,
                batch_size=payload.batch_size,
                max_batches=payload.max_batches,
                identity_service=features.catalogue_identities,
            )
            return AutoMapIngredientsResponse.model_validate(result)
        except KeyError:
            raise HTTPException(404, "Recipe not found") from None
        except ValueError as err:
            raise HTTPException(422, str(err)) from err
        except Exception as err:
            raise HTTPException(502, "KI-kartlegging er midlertidig utilgjengelig.") from err

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
        try:
            return await select_basket_products_batch(
                basket,
                features.basket_jobs,
                start=data.start,
                end=data.end,
                job_id=data.job_id,
                selections=data.selections,
                actor_id=actor,
                channel="api",
            )
        except BatchSelectionValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "Product match job not found") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.delete("/basket/selections/{need_id}/{product_id}")
    def remove_selection(need_id: UUID, product_id: str, start: date, end: date) -> BasketReview:
        return basket.remove(start, end, need_id, product_id)

    return router
