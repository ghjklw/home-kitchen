"""End-to-end offline connected journey test: find -> adapt -> plan -> shop -> cook -> feedback."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from home_kitchen import imports
from home_kitchen.app import create_app
from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.context import history_context
from home_kitchen.history import HistoryEntry, HistoryEntryCreate
from home_kitchen.imports import ImportedDraft
from home_kitchen.models import (
    Meal,
    Recipe,
    RecipeActivityStats,
    RecipeType,
    RegularItem,
    ShoppingItem,
)
from home_kitchen.recipes.discovery import (
    MarmitonDiscoveryProvider,
    RecipeDiscoveryRegistry,
)
from home_kitchen.recipes.models import DraftIngredient, EditableRecipeDraft, SourceIngredient
from home_kitchen.service import KitchenService
from home_kitchen.settings import Settings

pytestmark = pytest.mark.mocked_provider


class _MemoryIdentityRepo:
    def __init__(self) -> None:
        self.products: dict[tuple[str, str], CanonicalProduct] = {}
        self.units: dict[str, CanonicalUnit] = {}
        self.aliases: dict[tuple[str, str, str, str], set[UUID]] = {}

    def product(self, name: str, variant: str) -> CanonicalProduct:
        return self.products.setdefault(
            (name, variant), CanonicalProduct(id=uuid4(), canonical_name=name, variant=variant)
        )

    def unit(self, name: str) -> CanonicalUnit:
        return self.units.setdefault(name, CanonicalUnit(id=uuid4(), canonical_name=name))

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        key = (kind, locale, context, " ".join(alias.split()).casefold())
        self.aliases.setdefault(key, set()).add(identity_id)

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        normalized = " ".join(alias.split()).casefold()
        ids = self.aliases.get((kind, locale, context, normalized), set()) | self.aliases.get(
            (kind, locale, "", normalized), set()
        )
        items = list(self.products.values()) if kind == "product" else list(self.units.values())
        return [item for item in items if item.id in ids]

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        known = {
            item.id
            for item in (self.products.values() if kind == "product" else self.units.values())
        }
        missing = ids - known
        if missing:
            raise ValueError(f"Unknown {kind} identities: {missing}")


class _InMemoryStore:
    """Offline store supporting recipes, meals, shopping, settings, and SQLite inventory/trip."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE kitchen_inventory ("
                    "id TEXT PRIMARY KEY, "
                    "name TEXT NOT NULL, "
                    "normalized_name TEXT NOT NULL, "
                    "unit TEXT NOT NULL, "
                    "normalized_unit TEXT NOT NULL, "
                    "quantity TEXT NOT NULL, "
                    "category TEXT NOT NULL, "
                    "note TEXT)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE kitchen_store_trip ("
                    "week_start DATE NOT NULL, "
                    "week_end DATE NOT NULL, "
                    "need_id TEXT NOT NULL, "
                    "checked_quantity NUMERIC NOT NULL, "
                    "updated_at TEXT, "
                    "PRIMARY KEY (week_start, week_end, need_id))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE kitchen_trip_source_closures ("
                    "week_start DATE NOT NULL, "
                    "week_end DATE NOT NULL, "
                    "need_id TEXT NOT NULL, "
                    "source_kind TEXT NOT NULL, "
                    "source_id TEXT NOT NULL, "
                    "PRIMARY KEY (week_start, week_end, need_id, source_kind, source_id))"
                )
            )
        self.recipes: dict[UUID, Recipe] = {}
        self.meals: dict[UUID, Meal] = {}
        self.items: dict[UUID, ShoppingItem] = {}
        self.regulars: dict[UUID, RegularItem] = {}
        self.settings: dict[str, str] = {}
        self.history_entries: list[HistoryEntry] = []

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def ensure_setting(self, key: str, value: str) -> str:
        return self.settings.setdefault(key, value)

    def save(self, kind: str, entity) -> None:
        if kind == "recipe":
            self.recipes[entity.id] = entity.model_copy(
                update={"activity_stats": None, "stats": None}
            )
        elif kind == "meal":
            self.meals[entity.id] = entity.model_copy(deep=True)
        elif kind == "item":
            self.items[entity.id] = entity.model_copy(deep=True)
        elif kind == "regular":
            self.regulars[entity.id] = entity.model_copy(deep=True)
        else:
            raise KeyError(kind)

    def update_meal(self, meal: Meal) -> None:
        self.meals[meal.id] = meal.model_copy(deep=True)

    def get(self, kind: str, entity_id: UUID, model):
        del model
        bucket = {
            "recipe": self.recipes,
            "meal": self.meals,
            "item": self.items,
            "regular": self.regulars,
        }[kind]
        if entity_id not in bucket:
            raise KeyError(str(entity_id))
        return bucket[entity_id].model_copy(deep=True)

    def list(self, kind: str, model):
        del model
        bucket = {
            "recipe": self.recipes,
            "meal": self.meals,
            "item": self.items,
            "regular": self.regulars,
        }[kind]
        return [item.model_copy(deep=True) for item in bucket.values()]

    def delete(self, kind: str, entity_id: UUID) -> None:
        bucket = {
            "recipe": self.recipes,
            "meal": self.meals,
            "item": self.items,
            "regular": self.regulars,
        }[kind]
        bucket.pop(entity_id, None)

    def active_confirmations(self):
        return []

    def get_recipe_activity_stats(self, recipe_id: UUID) -> RecipeActivityStats:
        return self.get_recipes_activity_stats([recipe_id]).get(recipe_id, RecipeActivityStats())

    def get_recipes_activity_stats(
        self, recipe_ids: list[UUID] | None = None
    ) -> dict[UUID, RecipeActivityStats]:
        target_ids = list(self.recipes) if recipe_ids is None else list(dict.fromkeys(recipe_ids))
        meals = list(self.meals.values())
        result: dict[UUID, RecipeActivityStats] = {}
        for recipe_id in target_ids:
            entries = [
                entry
                for entry in reversed(self.history_entries)
                if entry.recipe_id == recipe_id and entry.kind == "recipe_rating"
            ]
            ratings = [
                entry.structured["rating"]
                for entry in entries
                if isinstance(entry.structured.get("rating"), int)
                and not isinstance(entry.structured.get("rating"), bool)
            ]
            would_make = next(
                (
                    entry.structured["would_make_again"]
                    for entry in entries
                    if isinstance(entry.structured.get("would_make_again"), bool)
                ),
                None,
            )
            cooked = sum(
                1
                for entry in self.history_entries
                if entry.recipe_id == recipe_id
                and entry.kind == "domain_event"
                and entry.structured.get("event") == "meal_cooked"
            )
            ordered = sum(
                1
                for entry in self.history_entries
                if entry.recipe_id == recipe_id and entry.kind == "purchase"
            )
            result[recipe_id] = RecipeActivityStats(
                times_planned=sum(1 for m in meals if m.recipe_id == recipe_id),
                times_cooked=cooked,
                times_ordered=ordered,
                latest_rating=ratings[0] if ratings else None,
                average_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
                would_make_again=would_make,
                feedback_count=len(entries),
            )
        return result


