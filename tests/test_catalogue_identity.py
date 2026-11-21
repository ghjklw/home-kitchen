"""Canonical identity reuse and conservative alias resolution."""

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from home_kitchen.catalogue.identity import (
    AliasResolution,
    AliasSourceType,
    CanonicalProduct,
    CanonicalUnit,
)
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.models import Ingredient, Recipe, RecipeCreate
from home_kitchen.service import KitchenService


class MemoryIdentityRepository:
    def __init__(self):
        self.products: dict[tuple[str, str], CanonicalProduct] = {}
        self.units: dict[str, CanonicalUnit] = {}
        self.aliases: dict[tuple[str, str, str, str], set[UUID]] = {}
        self.reviewed: set[tuple[str, str, str, str]] = set()
        self.alias_source_types: dict[tuple[str, str, str, str, UUID], AliasSourceType] = {}

    def product(self, name: str, variant: str) -> CanonicalProduct:
        return self.products.setdefault(
            (name, variant), CanonicalProduct(id=uuid4(), canonical_name=name, variant=variant)
        )

    def unit(self, name: str) -> CanonicalUnit:
        return self.units.setdefault(name, CanonicalUnit(id=uuid4(), canonical_name=name))

    def add_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        household_id: UUID | None = None,
        source_type: AliasSourceType = "automatic_suggestion",
    ) -> None:
        del household_id
        normalized = " ".join(alias.split()).casefold()
        key = kind, locale, context, normalized
        self.aliases.setdefault(key, set()).add(identity_id)
        self.alias_source_types[(kind, locale, context, normalized, identity_id)] = source_type

    def correct_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        actor_id: str = "system",
        household_id: UUID | None = None,
        channel: str = "api",
        source_type: AliasSourceType = "reviewed_correction",
    ) -> None:
        del actor_id, household_id, channel
        normalized = " ".join(alias.split()).casefold()
        key = kind, locale, context, normalized
        self.aliases[key] = {identity_id}
        self.reviewed.add(key)
        self.alias_source_types[(kind, locale, context, normalized, identity_id)] = source_type

    def resolve_with_metadata(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> AliasResolution:
        del household_id
        normalized = " ".join(alias.split()).casefold()
        ids = self.aliases.get((kind, locale, context, normalized), set()) | self.aliases.get(
            (kind, locale, "", normalized), set()
        )
        identities = (
            list(self.products.values()) if kind == "product" else list(self.units.values())
        )
        candidates = [identity for identity in identities if identity.id in ids]
        if not candidates:
            return AliasResolution(status="unmapped", candidates=[])
        source_types: list[AliasSourceType] = []
        for candidate in candidates:
            for ctx in (context, ""):
                st = self.alias_source_types.get((kind, locale, ctx, normalized, candidate.id))
                if st and st not in source_types:
                    source_types.append(st)
        if not source_types:
            source_types = ["automatic_suggestion"]
        status = "resolved" if len(candidates) == 1 else "ambiguous"
        is_rev = (kind, locale, context, normalized) in self.reviewed or (
            kind,
            locale,
            "",
            normalized,
        ) in self.reviewed
        return AliasResolution(
            status=status,
            candidates=candidates,
            source_type=source_types[0] if len(source_types) == 1 else None,
            source_types=source_types,
            reviewed=is_rev if status == "resolved" else None,
        )

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        return self.resolve_with_metadata(kind, locale, context, alias).candidates

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        if not ids:
            return
        if kind == "product":
            existing = {p.id for p in self.products.values()}
        elif kind == "unit":
            existing = {u.id for u in self.units.values()}
        else:
            raise ValueError("kind must be product or unit")
        if not ids <= existing:
            raise ValueError(f"Recipe contains an unknown canonical {kind} identity")


class MemoryStore:
    def __init__(self):
        self.recipes: dict[UUID, Recipe] = {}
        self.settings: dict[str, str] = {}
        self.photo_household_id = None
        self.engine = None

    def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    def save(self, kind: str, entity: Any) -> None:
        if kind == "recipe":
            self.recipes[entity.id] = entity

    def get(self, kind: str, entity_id: UUID, model: type) -> Any:
        if kind == "recipe":
            if entity_id not in self.recipes:
                raise KeyError(str(entity_id))
            return self.recipes[entity_id]
        raise KeyError(str(entity_id))

    def list(self, kind: str, model: type) -> list[Any]:
        if kind == "recipe":
            return list(self.recipes.values())
        return []


def test_product_identities_reuse_exact_canonical_variant_and_keep_variants_distinct():
    service = CanonicalIdentityService(MemoryIdentityRepository())
    first = service.create_product("  Red Tomato ")
    reused = service.create_product("red   tomato")
    distinct = service.create_product("red tomato", "canned")
    assert first.id == reused.id
    assert distinct.id != first.id


def test_aliases_are_language_and_context_scoped_and_ambiguity_needs_reviewed_correction():
    repository = MemoryIdentityRepository()
    service = CanonicalIdentityService(repository)
    fresh = service.create_product("tomato", "fresh")
    tinned = service.create_product("tomato", "tinned")
    service.add_alias("product", "nb", "recipe", "tomat", fresh.id)
    service.add_alias("product", "nb", "recipe", "tomat", tinned.id)

    ambiguous_res = service.resolve("product", "nb", "recipe", "tomat")
    assert ambiguous_res.status == "ambiguous"
    assert ambiguous_res.source_type == "automatic_suggestion"
    assert ambiguous_res.source_types == ["automatic_suggestion"]
    unmapped_res = service.resolve("product", "en", "recipe", "tomat")
    assert unmapped_res.status == "unmapped"
    assert unmapped_res.source_type is None
    corrected = service.correct_alias("product", "nb", "recipe", "tomat", fresh.id)
    assert corrected is None
    result = service.resolve("product", "nb", "recipe", " TOMAT ")
    assert result.status == "resolved"
    assert result.candidates == [fresh]
    assert result.source_type == "reviewed_correction"
    assert result.reviewed is True
    assert ("product", "nb", "recipe", "tomat") in repository.reviewed


def test_unmapped_legacy_ingredient_remains_valid_and_source_quantity_is_unchanged():
    from decimal import Decimal

    from home_kitchen.models import RecipeCreate

    recipe = RecipeCreate.model_validate(
        {
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {"name": "my grandmother's odd ingredient", "quantity": "1.25", "unit": "handfuls"}
            ],
            "instructions": ["Cook"],
        }
    )
    assert recipe.ingredients[0].name == "my grandmother's odd ingredient"
    assert recipe.ingredients[0].quantity == Decimal("1.25")
    assert recipe.ingredients[0].unit == "handfuls"


