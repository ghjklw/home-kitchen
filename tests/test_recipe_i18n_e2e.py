"""End-to-end i18n workflow tests across URL import, identity mapping, translation, planning, shopping, and cooking."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from home_kitchen import imports
from home_kitchen.app import create_app
from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.models import (
    Meal,
    Recipe,
    RecipeTranslation,
    RegularItem,
    ShoppingItem,
)
from home_kitchen.recipes.translation import MockRecipeTranslator, RecipeTranslationPayload
from home_kitchen.service import KitchenService
from home_kitchen.settings import Settings


class E2EIdentityRepository:
    def __init__(self):
        self.products: dict[tuple[str, str], CanonicalProduct] = {}
        self.units: dict[str, CanonicalUnit] = {}
        self.aliases: dict[tuple[str, str, str, str], set[UUID]] = {}
        self.reviewed: set[tuple[str, str, str, str]] = set()

    def product(
        self, name: str, variant: str = "", identity_id: UUID | None = None
    ) -> CanonicalProduct:
        key = (name, variant)
        if key not in self.products:
            self.products[key] = CanonicalProduct(
                id=identity_id or uuid4(), canonical_name=name, variant=variant
            )
        return self.products[key]

    def unit(self, name: str, identity_id: UUID | None = None) -> CanonicalUnit:
        if name not in self.units:
            self.units[name] = CanonicalUnit(id=identity_id or uuid4(), canonical_name=name)
        return self.units[name]

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        key = (kind, locale, context, " ".join(alias.split()).casefold())
        self.aliases.setdefault(key, set()).add(identity_id)

    def correct_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        **kwargs: Any,
    ) -> None:
        key = (kind, locale, context, " ".join(alias.split()).casefold())
        self.aliases[key] = {identity_id}
        self.reviewed.add(key)

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        normalized = " ".join(alias.split()).casefold()
        ids = self.aliases.get((kind, locale, context, normalized), set()) | self.aliases.get(
            (kind, locale, "", normalized), set()
        )
        pool = list(self.products.values()) if kind == "product" else list(self.units.values())
        return [item for item in pool if item.id in ids]

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        if not ids:
            return
        pool = self.products.values() if kind == "product" else self.units.values()
        existing = {item.id for item in pool}
        if not ids <= existing:
            raise ValueError(f"Unknown canonical {kind} identity")


class E2EStore:
    def __init__(self):
        self.recipes: dict[UUID, Recipe] = {}
        self.meals: dict[UUID, Meal] = {}
        self.items: dict[UUID, ShoppingItem] = {}
        self.regulars: dict[UUID, RegularItem] = {}
        self.confirmations: dict[UUID, UUID] = {}
        self.translations: list[RecipeTranslation] = []
        self.settings: dict[str, str] = {}
        self.photo_household_id = None
        self.engine = None

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def ensure_setting(self, key: str, value: str) -> str:
        if key not in self.settings:
            self.settings[key] = value
        return self.settings[key]

    def save(self, kind: str, entity: Any) -> None:
        if kind == "recipe":
            self.recipes[entity.id] = entity
        elif kind == "meal":
            self.meals[entity.id] = entity
        elif kind == "item":
            self.items[entity.id] = entity
        elif kind == "regular":
            self.regulars[entity.id] = entity

    def update_meal(self, meal: Meal) -> None:
        self.meals[meal.id] = meal

    def get(self, kind: str, entity_id: UUID, model: type) -> Any:
        bucket = {
            "recipe": self.recipes,
            "meal": self.meals,
            "item": self.items,
            "regular": self.regulars,
        }.get(kind, {})
        if entity_id not in bucket:
            raise KeyError(str(entity_id))
        return bucket[entity_id]

    def list(self, kind: str, model: type) -> list[Any]:
        bucket = {
            "recipe": self.recipes,
            "meal": self.meals,
            "item": self.items,
            "regular": self.regulars,
        }.get(kind, {})
        return list(bucket.values())

    def delete(self, kind: str, entity_id: UUID) -> None:
        if kind == "recipe":
            self.recipes.pop(entity_id, None)
            self.translations = [t for t in self.translations if t.recipe_id != entity_id]
        elif kind == "meal":
            self.meals.pop(entity_id, None)
        elif kind == "item":
            self.items.pop(entity_id, None)

    def confirm(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        self.items[item.id] = item
        self.confirmations[regular_id] = item.id
        return item

    def active_confirmations(self) -> dict[UUID, UUID]:
        return dict(self.confirmations)

    def save_recipe_translation(self, translation: RecipeTranslation) -> None:
        self.translations = [
            t
            for t in self.translations
            if not (
                t.recipe_id == translation.recipe_id
                and t.target_locale == translation.target_locale
                and t.recipe_version == translation.recipe_version
            )
        ]
        self.translations.append(translation)

    def get_recipe_translation(
        self, recipe_id: UUID, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        matches = [
            t
            for t in self.translations
            if t.recipe_id == recipe_id and t.target_locale == target_locale
        ]
        if recipe_version is not None:
            matches = [t for t in matches if t.recipe_version == recipe_version]
        return matches[-1] if matches else None

    def list_recipe_translations(self, recipe_id: UUID) -> list[RecipeTranslation]:
        return [t for t in reversed(self.translations) if t.recipe_id == recipe_id]


class FailingOnLocaleTranslator:
    def __init__(self, delegate: MockRecipeTranslator, fail_locales: set[str]):
        self.delegate = delegate
        self.fail_locales = fail_locales

    @property
    def call_count(self) -> int:
        return self.delegate.call_count

    async def translate(self, recipe: Recipe, target_locale: str) -> RecipeTranslationPayload:
        if target_locale.casefold() in self.fail_locales:
            raise RuntimeError(f"Simulated translation failure for {target_locale}")
        return await self.delegate.translate(recipe, target_locale)


def _create_sqlite_shopping_tables(db_path) -> Any:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE kitchen_inventory (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    normalized_unit TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Other',
                    note TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE kitchen_trip_source_closures (
                    week_start DATE NOT NULL,
                    week_end DATE NOT NULL,
                    need_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE kitchen_store_trip (
                    week_start DATE NOT NULL,
                    week_end DATE NOT NULL,
                    need_id TEXT NOT NULL,
                    checked_quantity TEXT NOT NULL
                )
                """
            )
        )
    return engine