class _InMemoryHistory:
    def __init__(self, store: _InMemoryStore) -> None:
        self.store = store
        self.engine = store.engine

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection=None,
    ) -> HistoryEntry:
        del connection
        entry = HistoryEntry(
            id=uuid4(),
            occurred_at=datetime.now(UTC),
            actor_id=actor_id or history_context.get().actor_id,
            **data.model_dump(),
        )
        self.store.history_entries.append(entry)
        return entry

    def recent(self, limit: int = 50) -> list[HistoryEntry]:
        return list(reversed(self.store.history_entries))[:limit]

    def list(self, limit: int = 30) -> list[HistoryEntry]:
        return self.recent(limit)

    def search(self, query: str, limit: int = 8, kinds=None) -> list[HistoryEntry]:
        items = list(reversed(self.store.history_entries))
        if kinds:
            kind_set = set(kinds)
            items = [entry for entry in items if entry.kind in kind_set]
        if query.strip():
            needle = query.strip().casefold()
            items = [
                entry
                for entry in items
                if needle in entry.message.casefold()
                or needle in json.dumps(entry.structured, ensure_ascii=False).casefold()
            ]
        return items[:limit]


def test_connected_recipe_to_feedback_journey_offline(monkeypatch):
    """Exercise find -> adapt -> plan -> shop -> cook -> feedback end-to-end without network."""
    store = _InMemoryStore()
    service = KitchenService(
        store=store,  # type: ignore[arg-type]
        identity_service=CanonicalIdentityService(_MemoryIdentityRepo()),
    )

    recipe_url = "https://www.marmiton.org/recettes/recette_pasta-tomat_101.aspx"
    search_html = (
        '<ul class="search-list"><li class="search-list__item">'
        '<a class="card-content__title" href="/recettes/recette_pasta-tomat_101.aspx">'
        "Kremet tomatpasta</a>"
        "</li></ul>"
    )
    discovery_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _req: httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=search_html,
            )
        ),
        trust_env=False,
    )

    async def fake_import(url: str) -> ImportedDraft:
        assert url == recipe_url
        return ImportedDraft(
            title="Pasta à la tomate",
            servings=2,
            ingredients=["200 g pasta", "100 ml fløte"],
            instructions=["Kok pasta etter anvisning.", "Vend inn saus."],
            source_url=recipe_url,
            recipe_type=RecipeType.MIDDAG,
            prep_time_minutes=20,
        )

    monkeypatch.setattr(imports, "import_recipe", fake_import)

    adapted_output = EditableRecipeDraft(
        title="Kremet tomatpasta (hverdagsutgave)",
        servings=2,
        ingredients=[
            DraftIngredient(name="Pasta", quantity=Decimal("200"), unit="g", source_indices=[0]),
            DraftIngredient(name="Fløte", quantity=Decimal("100"), unit="ml", source_indices=[1]),
        ],
        instructions=["Kok pasta i lettsaltet vann.", "Rør inn fløte og server."],
        source_url=recipe_url,
        source_ingredients=[
            SourceIngredient(index=0, text="200 g pasta"),
            SourceIngredient(index=1, text="100 ml fløte"),
        ],
        recipe_type=RecipeType.MIDDAG,
        prep_time_minutes=20,
    )
    ai_agent = Agent(TestModel(custom_output_args=adapted_output), output_type=EditableRecipeDraft)

    settings = Settings(
        local_dev=True,
        database_url="postgresql+psycopg://localhost/kitchen_test",
    )
    app = create_app(settings, ai=ai_agent, service=service)
    history = _InMemoryHistory(store)
    app.state.features.history = history  # type: ignore[assignment]
    app.state.features.recipe_feedback.history = history  # type: ignore[assignment]
    app.state.features.store_trip.history = history  # type: ignore[assignment]
    app.state.features.recipe_discovery = RecipeDiscoveryRegistry(
        [MarmitonDiscoveryProvider(http_client=discovery_client)],
        context_provider=app.state.features.discovery_context,
    )

    plan_start = date(2026, 11, 23)
    plan_end = date(2026, 11, 29)
    meal_date = date(2026, 11, 25)
    outside_start = date(2026, 12, 1)
    outside_end = date(2026, 12, 7)

    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        # 1. FIND: discovery returns bounded source-backed results without saving a recipe
        discovered = client.post(
            "/api/recipe-discovery",
            json={"query": "tomatpasta", "sources": ["marmiton"], "limit": 5},
        )
        assert discovered.status_code == 200
        discovery_body = discovered.json()
        assert len(discovery_body["results"]) == 1
        found = discovery_body["results"][0]
        assert found["source"] == "marmiton"
        assert found["url"] == recipe_url
        assert found["importable"] is True
        assert client.get("/api/recipes").json() == []

        # 2. ADAPT: AI adaptation produces an editable draft without saving until explicit confirm
        draft_resp = client.post("/api/recipe-drafts/ai", json={"url": found["url"]})
        assert draft_resp.status_code == 200
        draft = draft_resp.json()
        assert draft["requires_review"] is True
        assert draft["adapted"]["source_url"] == recipe_url
        assert draft["adapted"]["provenance"]["original_source"] == "imported"
        assert client.get("/api/recipes").json() == []

        # Explicit user confirmation saves the reviewed recipe
        save_payload = {
            "title": draft["adapted"]["title"],
            "servings": draft["adapted"]["servings"],
            "ingredients": [
                {
                    "name": item["name"],
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "source_indices": item["source_indices"],
                }
                for item in draft["adapted"]["ingredients"]
            ],
            "instructions": draft["adapted"]["instructions"],
            "source": "ai",
            "source_url": draft["adapted"]["source_url"],
            "source_ingredients": draft["adapted"]["source_ingredients"],
            "provenance": draft["adapted"]["provenance"],
            "recipe_type": draft["adapted"]["recipe_type"],
            "prep_time_minutes": draft["adapted"]["prep_time_minutes"],
        }
        saved_resp = client.post("/api/recipes", json=save_payload)
        assert saved_resp.status_code == 201
        saved_recipe = saved_resp.json()
        recipe_id = saved_recipe["id"]
        assert saved_recipe["source"] == "ai"
        assert saved_recipe["source_url"] == recipe_url
        assert saved_recipe["provenance"]["original_source"] == "imported"
        assert saved_recipe["servings"] == 2

        # 3. PLAN: schedule the recipe for 5 servings within the planning period
        meal_resp = client.post(
            "/api/meals",
            json={"date": meal_date.isoformat(), "recipe_id": recipe_id, "servings": 5},
        )
        assert meal_resp.status_code == 201
        meal = meal_resp.json()
        assert meal["servings"] == 5

        in_period_meals = client.get(
            "/api/meals",
            params={"start": plan_start.isoformat(), "end": plan_end.isoformat()},
        ).json()
        assert [m["id"] for m in in_period_meals] == [meal["id"]]
        out_period_meals = client.get(
            "/api/meals",
            params={"start": outside_start.isoformat(), "end": outside_end.isoformat()},
        ).json()
        assert out_period_meals == []

        # Planning alone increments times_planned only, never times_cooked
        pre_cook_stats = client.get(f"/api/recipes/{recipe_id}/stats").json()
        assert pre_cook_stats["times_planned"] == 1
        assert pre_cook_stats["times_cooked"] == 0

        # 4. SHOP: shopping list and purchase plan scale 5/2 = 2.5x for the planning period only
        shopping = client.get(
            "/api/shopping",
            params={"start": plan_start.isoformat(), "end": plan_end.isoformat()},
        ).json()
        shopping_by_name = {line["name"]: line for line in shopping["lines"]}
        assert Decimal(shopping_by_name["Pasta"]["quantity"]) == Decimal("500")
        assert shopping_by_name["Pasta"]["unit"] == "g"
        assert Decimal(shopping_by_name["Fløte"]["quantity"]) == Decimal("250")
        assert shopping_by_name["Fløte"]["unit"] == "ml"

        purchase_plan = client.get(
            "/api/purchase-plan",
            params={"start": plan_start.isoformat(), "end": plan_end.isoformat()},
        ).json()
        plan_by_name = {line["name"]: line for line in purchase_plan["lines"]}
        assert Decimal(plan_by_name["Pasta"]["to_buy_quantity"]) == Decimal("500")
        assert Decimal(plan_by_name["Fløte"]["to_buy_quantity"]) == Decimal("250")

        store_trip = client.get(
            "/api/store-trip",
            params={"start": plan_start.isoformat(), "end": plan_end.isoformat()},
        ).json()
        trip_by_name = {line["name"]: line for line in store_trip["lines"]}
        assert Decimal(trip_by_name["Pasta"]["to_buy_quantity"]) == Decimal("500")
        assert trip_by_name["Pasta"]["checked"] is False

        outside_shopping = client.get(
            "/api/shopping",
            params={"start": outside_start.isoformat(), "end": outside_end.isoformat()},
        ).json()
        assert outside_shopping["lines"] == []

        # 5. COOK: planned cooking view scales to 5 servings while keeping saved recipe at 2
        cook_resp = client.get(f"/api/meals/{meal['id']}/recipe")
        assert cook_resp.status_code == 200
        cook_view = cook_resp.json()
        assert cook_view["servings"] == 5
        cook_by_name = {item["name"]: item for item in cook_view["ingredients"]}
        assert Decimal(cook_by_name["Pasta"]["quantity"]) == Decimal("500")
        assert Decimal(cook_by_name["Fløte"]["quantity"]) == Decimal("250")
        assert cook_view["source_recipe"]["id"] == recipe_id
        assert cook_view["source_recipe"]["servings"] == 2
        assert cook_view["source_recipe"]["source"] == "ai"
        assert cook_view["source_recipe"]["provenance"]["original_source"] == "imported"
        assert Decimal(cook_view["source_recipe"]["ingredients"][0]["quantity"]) == Decimal("200")

        # 6. FEEDBACK: quick 1-click feedback + detailed note persist for future discovery/planning
        quick_star = client.post(f"/api/recipes/{recipe_id}/feedback", json={"rating": 5})
        assert quick_star.status_code == 201
        quick_thumb = client.post(
            f"/api/recipes/{recipe_id}/feedback", json={"would_make_again": True}
        )
        assert quick_thumb.status_code == 201
        detailed = client.post(
            f"/api/recipes/{recipe_id}/feedback",
            json={"rating": 4, "would_make_again": True, "notes": "Passer fint til 5 porsjoner"},
        )
        assert detailed.status_code == 201

        stats = client.get(f"/api/recipes/{recipe_id}/stats").json()
        assert stats == {
            "times_planned": 1,
            "times_cooked": 0,
            "times_ordered": 0,
            "latest_rating": 4,
            "average_rating": 4.5,
            "would_make_again": True,
            "feedback_count": 3,
        }

        recipes_with_stats = client.get("/api/recipes", params={"include_stats": "true"}).json()
        assert recipes_with_stats[0]["id"] == recipe_id
        assert recipes_with_stats[0]["activity_stats"] == stats

        interactions = client.get("/api/interactions", params={"limit": 20}).json()
        recipe_feedback_entries = [
            entry
            for entry in interactions
            if entry["kind"] == "recipe_rating" and entry["recipe_id"] == recipe_id
        ]
        assert len(recipe_feedback_entries) == 3
        assert any(
            entry["structured"].get("notes") == "Passer fint til 5 porsjoner"
            for entry in recipe_feedback_entries
        )

        # Stored feedback surfaces in recommendation evidence and discovery context
        evidence = app.state.features.evidence("tomatpasta")
        assert any("Passer fint til 5 porsjoner" in snippet.text for snippet in evidence)
        ctx = app.state.features.discovery_context()
        assert "Kremet tomatpasta (hverdagsutgave)" in ctx.likes