def test_old_client_update_preserves_exact_unchanged_unique_ingredient_subdocument():
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    product, unit = uuid4(), uuid4()
    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "canonical_product_id": product,
                    "canonical_unit_id": unit,
                    "source_unit_expression": "Teaspoons",
                },
            ],
            "instructions": ["Cook"],
        }
    )
    old_payload = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [{"name": "pepper", "quantity": "1", "unit": "tsp"}],
            "instructions": ["Cook"],
        }
    )
    updated = KitchenService._retain_omitted_ingredient_identity_fields(
        old_payload, existing, [item.model_dump() for item in old_payload.ingredients]
    )
    assert updated[0]["canonical_product_id"] == product
    assert updated[0]["canonical_unit_id"] == unit
    assert updated[0]["source_unit_expression"] == "Teaspoons"


def test_old_client_update_signature_supports_nested_purchase_estimates():
    from home_kitchen.models import PurchaseEstimate, Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    product, unit = uuid4(), uuid4()
    estimate = PurchaseEstimate(
        quantity="100", unit="g", approximate=False, provenance="source estimate"
    )
    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "purchase_estimate": estimate.model_dump(),
                    "canonical_product_id": product,
                    "canonical_unit_id": unit,
                    "source_unit_expression": "Teaspoons",
                }
            ],
            "instructions": ["Cook"],
        }
    )
    old_payload = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "purchase_estimate": estimate.model_dump(),
                }
            ],
            "instructions": ["Cook"],
        }
    )
    current_values = [item.model_dump() for item in old_payload.ingredients]
    updated = KitchenService._retain_omitted_ingredient_identity_fields(
        old_payload, existing, current_values
    )
    assert updated[0]["canonical_product_id"] == product
    assert updated[0]["canonical_unit_id"] == unit


