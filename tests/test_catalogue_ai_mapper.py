"""Unit tests for AI-assisted ingredient and unit mapping with multilingual aliases."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import create_engine, text

from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_ingredients
from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.connector_store import ConnectorStore
from home_kitchen.connector_store import metadata as connector_metadata
from home_kitchen.cookidoo_recipes import CookidooRecipeService
from home_kitchen.feature_api import feature_router
from home_kitchen.imports import ImportedDraft
from home_kitchen.models import Ingredient, Recipe, RecipeCreate
from home_kitchen.recipes.collections import CollectionEntry, CollectionService
from home_kitchen.service import KitchenService
from home_kitchen.settings import Settings


class MemoryIdentityRepository:
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
        if key in self.reviewed:
            return
        self.aliases.setdefault(key, set()).add(identity_id)

    def has_reviewed_alias(self, kind: str, locale: str, context: str, alias: str) -> bool:
        key = (kind, locale, context, " ".join(alias.split()).casefold())
        return key in self.reviewed

    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
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
        identities = (
            list(self.products.values()) if kind == "product" else list(self.units.values())
        )
        return [identity for identity in identities if identity.id in ids]

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        if not ids:
            return
        existing = (
            {p.id for p in self.products.values()}
            if kind == "product"
            else {u.id for u in self.units.values()}
        )
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

    def save(self, kind: str, entity: object) -> None:
        if kind == "recipe" and isinstance(entity, Recipe):
            self.recipes[entity.id] = entity

    def get(self, kind: str, entity_id: UUID, model: type) -> Recipe:
        if kind == "recipe" and entity_id in self.recipes:
            return self.recipes[entity_id]
        raise KeyError(str(entity_id))

    def list(self, kind: str, model: type) -> list[Recipe]:
        if kind == "recipe":
            return list(self.recipes.values())
        return []


def _configured_settings() -> Settings:
    return Settings(
        local_dev=True,
        ai_model="claude-sonnet-4-5",
        anthropic_api_key="test-key",  # type: ignore[arg-type]
    )


def test_auto_map_graceful_noop_when_ai_unconfigured():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)
    service.create_recipe(
        RecipeCreate(
            title="Garlic Soup",
            servings=2,
            ingredients=[Ingredient(name="garlic", quantity="2", unit="cloves")],
            instructions=["Simmer"],
        )
    )

    result = auto_map_unmapped_ingredients(
        store,
        Settings(local_dev=True),
        household_id=uuid4(),
        identity_service=identities,
    )
    assert result == {
        "processed_unique": 0,
        "mapped_occurrences": 0,
        "created_products": 0,
        "created_aliases": 0,
        "remaining_unmapped": 1,
    }


def test_auto_map_batch_dedup_multilingual_aliases_and_idempotency():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    existing_tomato = identities.create_product("tomat")
    existing_gram = identities.create_unit("g")

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)

    r1 = service.create_recipe(
        RecipeCreate(
            title="Pasta",
            servings=2,
            source_language="en",
            ingredients=[
                Ingredient(name="garlic", quantity="3", unit="cloves"),
                Ingredient(name="tomato", quantity="400", unit="grams"),
            ],
            instructions=["Cook pasta"],
        )
    )
    r2 = service.create_recipe(
        RecipeCreate(
            title="Garlic Toast",
            servings=2,
            source_language="en",
            ingredients=[
                Ingredient(name="Garlic", quantity="1", unit="cloves"),
            ],
            instructions=["Toast bread"],
        )
    )

    model = TestModel(
        custom_output_args={
            "mappings": [
                {
                    "item_index": 0,
                    "source_name": "garlic",
                    "source_unit": "cloves",
                    "new_product_name": "hvitløk",
                    "new_unit_symbol": "fedd",
                    "new_unit_name": "fedd",
                    "aliases": [
                        {"locale": "nb", "alias": "hvitløk"},
                        {"locale": "en", "alias": "garlic"},
                        {"locale": "fr", "alias": "ail"},
                    ],
                    "unit_aliases": [
                        {"locale": "nb", "alias": "fedd"},
                        {"locale": "en", "alias": "cloves"},
                        {"locale": "fr", "alias": "gousses"},
                    ],
                },
                {
                    "item_index": 1,
                    "source_name": "tomato",
                    "source_unit": "grams",
                    "existing_product_id": str(existing_tomato.id),
                    "existing_unit_id": str(existing_gram.id),
                    "aliases": [
                        {"locale": "nb", "alias": "tomat"},
                        {"locale": "en", "alias": "tomato"},
                        {"locale": "fr", "alias": "tomate"},
                    ],
                    "unit_aliases": [
                        {"locale": "nb", "alias": "gram"},
                        {"locale": "en", "alias": "grams"},
                        {"locale": "fr", "alias": "grammes"},
                    ],
                },
            ]
        }
    )

    result = auto_map_unmapped_ingredients(
        store,
        _configured_settings(),
        household_id=uuid4(),
        batch_size=30,
        max_batches=1,
        identity_service=identities,
        model=model,
    )

    assert result["processed_unique"] == 2
    assert result["mapped_occurrences"] == 3
    assert result["created_products"] == 1
    assert result["created_aliases"] >= 10
    assert result["remaining_unmapped"] == 0

    updated_r1 = store.get("recipe", r1.id, Recipe)
    updated_r2 = store.get("recipe", r2.id, Recipe)
    garlic_prod = updated_r1.ingredients[0].canonical_product_id
    clove_unit = updated_r1.ingredients[0].canonical_unit_id
    assert garlic_prod is not None
    assert clove_unit is not None
    assert updated_r2.ingredients[0].canonical_product_id == garlic_prod
    assert updated_r2.ingredients[0].canonical_unit_id == clove_unit
    assert updated_r1.ingredients[1].canonical_product_id == existing_tomato.id
    assert updated_r1.ingredients[1].canonical_unit_id == existing_gram.id

    # Multilingual alias resolution works across nb, en, and fr
    assert identities.resolve("product", "nb", "recipe", "hvitløk").candidates[0].id == garlic_prod
    assert identities.resolve("product", "en", "recipe", "garlic").candidates[0].id == garlic_prod
    assert identities.resolve("product", "fr", "recipe", "ail").candidates[0].id == garlic_prod
    assert identities.resolve("unit", "en", "recipe", "cloves").candidates[0].id == clove_unit
    assert identities.resolve("unit", "fr", "recipe", "gousses").candidates[0].id == clove_unit

    # Future French recipe auto-resolves without AI!
    french_recipe = service.create_recipe(
        RecipeCreate(
            title="Soupe à l'ail",
            servings=2,
            source_language="fr",
            ingredients=[Ingredient(name="ail", quantity="4", unit="gousses")],
            instructions=["Cuire doucement"],
        )
    )
    assert french_recipe.ingredients[0].canonical_product_id == garlic_prod
    assert french_recipe.ingredients[0].canonical_unit_id == clove_unit

    # Idempotency: re-running returns zeroes and adds no duplicates
    second = auto_map_unmapped_ingredients(
        store,
        _configured_settings(),
        household_id=uuid4(),
        identity_service=identities,
        model=model,
    )
    assert second == {
        "processed_unique": 0,
        "mapped_occurrences": 0,
        "created_products": 0,
        "created_aliases": 0,
        "remaining_unmapped": 0,
    }


def test_auto_map_respects_batch_size_and_max_batches():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)
    service.create_recipe(
        RecipeCreate(
            title="Salad",
            servings=2,
            ingredients=[
                Ingredient(name="agurk", quantity="1", unit="stk"),
                Ingredient(name="paprika", quantity="1", unit="stk"),
            ],
            instructions=["Chop"],
        )
    )
    model = TestModel(
        custom_output_args={
            "mappings": [
                {
                    "item_index": 0,
                    "source_name": "agurk",
                    "source_unit": "stk",
                    "new_product_name": "agurk",
                    "new_unit_symbol": "stk",
                    "aliases": [{"locale": "nb", "alias": "agurk"}],
                }
            ]
        }
    )
    result = auto_map_unmapped_ingredients(
        store,
        _configured_settings(),
        household_id=uuid4(),
        batch_size=1,
        max_batches=1,
        identity_service=identities,
        model=model,
    )
    assert result["processed_unique"] == 1
    assert result["mapped_occurrences"] == 1
    assert result["remaining_unmapped"] == 1


def test_kitchen_service_create_and_update_auto_map_and_survive_ai_failure():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    store = MemoryStore()
    service = KitchenService(
        store=store,
        identity_service=identities,
        settings=_configured_settings(),
        household_id=uuid4(),
    )

    with patch(
        "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients",
        side_effect=RuntimeError("Anthropic unavailable"),
    ) as mocked:
        created = service.create_recipe(
            RecipeCreate(
                title="Resilient Soup",
                servings=2,
                ingredients=[Ingredient(name="gulrot", quantity="2", unit="stk")],
                instructions=["Boil"],
            )
        )
        assert mocked.call_count == 1
        assert created.title == "Resilient Soup"
        assert created.ingredients[0].canonical_product_id is None

        updated = service.update_recipe(
            created.id,
            RecipeCreate(
                title="Resilient Soup v2",
                servings=2,
                ingredients=[Ingredient(name="gulrot", quantity="3", unit="stk")],
                instructions=["Boil longer"],
            ),
        )
        assert mocked.call_count == 2
        assert updated.title == "Resilient Soup v2"


def test_collection_save_and_cookidoo_import_trigger_automap_with_failure_safety(tmp_path):
    db_path = tmp_path / "automap_imports.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    connector_metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE recipes (
                    id TEXT PRIMARY KEY,
                    position INTEGER,
                    title TEXT NOT NULL,
                    servings INTEGER NOT NULL,
                    ingredients TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    source_url TEXT,
                    image_url TEXT,
                    source TEXT NOT NULL,
                    source_ingredients TEXT NOT NULL DEFAULT '[]',
                    provenance TEXT,
                    photo_source TEXT,
                    photo_source_id TEXT,
                    image_attribution TEXT,
                    source_language TEXT NOT NULL DEFAULT 'und',
                    recipe_type TEXT,
                    prep_time_minutes INTEGER,
                    prep_category TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE recipe_source_links (
                    provider TEXT NOT NULL,
                    external_recipe_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (provider, external_recipe_id)
                )
                """
            )
        )

    try:
        repo = MemoryIdentityRepository()
        identities = CanonicalIdentityService(repo)
        known_salt = identities.create_product("salt")
        identities.add_alias("product", "nb", "recipe", "salt", known_salt.id)

        store = SimpleNamespace(engine=engine, get_setting=lambda _k: None)
        kitchen = KitchenService(
            store=store,  # type: ignore[arg-type]
            identity_service=identities,
            settings=_configured_settings(),
            household_id=uuid4(),
        )
        fake_history = SimpleNamespace(record=lambda *a, **kw: None)
        cipher = Fernet(Fernet.generate_key())
        cstore = ConnectorStore(engine, cipher)

        # 1. CollectionService._save calls auto_map_unmapped_ingredients and ignores AI errors
        collection_provider = SimpleNamespace(
            id="oda",
            generation=lambda _actor: 1,
            lock_generation=lambda *a, **kw: None,
        )
        collections = CollectionService(
            kitchen,
            fake_history,  # type: ignore[arg-type]
            providers={"oda": collection_provider},  # type: ignore[dict-item]
        )
        job = SimpleNamespace(
            actor="malo", generation=1, cancelled=False, created=1e18, provider=collection_provider
        )
        entry = CollectionEntry(
            id="101",
            provider="oda",
            title="Saltet laks",
            source_url="https://oda.com/no/recipes/101-saltet-laks/",
        )
        draft = ImportedDraft(
            title="Saltet laks",
            servings=2,
            ingredients=["1 ts salt", "400 g laks"],
            instructions=["Stek laks"],
            source_url="https://oda.com/no/recipes/101-saltet-laks/",
        )
        with (
            patch("home_kitchen.recipes.collections.time.monotonic", return_value=1e18),
            patch(
                "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients",
                side_effect=RuntimeError("AI error"),
            ) as mocked_collection_map,
        ):
            rec_id, existed = collections._save(job, entry, draft)
            assert existed is False
            assert rec_id is not None
            assert mocked_collection_map.call_count == 1

        # 2. CookidooRecipeService.import_recipe_atomic triggers auto_map and ignores AI errors
        cookidoo_service = CookidooRecipeService(
            kitchen,
            fake_history,  # type: ignore[arg-type]
            cstore,
            uuid4(),
        )
        cookidoo_entry = CollectionEntry(
            id="r123",
            provider="cookidoo",
            title="Kremet suppe",
            source_url="https://cookidoo.no/recipes/recipe/nb-NO/r123",
            ingredients=["200 g gulrot"],
            instructions=["Kok"],
        )
        with patch(
            "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients",
            side_effect=RuntimeError("AI timeout"),
        ) as mocked_cookidoo_map:
            cid, cexisted = cookidoo_service.import_recipe_atomic(
                "malo", cookidoo_entry, reviewed=True
            )
            assert cexisted is False
            assert cid is not None
            assert mocked_cookidoo_map.call_count == 1
    finally:
        engine.dispose()


