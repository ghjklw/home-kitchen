"""Shared household actions for the PWA, REST clients and AI assistants."""

import sqlite3
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
from starlette.requests import Request

from home_kitchen.ai import draft_recipe, recipe_agent
from home_kitchen.auth import AccessControl
from home_kitchen.imports import ImportedDraft, import_recipe
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import (
    Meal,
    MealCreate,
    Recipe,
    RecipeCreate,
    RegularItem,
    ShoppingItem,
    ShoppingItemCreate,
    WeekShopping,
)
from home_kitchen.recommendations import PlanDraft, build_planner, recommend_plan
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


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
    service = KitchenService(settings.database_path)
    with service.store.connection() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")

    def load_household() -> Household:
        with service.store.connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key='household'").fetchone()
        return Household.model_validate_json(row[0]) if row else Household()

    mcp = build_mcp(service, load_household)
    mcp_app = mcp.http_app(path="/", stateless_http=True)
    app = FastAPI(title="Home Kitchen", version="0.1.0", lifespan=mcp_app.lifespan)
    app.state.service = service
    app.state.mcp = mcp
    access = AccessControl(settings)
    app.state.access = access
    app.middleware("http")(access.middleware)

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _exc: Exception):
        return JSONResponse({"detail": "Item not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_request: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.get("/healthz", include_in_schema=False)
    def health():
        try:
            with service.store.connection() as connection:
                connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            raise HTTPException(503, "Storage unavailable") from None
        return {"status": "ok"}

    @app.get("/api/household")
    def get_household() -> Household:
        return load_household()

    @app.put("/api/household")
    def update_household(data: Household) -> Household:
        with service.store.connection() as connection:
            connection.execute(
                "INSERT INTO settings VALUES ('household', ?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (data.model_dump_json(),),
            )
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
            return await import_recipe(str(data.url))
        except httpx.HTTPError:
            raise HTTPException(422, "Recipe source could not be fetched") from None
        except Exception as exc:
            # Scraper-specific errors must not disclose source content or credentials.
            raise HTTPException(422, "Recipe could not be imported; paste it manually") from exc

    @app.post("/api/ai/recipe")
    async def generate_recipe(data: Prompt) -> RecipeCreate:
        try:
            agent = ai or recipe_agent(settings)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
        try:
            return await draft_recipe(agent, get_household(), data.prompt)
        except Exception:
            raise HTTPException(502, "AI draft unavailable; no recipe has been saved") from None

    @app.post("/api/ai/plan")
    async def generate_plan(data: PlanRequest) -> PlanDraft:
        if not service.list_recipes():
            raise HTTPException(422, "Save a recipe before requesting a meal plan")
        try:
            planner = build_planner(settings)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None
        try:
            return await recommend_plan(
                planner, get_household(), service.list_recipes(), data.start, data.days
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502, "AI plan unavailable; no meals have been saved") from None

    @app.get("/api/integrations")
    def integrations():
        return {
            "oda": {
                "status": "setup_required",
                "mcp_url": "https://oda.com/mcp",
                "checkout_url": "https://oda.com/no/cart/",
                "message": "Connect Oda in your AI app alongside Kitchen. Review and pay in Oda.",
            },
            "ai": {"configured": bool(ai or (settings.ai_model and settings.anthropic_api_key))},
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