def test_unmapped_quantity_edit_keeps_distinct_source_unit_wording_without_blocking_changes():
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tbsp",
                    "source_unit_expression": "Tablespoons",
                }
            ],
            "instructions": ["Cook"],
        }
    )
    quantity_edit = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [{"name": "pepper", "quantity": "2", "unit": "tbsp"}],
            "instructions": ["Cook"],
        }
    )
    quantity_values = [item.model_dump() for item in quantity_edit.ingredients]
    quantity_result = KitchenService._retain_omitted_ingredient_identity_fields(
        quantity_edit, existing, quantity_values
    )
    assert quantity_result[0]["quantity"] == 2
    assert quantity_result[0]["source_unit_expression"] == "Tablespoons"

    measure_edit = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": [{"name": "pepper", "quantity": "30", "unit": "ml"}],
            "instructions": ["Cook"],
        }
    )
    measure_values = [item.model_dump() for item in measure_edit.ingredients]
    measure_result = KitchenService._retain_omitted_ingredient_identity_fields(
        measure_edit, existing, measure_values
    )
    assert measure_result[0]["quantity"] == 30
    assert measure_result[0]["unit"] == "ml"
    assert measure_result[0]["source_unit_expression"] == "ml"


def test_title_only_update_preserves_source_wording_for_distinct_exact_rows():
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tbsp",
                    "source_unit_expression": "Tablespoons",
                },
                {
                    "name": "pepper",
                    "quantity": "2",
                    "unit": "tbsp",
                    "source_unit_expression": "spiseskjeer",
                },
            ],
            "instructions": ["Cook"],
        }
    )
    title_only = RecipeCreate.model_validate(
        {
            "title": "New soup title",
            "servings": 2,
            "ingredients": [
                {"name": "pepper", "quantity": "1", "unit": "tbsp"},
                {"name": "pepper", "quantity": "2", "unit": "tbsp"},
            ],
            "instructions": ["Cook"],
        }
    )
    updated = KitchenService._retain_omitted_ingredient_identity_fields(
        title_only, existing, [item.model_dump() for item in title_only.ingredients]
    )
    assert [item["source_unit_expression"] for item in updated] == [
        "Tablespoons",
        "spiseskjeer",
    ]


@pytest.mark.parametrize(
    "incoming_ingredients",
    [
        [
            {"name": "pepper", "quantity": "2", "unit": "tsp"},
            {"name": "salt", "quantity": "1", "unit": "tsp"},
        ],
        [
            {"name": "salt", "quantity": "1", "unit": "tsp"},
            {"name": "pepper", "quantity": "1", "unit": "tsp"},
        ],
        [
            {"name": "pepper", "quantity": "1", "unit": "tsp"},
            {"name": "pepper", "quantity": "1", "unit": "tsp"},
        ],
    ],
    ids=["changed", "reordered", "ambiguous-duplicate"],
)
def test_old_client_update_rejects_changed_reordered_or_ambiguous_rows(incoming_ingredients):
    from home_kitchen.models import Recipe, RecipeCreate
    from home_kitchen.service import KitchenService

    product, unit = uuid4(), uuid4()
    existing = Recipe.model_validate(
        {
            "id": uuid4(),
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {
                    "name": "pepper",
                    "quantity": "1",
                    "unit": "tsp",
                    "canonical_product_id": product,
                    "canonical_unit_id": unit,
                    "source_unit_expression": "Teaspoons",
                },
                {"name": "salt", "quantity": "1", "unit": "tsp"},
            ],
            "instructions": ["Cook"],
        }
    )
    old_payload = RecipeCreate.model_validate(
        {
            "title": "Soup edited",
            "servings": 2,
            "ingredients": incoming_ingredients,
            "instructions": ["Cook"],
        }
    )
    with pytest.raises(ValueError, match="include identity metadata"):
        KitchenService._retain_omitted_ingredient_identity_fields(
            old_payload, existing, [item.model_dump() for item in old_payload.ingredients]
        )