def test_auto_map_api_route():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    store = MemoryStore()
    kitchen = KitchenService(store=store, identity_service=identities)
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Pasta",
            servings=2,
            ingredients=[Ingredient(name="basilikum", quantity="1", unit="bunt")],
            instructions=["Serve"],
        )
    )

    class DummyFeatures:
        def __init__(self):
            self.kitchen = kitchen
            self.catalogue_identities = identities
            self.settings = Settings(local_dev=True)
            self.household_id = uuid4()

    app = FastAPI()
    app.include_router(feature_router(DummyFeatures(), None))  # type: ignore[arg-type]

    with TestClient(app) as client:
        # Unconfigured AI returns graceful no-op summary
        noop_res = client.post("/api/catalogue/unmapped-ingredients/auto-map", json={})
        assert noop_res.status_code == 200
        assert noop_res.json() == {
            "processed_unique": 0,
            "mapped_occurrences": 0,
            "created_products": 0,
            "created_aliases": 0,
            "remaining_unmapped": 1,
        }

        # Unknown recipe_id returns 404
        missing_res = client.post(
            "/api/catalogue/unmapped-ingredients/auto-map",
            json={"recipe_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert missing_res.status_code == 404

        # Configured / mocked async mapper returns mapped counts
        with patch(
            "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients_async",
            return_value={
                "processed_unique": 1,
                "mapped_occurrences": 1,
                "created_products": 1,
                "created_aliases": 3,
                "remaining_unmapped": 0,
            },
        ) as mocked_route:
            ok_res = client.post(
                "/api/catalogue/unmapped-ingredients/auto-map",
                json={"recipe_id": str(recipe.id), "batch_size": 10, "max_batches": 2},
            )
            assert ok_res.status_code == 200
            assert ok_res.json()["mapped_occurrences"] == 1
            assert mocked_route.call_args.kwargs["recipe_id"] == recipe.id
            assert mocked_route.call_args.kwargs["batch_size"] == 10
            assert mocked_route.call_args.kwargs["max_batches"] == 2
