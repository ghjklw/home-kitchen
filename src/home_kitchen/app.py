"""Shared household actions for the PWA, REST clients and AI assistants."""

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, StringConstraints
from pydantic_ai import Agent
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request

from home_kitchen.ai_credentials import close_ai_clients
from home_kitchen.auth import AccessControl
from home_kitchen.basket.repository import BasketService
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.features import KitchenFeatures
from home_kitchen.imports import ImportedDraft
from home_kitchen.mcp_auth import HouseholdAzureProvider
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import (
    Meal,
    MealCreate,
    PlannedMealRecipe,
    Recipe,
    RecipeCreate,
    RegularItem,
    ShoppingItem,
    ShoppingItemCreate,
    WeekShopping,
)
from home_kitchen.oda_api import oda_router
from home_kitchen.recipes import RecipeDraftRequest
from home_kitchen.recommendations import PlanDraft
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings
from home_kitchen.store import MealSlotOccupied


class Completion(BaseModel):
    completed: bool


class Purchase(BaseModel):
    purchased_on: date


class RegularCreate(ShoppingItemCreate):
    interval_days: int = Field(default=7, gt=0)


class ImportRequest(BaseModel):
    url: HttpUrl


class Prompt(BaseModel):
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class PlanRequest(BaseModel):
    start: date
    days: int = Field(default=7, ge=1, le=14)