def test_automatic_alias_resolution_at_recipe_creation():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    tomato = identities.create_product("Tomato", "fresh")
    gram = identities.create_unit("Gram")
    identities.add_alias("product", "nb", "recipe", "tomat", tomato.id)
    identities.add_alias("unit", "nb", "recipe", "grams", gram.id)

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)

    recipe = service.create_recipe(
        RecipeCreate(
            title="Pasta Sauce",
            servings=4,
            ingredients=[
                Ingredient(name="tomat", quantity="500", unit="Grams"),
                Ingredient(name="mysterious herb", quantity="1", unit="pinch"),
            ],
            instructions=["Boil", "Simmer"],
        )
    )

    # First ingredient matched known aliases:
    mapped = recipe.ingredients[0]
    assert mapped.canonical_product_id == tomato.id
    assert mapped.canonical_unit_id == gram.id
    assert mapped.name == "tomat"
    assert mapped.unit == "grams"
    assert mapped.source_unit_expression == "Grams"

    # Second ingredient has no known aliases; leaves identities safely None:
    unmapped = recipe.ingredients[1]
    assert unmapped.canonical_product_id is None
    assert unmapped.canonical_unit_id is None
    assert unmapped.name == "mysterious herb"
    assert unmapped.unit == "pinch"


def test_ambiguous_alias_and_predefined_identity_preservation():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    fresh_tomato = identities.create_product("Tomato", "fresh")
    canned_tomato = identities.create_product("Tomato", "canned")
    identities.add_alias("product", "nb", "recipe", "tomat", fresh_tomato.id)
    identities.add_alias("product", "nb", "recipe", "tomat", canned_tomato.id)

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)

    recipe = service.create_recipe(
        RecipeCreate(
            title="Soup",
            servings=2,
            ingredients=[
                Ingredient(name="tomat", quantity="2", unit="item"),
                Ingredient(
                    name="tomat",
                    quantity="1",
                    unit="item",
                    canonical_product_id=canned_tomato.id,
                ),
            ],
            instructions=["Cook"],
        )
    )

    # Ambiguous alias is not guessed:
    assert recipe.ingredients[0].canonical_product_id is None
    # Pre-existing canonical ID is preserved untouched:
    assert recipe.ingredients[1].canonical_product_id == canned_tomato.id


def test_recipe_update_auto_resolution_and_explicit_clear_preservation():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    tomato = identities.create_product("Tomato")
    onion = identities.create_product("Onion")
    gram = identities.create_unit("Gram")
    identities.add_alias("product", "nb", "recipe", "tomat", tomato.id)
    identities.add_alias("product", "nb", "recipe", "løk", onion.id)
    identities.add_alias("unit", "nb", "recipe", "g", gram.id)

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)

    recipe = service.create_recipe(
        RecipeCreate(
            title="Sauce",
            servings=2,
            ingredients=[
                Ingredient(name="tomat", quantity="200", unit="g"),
            ],
            instructions=["Cook"],
        )
    )
    assert recipe.ingredients[0].canonical_product_id == tomato.id
    assert recipe.ingredients[0].canonical_unit_id == gram.id

    # 1. User explicitly clears product identity without renaming the ingredient:
    cleared_update = RecipeCreate(
        title="Sauce edited",
        servings=2,
        ingredients=[
            Ingredient(
                name="tomat",
                quantity="200",
                unit="g",
                canonical_product_id=None,
                canonical_unit_id=gram.id,
            ),
        ],
        instructions=["Cook"],
    )
    updated = service.update_recipe(recipe.id, cleared_update)
    assert updated.ingredients[0].canonical_product_id is None
    assert updated.ingredients[0].canonical_unit_id == gram.id

    # 2. Renaming the ingredient to "løk" auto-resolves to onion:
    renamed_update = RecipeCreate(
        title="Sauce with onion",
        servings=2,
        ingredients=[
            Ingredient(
                name="løk",
                quantity="100",
                unit="g",
                canonical_product_id=None,
                canonical_unit_id=gram.id,
            ),
        ],
        instructions=["Cook"],
    )
    updated_renamed = service.update_recipe(recipe.id, renamed_update)
    assert updated_renamed.ingredients[0].canonical_product_id == onion.id
    assert updated_renamed.ingredients[0].name == "løk"