def _html_page(recipe_json: dict[str, Any]) -> str:
    return (
        "<!doctype html><html><head><script type='application/ld+json'>"
        + json.dumps(recipe_json)
        + "</script></head><body></body></html>"
    )


@pytest.mark.asyncio
async def test_non_norwegian_recipe_i18n_end_to_end(monkeypatch, tmp_path):
    french_url = "https://www.marmiton.org/recettes/gratin-dauphinois/"
    ambiguous_url = "https://www.marmiton.org/recettes/plat/"

    french_html = _html_page(
        {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": "Gratin dauphinois maison",
            "recipeYield": "4 personnes",
            "recipeIngredient": [
                "1000 g de pommes de terre",
                "4 dl de crème fraîche",
                "2 pincée noix de muscade",
            ],
            "recipeInstructions": [
                {
                    "@type": "HowToStep",
                    "text": "Éplucher les pommes de terre et couper en fines rondelles.",
                },
                {
                    "@type": "HowToStep",
                    "text": "Ajouter la crème fraîche et cuire au four pendant une heure.",
                },
            ],
        }
    )
    ambiguous_html = _html_page(
        {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": "123",
            "recipeYield": "2",
            "recipeIngredient": ["1 foo"],
            "recipeInstructions": [{"@type": "HowToStep", "text": "bar"}],
        }
    )

    real_async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == french_url:
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, text=french_html
            )
        if str(request.url) == ambiguous_url:
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, text=ambiguous_html
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        imports.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    store = E2EStore()
    identity_repo = E2EIdentityRepository()
    identities = CanonicalIdentityService(identity_repo)
    mock_translator = MockRecipeTranslator()
    translator = FailingOnLocaleTranslator(mock_translator, fail_locales={"en"})
    service = KitchenService(store=store, identity_service=identities, translator=translator)

    app = create_app(Settings(local_dev=True), service=service)
    sqlite_engine = _create_sqlite_shopping_tables(tmp_path / "shopping.db")
    app.state.features.inventory.engine = sqlite_engine
    app.state.features.store_trip.engine = sqlite_engine

    try:
        client = TestClient(app, client=("127.0.0.1", 5000))

        # 1. Household locale selection persists
        hh_res = client.put("/api/household", json={"name": "Familien", "language": "en"})
        assert hh_res.status_code == 200
        assert hh_res.json()["language"] == "en"
        hh_get = client.get("/api/household")
        assert hh_get.status_code == 200
        assert hh_get.json()["language"] == "en"

        # 2. URL import detects source_language='fr' before save
        draft_res = client.post("/api/recipe-drafts/web", json={"url": french_url})
        assert draft_res.status_code == 200
        draft = draft_res.json()
        assert draft["provider"] == "web"
        assert draft["imported"]["source_language"] == "fr"
        assert draft["imported"]["title"] == "Gratin dauphinois maison"

        # 3. Save imported recipe preserving original French text and unmapped unit
        save_res = client.post(
            "/api/recipes",
            json={
                "title": draft["imported"]["title"],
                "servings": draft["imported"]["servings"],
                "source": "imported",
                "source_url": french_url,
                "source_language": draft["imported"]["source_language"],
                "ingredients": [
                    {
                        "name": "pommes de terre",
                        "quantity": "1000",
                        "unit": "g",
                        "category": "Produce",
                    },
                    {
                        "name": "crème fraîche",
                        "quantity": "4",
                        "unit": "dl",
                        "category": "Dairy",
                    },
                    {
                        "name": "noix de muscade",
                        "quantity": "2",
                        "unit": "pincée",
                        "category": "Spices",
                    },
                ],
                "instructions": draft["imported"]["instructions"],
            },
        )
        assert save_res.status_code == 201
        recipe = save_res.json()
        recipe_id = recipe["id"]
        assert recipe["source_language"] == "fr"
        assert recipe["ingredients"][2]["unit"] == "pincée"

        # 4. Identity mapping via unmapped-ingredients endpoint, keeping unmapped unit intact
        unmapped = client.get(
            "/api/catalogue/unmapped-ingredients", params={"recipe_id": recipe_id}
        )
        assert unmapped.status_code == 200
        assert [row["name"] for row in unmapped.json()] == [
            "pommes de terre",
            "crème fraîche",
            "noix de muscade",
        ]

        potato = client.post(
            "/api/catalogue/identities/products", json={"canonical_name": "potato"}
        ).json()
        cream = client.post(
            "/api/catalogue/identities/products", json={"canonical_name": "creme fraiche"}
        ).json()

        map_potato = client.post(
            "/api/catalogue/unmapped-ingredients/map",
            json={
                "recipe_id": recipe_id,
                "ingredient_index": 0,
                "canonical_product_id": potato["id"],
                "add_alias": True,
                "locale": "fr",
            },
        )
        assert map_potato.status_code == 200
        assert map_potato.json()["ingredients"][0]["canonical_product_id"] == potato["id"]

        map_cream = client.post(
            "/api/catalogue/unmapped-ingredients/map",
            json={
                "recipe_id": recipe_id,
                "ingredient_index": 1,
                "canonical_product_id": cream["id"],
                "add_alias": True,
                "locale": "fr",
            },
        )
        assert map_cream.status_code == 200

        # Alias is scoped to 'fr' and does not leak to 'nb'
        assert identities.resolve("product", "fr", "recipe", "pommes de terre").status == "resolved"
        assert identities.resolve("product", "nb", "recipe", "pommes de terre").status == "unmapped"

        # Reopen recipe: unmapped ingredient & unmapped unit 'pincée' remain intact
        reopened_recipe = client.get(f"/api/recipes/{recipe_id}").json()
        assert reopened_recipe["title"] == "Gratin dauphinois maison"
        assert reopened_recipe["ingredients"][0]["canonical_product_id"] == potato["id"]
        assert reopened_recipe["ingredients"][2]["canonical_product_id"] is None
        assert reopened_recipe["ingredients"][2]["canonical_unit_id"] is None
        assert reopened_recipe["ingredients"][2]["unit"] == "pincée"

        # 5. On-demand translation -> save & reopen (cached without extra translator calls)
        tr_res = client.post(f"/api/recipes/{recipe_id}/translate", json={"target_locale": "nb"})
        assert tr_res.status_code == 200
        tr = tr_res.json()
        assert tr["target_locale"] == "nb"
        assert tr["title"] == "[nb] Gratin dauphinois maison"
        assert tr["ingredients"][2]["quantity"] == "2"
        assert tr["ingredients"][2]["unit"] == "pincée"
        assert translator.call_count == 1

        reopened_tr = client.get(f"/api/recipes/{recipe_id}/translations/nb")
        assert reopened_tr.status_code == 200
        assert reopened_tr.json()["title"] == tr["title"]
        retranslate = client.post(
            f"/api/recipes/{recipe_id}/translate", json={"target_locale": "nb"}
        )
        assert retranslate.status_code == 200
        assert translator.call_count == 1

        # 6. Failed translation fallback: raises error without corrupting saved recipe/translations
        with pytest.raises(RuntimeError, match="Simulated translation failure"):
            client.post(f"/api/recipes/{recipe_id}/translate", json={"target_locale": "en"})
        assert client.get(f"/api/recipes/{recipe_id}/translations/en").status_code == 404
        assert client.get(f"/api/recipes/{recipe_id}/translations/nb").status_code == 200
        assert client.get(f"/api/recipes/{recipe_id}").json()["title"] == "Gratin dauphinois maison"

        # Unconfigured AI translator returns 503 cleanly without saving
        service_no_ai = KitchenService(
            store=store,
            identity_service=identities,
            translator=None,
            settings=Settings(local_dev=True),
        )
        app_no_ai = create_app(Settings(local_dev=True), service=service_no_ai)
        client_no_ai = TestClient(app_no_ai, client=("127.0.0.1", 5000))
        no_ai_res = client_no_ai.post(
            f"/api/recipes/{recipe_id}/translate", json={"target_locale": "en"}
        )
        assert no_ai_res.status_code == 503
        assert client_no_ai.get(f"/api/recipes/{recipe_id}/translations/en").status_code == 404

        # 7. Plan -> Shopping -> Store Trip -> Cooking (servings scaled 4 -> 6, unmapped unit preserved)
        meal_res = client.post(
            "/api/meals",
            json={
                "date": "2026-11-18",
                "label": "Middag",
                "scheduled_time": "18:30",
                "recipe_id": recipe_id,
                "servings": 6,
            },
        )
        assert meal_res.status_code == 201
        meal_id = meal_res.json()["id"]

        shopping = client.get("/api/shopping", params={"start": "2026-11-16", "end": "2026-11-22"})
        assert shopping.status_code == 200
        lines_by_name = {line["name"]: line for line in shopping.json()["lines"]}
        assert Decimal(lines_by_name["pommes de terre"]["quantity"]) == Decimal("1500")
        assert lines_by_name["pommes de terre"]["unit"] == "g"
        assert lines_by_name["pommes de terre"]["category"] == "Produce"
        assert Decimal(lines_by_name["crème fraîche"]["quantity"]) == Decimal("600")
        assert lines_by_name["crème fraîche"]["unit"] == "ml"
        assert Decimal(lines_by_name["noix de muscade"]["quantity"]) == Decimal("3")
        assert lines_by_name["noix de muscade"]["unit"] == "pincée"

        trip = client.get("/api/store-trip", params={"start": "2026-11-16", "end": "2026-11-22"})
        assert trip.status_code == 200
        trip_lines = {line["name"]: line for line in trip.json()["lines"]}
        assert trip_lines["noix de muscade"]["unit"] == "pincée"
        assert Decimal(trip_lines["noix de muscade"]["to_buy_quantity"]) == Decimal("3")

        cooking = client.get(f"/api/meals/{meal_id}/recipe")
        assert cooking.status_code == 200
        cooking_data = cooking.json()
        assert cooking_data["servings"] == 6
        assert cooking_data["source_recipe"]["source_language"] == "fr"
        assert cooking_data["source_recipe"]["title"] == "Gratin dauphinois maison"
        cooking_ing = {item["name"]: item for item in cooking_data["ingredients"]}
        assert Decimal(cooking_ing["pommes de terre"]["quantity"]) == Decimal("1500")
        assert Decimal(cooking_ing["crème fraîche"]["quantity"]) == Decimal("6")
        assert Decimal(cooking_ing["noix de muscade"]["quantity"]) == Decimal("3")
        assert cooking_ing["noix de muscade"]["unit"] == "pincée"

        # 8. Undetermined language ('und') URL import, save, translation, and correction
        und_draft = client.post("/api/recipe-drafts/web", json={"url": ambiguous_url})
        assert und_draft.status_code == 200
        assert und_draft.json()["imported"]["source_language"] == "und"

        und_save = client.post(
            "/api/recipes",
            json={
                "title": "123",
                "servings": 2,
                "ingredients": [{"name": "foo", "quantity": "1", "unit": "cuillère"}],
                "instructions": ["bar"],
            },
        )
        assert und_save.status_code == 201
        und_recipe = und_save.json()
        assert und_recipe["source_language"] == "und"
        assert und_recipe["ingredients"][0]["unit"] == "cuillère"

        und_tr = client.post(
            f"/api/recipes/{und_recipe['id']}/translate", json={"target_locale": "nb"}
        )
        assert und_tr.status_code == 200
        assert und_tr.json()["title"] == "[nb] 123"
    finally:
        sqlite_engine.dispose()