def create_app(
    settings: Settings | None = None, ai: Agent[None, RecipeCreate] | None = None
) -> FastAPI:
    settings = settings or Settings()
    service = KitchenService(settings.database_url.get_secret_value())

    def load_household() -> Household:
        value = service.store.get_setting("household")
        return Household.model_validate_json(value) if value else Household()

    features = KitchenFeatures(service, settings, load_household, ai)
    basket = BasketService(service, features.history)
    mcp_auth = (
        HouseholdAzureProvider(settings, service.store.engine)
        if settings.mcp_oauth_enabled
        else None
    )
    mcp = build_mcp(service, load_household, auth=mcp_auth)
    install_feature_tools(mcp, features, basket)
    mcp_app = mcp.http_app(path="/", stateless_http=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            try:
                await close_ai_clients(settings)
            finally:
                features.oda.close()
                service.store.engine.dispose()

    app = FastAPI(title="Home Kitchen", version="0.1.0", lifespan=lifespan)
    app.state.features = features
    app.state.service = service
    app.include_router(feature_router(features, basket))
    app.include_router(oda_router(features.oda, features.oda_cart))
    app.state.mcp = mcp
    if mcp_auth is not None:
        # Discovery is rooted at the origin even though OAuth lives under /mcp.
        # Install these before the static application fallback.
        app.router.routes.extend(mcp_auth.get_well_known_routes(mcp_path="/"))
    access = AccessControl(settings)
    app.state.access = access
    app.middleware("http")(access.middleware)

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _exc: Exception):
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    @app.exception_handler(MealSlotOccupied)
    async def meal_conflict(_request: Request, exc: MealSlotOccupied):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    def health():
        try:
            service.store.health()
        except SQLAlchemyError:
            raise HTTPException(503, "Storage unavailable") from None
        return {"status": "ok"}

    @app.get("/api/household")
    def get_household() -> Household:
        return load_household()

    @app.put("/api/household")
    def update_household(data: Household) -> Household:
        service.store.set_setting("household", data.model_dump_json())
        return data

    @app.get("/api/recipes")
    def list_recipes() -> list[Recipe]:
        return service.list_recipes()

    @app.post("/api/recipes", status_code=201)
    def create_recipe(data: RecipeCreate) -> Recipe:
        return service.create_recipe(data)

    @app.get("/api/recipes/{recipe_id}")
    def get_recipe(recipe_id: UUID) -> Recipe:
        return service.get_recipe(recipe_id)

    @app.put("/api/recipes/{recipe_id}")
    def update_recipe(recipe_id: UUID, data: RecipeCreate) -> Recipe:
        return service.update_recipe(recipe_id, data)

    @app.delete("/api/recipes/{recipe_id}")
    def delete_recipe(recipe_id: UUID) -> dict[str, bool]:
        service.delete_recipe(recipe_id)
        return {"deleted": True}

    @app.get("/api/meals")
    def list_meals(start: date, end: date) -> list[Meal]:
        return service.list_meals(start, end)

    @app.post("/api/meals", status_code=201)
    def plan_meal(data: MealCreate) -> Meal:
        return service.plan_meal(data)

    @app.get("/api/meals/{meal_id}/recipe")
    def get_meal_recipe(meal_id: UUID) -> PlannedMealRecipe:
        return service.meal_recipe(meal_id)

    @app.delete("/api/meals/{meal_id}")
    def delete_meal(meal_id: UUID) -> dict[str, bool]:
        service.delete_meal(meal_id)
        return {"deleted": True}

    @app.get("/api/items")
    def list_items() -> list[ShoppingItem]:
        return service.list_items()

    @app.post("/api/items", status_code=201)
    def add_item(data: ShoppingItemCreate) -> ShoppingItem:
        return service.add_item(data)

    @app.patch("/api/items/{item_id}")
    def complete_item(item_id: UUID, data: Completion) -> ShoppingItem:
        return service.complete_item(item_id, data.completed)

    @app.delete("/api/items/{item_id}")
    def delete_item(item_id: UUID) -> dict[str, bool]:
        service.delete_item(item_id)
        return {"deleted": True}

    @app.get("/api/regular-items")
    def list_regular_items() -> list[RegularItem]:
        return service.list_regular_items()

    @app.post("/api/regular-items", status_code=201)
    def add_regular_item(data: RegularCreate) -> RegularItem:
        item = ShoppingItemCreate.model_validate(data.model_dump())
        return service.add_regular_item(item, data.interval_days)

    @app.post("/api/regular-items/{item_id}/confirm")
    def confirm_suggestion(item_id: UUID) -> ShoppingItem:
        return service.confirm_suggestion(item_id)

    @app.post("/api/regular-items/{item_id}/purchase")
    def record_purchase(item_id: UUID, data: Purchase) -> RegularItem:
        return service.record_purchase(item_id, data.purchased_on)

    @app.get("/api/shopping")
    def shopping_list(start: date, end: date) -> WeekShopping:
        return service.shopping_list(start, end, today=date.today())

    @app.post("/api/recipe-import")
    async def import_draft(data: ImportRequest) -> ImportedDraft:
        try:
            draft = await features.draft_recipe("web", RecipeDraftRequest(url=data.url))
            assert draft.imported is not None
            return draft.imported
        except httpx.HTTPError:
            raise HTTPException(422, "Recipe source could not be fetched") from None
        except Exception as exc:
            # Scraper-specific errors must not disclose source content or credentials.
            raise HTTPException(422, "Recipe could not be imported; paste it manually") from exc

    @app.post("/api/ai/recipe")
    async def generate_recipe(data: RecipeDraftRequest):
        # Legacy URL now uses the shared source-backed editable draft envelope.
        data.require_ai_source()
        if not (ai or settings.ai_configured):
            raise HTTPException(503, "Configure an AI model and API credential to adapt recipes")
        try:
            return await features.draft_recipe("ai", data)
        except ValueError:
            raise
        except Exception:
            raise HTTPException(502, "AI draft unavailable; no recipe has been saved") from None

    @app.post("/api/ai/plan")
    async def generate_plan(data: PlanRequest) -> PlanDraft:
        if not service.list_recipes():
            raise HTTPException(422, "Save a recipe before requesting a meal plan")
        if not settings.ai_configured:
            raise HTTPException(503, "Configure an AI model and API credential to recommend meals")
        try:
            return await features.recommend_week(data.start, data.days)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502, "AI plan unavailable; no meals have been saved") from None

    @app.get("/api/integrations")
    def integrations(request: Request):
        oda_status = features.oda.status(request.state.actor_id)
        return {
            "oda": {
                "status": "connected" if oda_status["connected"] else "setup_required",
                "mcp_url": "https://oda.com/mcp",
                "checkout_url": "https://oda.com/no/cart/",
                "message": "Connect your Oda account directly in Kitchen. Review and pay in Oda.",
            },
            "ai": {"configured": bool(ai or settings.ai_configured)},
            "auth": {"mode": "local" if settings.local_dev else "entra"},
        }

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(
            Path(__file__).parent / "static" / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    app.mount("/mcp", mcp_app)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="pwa")
    return app