def test_list_and_map_unmapped_ingredients():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    garlic = identities.create_product("Garlic")
    clove = identities.create_unit("Clove")

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)

    recipe = service.create_recipe(
        RecipeCreate(
            title="Garlic Bread",
            servings=2,
            ingredients=[
                Ingredient(name="hvitløk", quantity="3", unit="fedd"),
            ],
            instructions=["Bake"],
        )
    )

    unmapped = service.list_unmapped_ingredients()
    assert len(unmapped) == 1
    assert unmapped[0].recipe_id == recipe.id
    assert unmapped[0].name == "hvitløk"
    assert unmapped[0].unit == "fedd"
    assert unmapped[0].canonical_product_id is None
    assert unmapped[0].canonical_unit_id is None

    # Filter by recipe_id
    assert len(service.list_unmapped_ingredients(recipe_id=recipe.id)) == 1
    with pytest.raises(KeyError):
        service.list_unmapped_ingredients(recipe_id=uuid4())

    # Map the ingredient using map_unmapped_ingredient:
    mapped_recipe = service.map_unmapped_ingredient(
        recipe_id=recipe.id,
        ingredient_index=0,
        canonical_product_id=garlic.id,
        canonical_unit_id=clove.id,
        add_alias=True,
    )
    assert mapped_recipe.ingredients[0].canonical_product_id == garlic.id
    assert mapped_recipe.ingredients[0].canonical_unit_id == clove.id
    assert (
        identities.resolve("product", "nb", "recipe", "hvitløk").source_type
        == "reviewed_correction"
    )
    assert identities.resolve("unit", "nb", "recipe", "fedd").source_type == "reviewed_correction"

    # Subsequent recipe creation with "hvitløk" auto-resolves because add_alias=True:
    subsequent = service.create_recipe(
        RecipeCreate(
            title="Other Recipe",
            servings=1,
            ingredients=[Ingredient(name="hvitløk", quantity="1", unit="fedd")],
            instructions=["Eat"],
        )
    )
    assert subsequent.ingredients[0].canonical_product_id == garlic.id
    assert subsequent.ingredients[0].canonical_unit_id == clove.id

    # All unmapped ingredients are resolved:
    assert len(service.list_unmapped_ingredients()) == 0


@pytest.mark.asyncio
async def test_unmapped_ingredients_routes_and_mcp_tools():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    butter = identities.create_product("Butter")

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)

    recipe = service.create_recipe(
        RecipeCreate(
            title="Toast",
            servings=1,
            ingredients=[Ingredient(name="smør", quantity="10", unit="g")],
            instructions=["Spread"],
        )
    )

    class DummyFeatures:
        def __init__(self, kitchen, ids):
            self.kitchen = kitchen
            self.catalogue_identities = ids

    dummy_features = DummyFeatures(service, identities)
    router = feature_router(dummy_features, None)
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        # GET /api/catalogue/unmapped-ingredients
        res = client.get("/api/catalogue/unmapped-ingredients")
        assert res.status_code == 200
        assert len(res.json()) == 1
        assert res.json()[0]["name"] == "smør"
        assert res.json()[0]["canonical_product_id"] is None

        # GET /api/catalogue/identities/unmapped (alias path)
        res_alias = client.get("/api/catalogue/identities/unmapped")
        assert res_alias.status_code == 200
        assert len(res_alias.json()) == 1

        # Error on nonexistent recipe
        bad_get = client.get(
            "/api/catalogue/unmapped-ingredients",
            params={"recipe_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert bad_get.status_code == 404

        # POST /api/catalogue/unmapped-ingredients/map with missing IDs -> 422
        bad_map = client.post(
            "/api/catalogue/unmapped-ingredients/map",
            json={"recipe_id": str(recipe.id), "ingredient_index": 0},
        )
        assert bad_map.status_code == 422

        # POST /api/catalogue/unmapped-ingredients/map
        map_res = client.post(
            "/api/catalogue/unmapped-ingredients/map",
            json={
                "recipe_id": str(recipe.id),
                "ingredient_index": 0,
                "canonical_product_id": str(butter.id),
                "add_alias": True,
            },
        )
        assert map_res.status_code == 200
        assert map_res.json()["ingredients"][0]["canonical_product_id"] == str(butter.id)

    # MCP tool test:
    mcp = FastMCP("test")
    install_feature_tools(mcp, dummy_features, None)
    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert "list_unmapped_ingredients" in tools
    assert "map_unmapped_ingredient" in tools
    assert tools["list_unmapped_ingredients"].annotations.readOnlyHint is True
    assert tools["map_unmapped_ingredient"].annotations.readOnlyHint is False


@pytest.mark.skipif(
    not os.environ.get("KITCHEN_TEST_DATABASE_URL"),
    reason="requires the explicitly configured disposable PostgreSQL test database",
)
def test_identity_api_reuses_products_resolves_aliases_and_preserves_recipe_payload(
    test_database_url,
):
    from fastapi.testclient import TestClient

    from home_kitchen.app import create_app
    from home_kitchen.settings import Settings

    with TestClient(
        create_app(Settings(local_dev=True, database_url=test_database_url)),
        client=("127.0.0.1", 5000),
    ) as client:
        first = client.post(
            "/api/catalogue/identities/products",
            json={"canonical_name": "Tomato", "variant": "fresh"},
        )
        reused = client.post(
            "/api/catalogue/identities/products",
            json={"canonical_name": " tomato ", "variant": "FRESH"},
        )
        distinct = client.post(
            "/api/catalogue/identities/products",
            json={"canonical_name": "tomato", "variant": "canned"},
        )
        assert first.status_code == reused.status_code == distinct.status_code == 201
        assert first.json()["id"] == reused.json()["id"]
        assert distinct.json()["id"] != first.json()["id"]

        alias_path = f"/api/catalogue/aliases/product/{first.json()['id']}"
        assert (
            client.post(
                alias_path, json={"alias": "tomat", "locale": "nb", "context": "recipe"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/api/catalogue/aliases/product/{distinct.json()['id']}",
                json={"alias": "tomat", "locale": "nb", "context": "recipe"},
            ).status_code
            == 201
        )
        ambiguous = client.get(
            "/api/catalogue/aliases/product",
            params={"alias": "tomat", "locale": "nb", "context": "recipe"},
        )
        assert ambiguous.json()["status"] == "ambiguous"
        assert (
            client.put(
                f"{alias_path}/review", json={"alias": "tomat", "locale": "nb", "context": "recipe"}
            ).status_code
            == 204
        )
        resolved = client.get(
            "/api/catalogue/aliases/product",
            params={"alias": "tomat", "locale": "nb", "context": "recipe"},
        )
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["candidates"][0]["id"] == first.json()["id"]

        unit = client.post("/api/catalogue/identities/units", json={"canonical_name": "gram"})
        assert unit.status_code == 201
        unit_alias_path = f"/api/catalogue/aliases/unit/{unit.json()['id']}"
        assert (
            client.post(
                unit_alias_path,
                json={"alias": "g", "locale": "nb", "context": "recipe"},
            ).status_code
            == 201
        )
        unit_result = client.get(
            "/api/catalogue/aliases/unit",
            params={"alias": "g", "locale": "nb", "context": "recipe"},
        )
        assert unit_result.json()["status"] == "resolved"
        assert unit_result.json()["candidates"][0]["id"] == unit.json()["id"]

        nonexistent_id = "00000000-0000-0000-0000-000000000001"
        for path in (
            f"/api/catalogue/aliases/product/{unit.json()['id']}",
            f"/api/catalogue/aliases/unit/{nonexistent_id}",
        ):
            invalid_alias = client.post(
                path, json={"alias": "bad", "locale": "nb", "context": "recipe"}
            )
            assert invalid_alias.status_code == 422

        recipe = client.post(
            "/api/recipes",
            json={
                "title": "Legacy",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Old unmatched phrase",
                        "quantity": "1.25",
                        "unit": "Handfuls",
                        "canonical_product_id": first.json()["id"],
                        "canonical_unit_id": unit.json()["id"],
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert recipe.status_code == 201
        assert recipe.json()["ingredients"][0]["quantity"] == "1.25"
        assert recipe.json()["ingredients"][0]["unit"] == "handfuls"
        assert recipe.json()["ingredients"][0]["source_unit_expression"] == "Handfuls"
        assert recipe.json()["ingredients"][0]["canonical_product_id"] == first.json()["id"]
        assert recipe.json()["ingredients"][0]["canonical_unit_id"] == unit.json()["id"]

        old_client_unchanged_update = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Legacy edited",
                "servings": 1,
                "ingredients": [
                    {"name": "Old unmatched phrase", "quantity": "1.25", "unit": "handfuls"}
                ],
                "instructions": ["Cook slowly"],
            },
        )
        assert old_client_unchanged_update.status_code == 200
        edited_ingredient = old_client_unchanged_update.json()["ingredients"][0]
        assert edited_ingredient["quantity"] == "1.25"
        assert edited_ingredient["canonical_product_id"] == first.json()["id"]
        assert edited_ingredient["canonical_unit_id"] == unit.json()["id"]
        assert edited_ingredient["source_unit_expression"] == "Handfuls"

        old_client_changed_update = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Legacy edited",
                "servings": 1,
                "ingredients": [
                    {"name": "Old unmatched phrase", "quantity": "2.5", "unit": "handfuls"}
                ],
                "instructions": ["Cook slowly"],
            },
        )
        assert old_client_changed_update.status_code == 422
        assert "include identity metadata" in old_client_changed_update.json()["detail"]
        after_rejection = client.get(f"/api/recipes/{recipe.json()['id']}").json()["ingredients"][0]
        assert after_rejection["quantity"] == "1.25"
        assert after_rejection["canonical_product_id"] == first.json()["id"]

        explicitly_cleared = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Legacy edited",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Old unmatched phrase",
                        "quantity": "2.5",
                        "unit": "handfuls",
                        "canonical_product_id": None,
                        "canonical_unit_id": None,
                        "source_unit_expression": None,
                    }
                ],
                "instructions": ["Cook slowly"],
            },
        )
        assert explicitly_cleared.status_code == 200
        cleared_ingredient = explicitly_cleared.json()["ingredients"][0]
        assert cleared_ingredient["canonical_product_id"] is None
        assert cleared_ingredient["canonical_unit_id"] is None
        assert cleared_ingredient["source_unit_expression"] is None
        invalid_recipe = client.post(
            "/api/recipes",
            json={
                "title": "Invalid link",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Tomato",
                        "quantity": "1",
                        "unit": "item",
                        "canonical_product_id": nonexistent_id,
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert invalid_recipe.status_code == 422
        invalid_update = client.put(
            f"/api/recipes/{recipe.json()['id']}",
            json={
                "title": "Invalid update",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "Tomato",
                        "quantity": "1",
                        "unit": "item",
                        "canonical_unit_id": first.json()["id"],
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert invalid_update.status_code == 422

        auto_recipe = client.post(
            "/api/recipes",
            json={
                "title": "Auto resolved",
                "servings": 1,
                "ingredients": [
                    {
                        "name": "tomat",
                        "quantity": "2",
                        "unit": "g",
                    }
                ],
                "instructions": ["Cook"],
            },
        )
        assert auto_recipe.status_code == 201
        auto_ing = auto_recipe.json()["ingredients"][0]
        assert auto_ing["canonical_product_id"] == first.json()["id"]
        assert auto_ing["canonical_unit_id"] == unit.json()["id"]
        assert auto_ing["name"] == "tomat"
        assert auto_ing["unit"] == "g"

        unmapped_res = client.get("/api/catalogue/unmapped-ingredients")
        assert unmapped_res.status_code == 200
        assert any(item["recipe_id"] == recipe.json()["id"] for item in unmapped_res.json())

        unmapped_alias_res = client.get("/api/catalogue/identities/unmapped")
        assert unmapped_alias_res.status_code == 200

        map_res = client.post(
            "/api/catalogue/unmapped-ingredients/map",
            json={
                "recipe_id": recipe.json()["id"],
                "ingredient_index": 0,
                "canonical_product_id": first.json()["id"],
                "canonical_unit_id": unit.json()["id"],
                "add_alias": False,
            },
        )
        assert map_res.status_code == 200
        assert map_res.json()["ingredients"][0]["canonical_product_id"] == first.json()["id"]
        assert map_res.json()["ingredients"][0]["canonical_unit_id"] == unit.json()["id"]
