"""Unit tests for AI-assisted ingredient and unit mapping with multilingual aliases."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel
from sqlalchemy import create_engine, text

from home_kitchen.catalogue.ai_mapper import (
    _build_batch_prompt,
    _collect_unmapped_groups,
    _load_existing_catalogue,
    auto_map_unmapped_ingredients,
)
from home_kitchen.catalogue.identity import (
    CanonicalProduct,
    CanonicalUnit,
    strip_ingredient_attributes,
)
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.connector_store import ConnectorStore
from home_kitchen.connector_store import metadata as connector_metadata
from home_kitchen.cookidoo_recipes import CookidooRecipeService
from home_kitchen.feature_api import feature_router
from home_kitchen.imports import ImportedDraft
from home_kitchen.models import Ingredient, Recipe, RecipeCreate
from home_kitchen.recipes.collections import CollectionEntry, CollectionJob, CollectionService
from home_kitchen.service import KitchenService
from home_kitchen.settings import Settings

pytestmark = pytest.mark.mocked_provider


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


def _create_import_sqlite_schema(engine) -> None:
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
                    prep_category TEXT,
                    guided_steps TEXT NOT NULL DEFAULT '[]'
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


def test_collection_save_and_cookidoo_import_skip_per_recipe_ai_and_run_sql_resolution(tmp_path):
    db_path = tmp_path / "automap_imports.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    _create_import_sqlite_schema(engine)

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

        # 1. CollectionService._save runs SQL/alias resolution only (no per-recipe AI call)
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
            ) as mocked_collection_sync,
            patch(
                "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients_async",
                new_callable=AsyncMock,
            ) as mocked_collection_async,
        ):
            rec_id, existed = collections._save(job, entry, draft)
            assert existed is False
            assert rec_id is not None
            assert mocked_collection_sync.call_count == 0
            assert mocked_collection_async.call_count == 0

        with engine.connect() as conn:
            saved_ing = json.loads(
                conn.execute(
                    text("SELECT ingredients FROM recipes WHERE id = :id"), {"id": rec_id.hex}
                ).scalar_one()
            )
            assert saved_ing[0]["canonical_product_id"] == str(known_salt.id)
            assert saved_ing[1]["canonical_product_id"] is None

        # 2. CookidooRecipeService.import_recipe_atomic runs SQL resolution only (no AI call)
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
            ingredients=["1 ts salt", "200 g gulrot"],
            instructions=["Kok"],
        )
        with (
            patch(
                "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients",
            ) as mocked_cookidoo_sync,
            patch(
                "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients_async",
                new_callable=AsyncMock,
            ) as mocked_cookidoo_async,
        ):
            cid, cexisted = cookidoo_service.import_recipe_atomic(
                "malo", cookidoo_entry, reviewed=True
            )
            assert cexisted is False
            assert cid is not None
            assert mocked_cookidoo_sync.call_count == 0
            assert mocked_cookidoo_async.call_count == 0

        with engine.connect() as conn:
            cookidoo_ing = json.loads(
                conn.execute(
                    text("SELECT ingredients FROM recipes WHERE id = :id"), {"id": cid.hex}
                ).scalar_one()
            )
            assert cookidoo_ing[0]["canonical_product_id"] == str(known_salt.id)
            assert cookidoo_ing[1]["canonical_product_id"] is None
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_bulk_collection_import_runs_batched_ai_mapping_once_with_failure_safety(tmp_path):
    db_path = tmp_path / "bulk_import.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    _create_import_sqlite_schema(engine)

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
        collection_provider = SimpleNamespace(
            id="cookidoo",
            generation=lambda _actor: 1,
            lock_generation=lambda *a, **kw: None,
            validate_source=lambda _entry: None,
        )
        collections = CollectionService(
            kitchen,
            fake_history,  # type: ignore[arg-type]
            providers={"cookidoo": collection_provider},  # type: ignore[dict-item]
        )

        entries = [
            CollectionEntry(
                id="c1",
                provider="cookidoo",
                title="Suppe 1",
                source_url="https://cookidoo.no/recipes/recipe/nb-NO/c1",
                ingredients=["1 ts salt", "200 g gulrot"],
                instructions=["Kok"],
            ),
            CollectionEntry(
                id="c2",
                provider="cookidoo",
                title="Suppe 2",
                source_url="https://cookidoo.no/recipes/recipe/nb-NO/c2",
                ingredients=["100 g gulrot", "1 stk løk"],
                instructions=["Fres"],
            ),
            CollectionEntry(
                id="c3",
                provider="cookidoo",
                title="Suppe 3",
                source_url="https://cookidoo.no/recipes/recipe/nb-NO/c3",
                ingredients=["2 ts økologisk salt", "300 g potet"],
                instructions=["Stek"],
            ),
        ]
        view = CollectionJob(
            job_id=uuid4(),
            provider="cookidoo",
            status="importing",
            kinds=["saved"],
            entries=entries,
            total=len(entries),
        )
        job = SimpleNamespace(
            actor="malo",
            generation=1,
            cancelled=False,
            created=1e18,
            provider=collection_provider,
            view=view,
        )

        with (
            patch("home_kitchen.recipes.collections.time.monotonic", return_value=1e18),
            patch(
                "home_kitchen.catalogue.ai_mapper.auto_map_unmapped_ingredients_async",
                new_callable=AsyncMock,
                side_effect=RuntimeError("AI unavailable"),
            ) as mocked_bulk_map,
        ):
            await collections._import(job, entries)

        # AI mapping is invoked ONCE across all 3 imported recipes, and failure does not fail import
        assert view.status == "completed"
        assert view.processed == 3
        assert all(entry.status == "imported" for entry in entries)
        assert mocked_bulk_map.call_count == 1
        call_kwargs = mocked_bulk_map.call_args.kwargs
        assert call_kwargs["batch_size"] == 40
        assert call_kwargs["max_batches"] == 10
        assert len(call_kwargs["recipe_ids"]) == 3

        # Deterministic SQL + attribute-stripped resolution already mapped salt & økologisk salt
        with engine.connect() as conn:
            saved_ingredients = []
            for entry in entries:
                assert entry.recipe_id is not None
                saved_ingredients.append(
                    json.loads(
                        conn.execute(
                            text("SELECT ingredients FROM recipes WHERE id = :id"),
                            {"id": entry.recipe_id.hex},
                        ).scalar_one()
                    )
                )
            assert saved_ingredients[0][0]["canonical_product_id"] == str(known_salt.id)
            assert saved_ingredients[1][0]["canonical_product_id"] is None
            assert saved_ingredients[2][0]["canonical_product_id"] == str(known_salt.id)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_bulk_collection_import_deduplicates_and_maps_end_to_end(tmp_path):
    db_path = tmp_path / "bulk_import_e2e.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    _create_import_sqlite_schema(engine)

    try:
        repo = MemoryIdentityRepository()
        identities = CanonicalIdentityService(repo)
        known_gram = identities.create_unit("g")
        identities.add_alias("unit", "nb", "recipe", "g", known_gram.id)

        store = SimpleNamespace(engine=engine, get_setting=lambda _k: None)
        kitchen = KitchenService(
            store=store,  # type: ignore[arg-type]
            identity_service=identities,
            settings=_configured_settings(),
            household_id=uuid4(),
        )
        fake_history = SimpleNamespace(record=lambda *a, **kw: None)
        collection_provider = SimpleNamespace(
            id="cookidoo",
            generation=lambda _actor: 1,
            lock_generation=lambda *a, **kw: None,
            validate_source=lambda _entry: None,
        )
        collections = CollectionService(
            kitchen,
            fake_history,  # type: ignore[arg-type]
            providers={"cookidoo": collection_provider},  # type: ignore[dict-item]
        )

        entries = [
            CollectionEntry(
                id="b1",
                provider="cookidoo",
                title="R1",
                source_url="https://cookidoo.no/recipes/recipe/nb-NO/b1",
                ingredients=["200 g gulrot"],
                instructions=["Kok"],
            ),
            CollectionEntry(
                id="b2",
                provider="cookidoo",
                title="R2",
                source_url="https://cookidoo.no/recipes/recipe/nb-NO/b2",
                ingredients=["100 g gulrot"],
                instructions=["Stek"],
            ),
        ]
        view = CollectionJob(
            job_id=uuid4(),
            provider="cookidoo",
            status="importing",
            kinds=["saved"],
            entries=entries,
            total=len(entries),
        )
        job = SimpleNamespace(
            actor="malo",
            generation=1,
            cancelled=False,
            created=1e18,
            provider=collection_provider,
            view=view,
        )
        from home_kitchen.catalogue import ai_mapper as ai_mapper_mod

        orig_builder = ai_mapper_mod.build_ingredient_mapper
        builder_calls = 0

        def fake_builder(settings, model=None):
            nonlocal builder_calls
            builder_calls += 1
            return orig_builder(
                settings,
                model=TestModel(
                    custom_output_args={
                        "mappings": [
                            {
                                "item_index": 0,
                                "source_name": "gulrot",
                                "source_unit": "g",
                                "new_product_name": "gulrot",
                                "aliases": [{"locale": "nb", "alias": "gulrot"}],
                            }
                        ]
                    }
                ),
            )

        with (
            patch("home_kitchen.recipes.collections.time.monotonic", return_value=1e18),
            patch(
                "home_kitchen.catalogue.ai_mapper.build_ingredient_mapper", side_effect=fake_builder
            ),
        ):
            await collections._import(job, entries)

        assert builder_calls == 1
        assert view.status == "completed"
        gulrot_prod = repo.products[("gulrot", "")]
        with engine.connect() as conn:
            for entry in entries:
                assert entry.recipe_id is not None
                row_ing = json.loads(
                    conn.execute(
                        text("SELECT ingredients FROM recipes WHERE id = :id"),
                        {"id": entry.recipe_id.hex},
                    ).scalar_one()
                )
                assert row_ing[0]["canonical_product_id"] == str(gulrot_prod.id)
    finally:
        engine.dispose()


def test_deterministic_attribute_stripping_maps_to_existing_products_without_ai():
    assert strip_ingredient_attributes("økologisk gulrot") == ("gulrot", ["økologisk"])
    assert strip_ingredient_attributes("paprika, finskåret") == ("paprika", ["finskåret"])

    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    gulrot = identities.create_product("gulrot")
    identities.add_alias("product", "en", "recipe", "carrot", gulrot.id)
    paprika = identities.create_product("paprika")
    helmelk = identities.create_product("helmelk")
    gram = identities.create_unit("g")

    store = MemoryStore()
    service = KitchenService(store=store, identity_service=identities)
    # Create recipe directly in store without resolving yet so ai_mapper deterministic step is exercised
    recipe = Recipe(
        id=uuid4(),
        title="Variant Salad",
        servings=2,
        source_language="nb",
        ingredients=[
            Ingredient(name="økologisk gulrot", quantity="200", unit="g"),
            Ingredient(name="organic carrots", quantity="100", unit="g"),
            Ingredient(name="paprika, finskåret", quantity="150", unit="g"),
            Ingredient(name="økologisk helmelk", quantity="2", unit="g"),
        ],
        instructions=["Mix"],
    )
    store.save("recipe", recipe)

    with patch(
        "home_kitchen.catalogue.ai_mapper.build_ingredient_mapper",
        side_effect=AssertionError(
            "AI agent must not be called when deterministic matching suffices"
        ),
    ):
        result = auto_map_unmapped_ingredients(
            store,
            _configured_settings(),
            household_id=uuid4(),
            identity_service=identities,
        )

    assert result["processed_unique"] == 4
    assert result["mapped_occurrences"] == 4
    assert result["created_products"] == 0
    assert result["remaining_unmapped"] == 0
    assert len(repo.products) == 3

    updated = store.get("recipe", recipe.id, Recipe)
    assert updated.ingredients[0].canonical_product_id == gulrot.id
    assert updated.ingredients[1].canonical_product_id == gulrot.id
    assert updated.ingredients[2].canonical_product_id == paprika.id
    assert updated.ingredients[3].canonical_product_id == helmelk.id
    assert all(ing.canonical_unit_id == gram.id for ing in updated.ingredients)

    # Full variant strings are recorded as aliases so future lookups resolve directly
    assert (
        identities.resolve("product", "nb", "recipe", "økologisk gulrot").candidates[0].id
        == gulrot.id
    )
    assert (
        identities.resolve("product", "nb", "recipe", "organic carrots").candidates[0].id
        == gulrot.id
    )
    assert (
        identities.resolve("product", "nb", "recipe", "paprika, finskåret").candidates[0].id
        == paprika.id
    )
    assert (
        identities.resolve("product", "nb", "recipe", "økologisk helmelk").candidates[0].id
        == helmelk.id
    )

    # Fast SQL/service resolution also resolves stripped variants during create_recipe
    service_recipe = service.create_recipe(
        RecipeCreate(
            title="Fresh Carrot Soup",
            servings=2,
            ingredients=[Ingredient(name="økologisk gulrot", quantity="100", unit="g")],
            instructions=["Boil"],
        )
    )
    assert service_recipe.ingredients[0].canonical_product_id == gulrot.id


def test_ai_mapper_prompt_includes_aliases_and_python_prevents_duplicate_products():
    repo = MemoryIdentityRepository()
    identities = CanonicalIdentityService(repo)
    gulrot = identities.create_product("gulrot")
    identities.add_alias("product", "en", "recipe", "carrot", gulrot.id)

    store = MemoryStore()
    # "gulrot-suppe-ingrediens" won't match deterministically, e.g. French "carottes râpées"
    # when "fr" alias isn't registered yet, and AI erroneously proposes new_product_name="Gulrot"
    # or "økologisk gulrot" instead of existing_product_id.
    recipe = Recipe(
        id=uuid4(),
        title="Råkost",
        servings=2,
        source_language="fr",
        ingredients=[
            Ingredient(name="carottes bio", quantity="2", unit="stk"),
        ],
        instructions=["Râper"],
    )
    store.save("recipe", recipe)

    catalogue = _load_existing_catalogue(store, identities)
    groups, _ = _collect_unmapped_groups([recipe])
    prompt = _build_batch_prompt(groups, catalogue)
    assert "Existing canonical_products:" in prompt
    assert "Existing product_aliases:" in prompt
    assert "gulrot" in prompt
    assert "carrot" in prompt

    # AI tries to create duplicate product "Gulrot" (with variant "økologisk") instead of existing_product_id
    model = TestModel(
        custom_output_args={
            "mappings": [
                {
                    "item_index": 0,
                    "source_name": "carottes bio",
                    "source_unit": "stk",
                    "existing_product_id": None,
                    "new_product_name": "Gulrot",
                    "new_product_variant": "økologisk",
                    "new_unit_symbol": "stk",
                    "aliases": [
                        {"locale": "nb", "alias": "gulrot"},
                        {"locale": "fr", "alias": "carottes bio"},
                    ],
                }
            ]
        }
    )

    result = auto_map_unmapped_ingredients(
        store,
        _configured_settings(),
        household_id=uuid4(),
        identity_service=identities,
        model=model,
    )

    assert result["created_products"] == 0
    assert result["mapped_occurrences"] == 1
    assert result["remaining_unmapped"] == 0
    assert len(repo.products) == 1

    updated = store.get("recipe", recipe.id, Recipe)
    assert updated.ingredients[0].canonical_product_id == gulrot.id
    assert (
        identities.resolve("product", "fr", "recipe", "carottes bio").candidates[0].id == gulrot.id
    )


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


def test_ai_mapper_has_no_direct_sqlalchemy_or_postgres_repo_imports():
    import ast
    from pathlib import Path

    from home_kitchen.catalogue import ai_mapper

    tree = ast.parse(Path(ai_mapper.__file__).read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {
        "sqlalchemy",
        "home_kitchen.store",
        "home_kitchen.catalogue.identity_repository",
        "home_kitchen.history",
    }
    for mod in imported_modules:
        assert not any(mod == f or mod.startswith(f"{f}.") for f in forbidden), (
            f"Unexpected direct infrastructure import in ai_mapper.py: {mod}"
        )


def test_auto_map_uses_protocol_ports_without_store_engine_or_repo_attributes():
    from home_kitchen.catalogue.identity import IdentityAliasRecord

    class PortIdentityRepo:
        def __init__(self):
            self._products: dict[tuple[str, str], CanonicalProduct] = {}
            self._units: dict[str, CanonicalUnit] = {}
            self._aliases: list[IdentityAliasRecord] = []

        def product(
            self, canonical_name: str, variant: str = "", identity_id: UUID | None = None
        ) -> CanonicalProduct:
            key = (canonical_name, variant)
            if key not in self._products:
                self._products[key] = CanonicalProduct(
                    id=identity_id or uuid4(), canonical_name=canonical_name, variant=variant
                )
            return self._products[key]

        def unit(self, canonical_name: str, identity_id: UUID | None = None) -> CanonicalUnit:
            if canonical_name not in self._units:
                self._units[canonical_name] = CanonicalUnit(
                    id=identity_id or uuid4(), canonical_name=canonical_name
                )
            return self._units[canonical_name]

        def add_alias(
            self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
        ) -> None:
            self._aliases.append(
                IdentityAliasRecord(
                    kind=kind,  # type: ignore[arg-type]
                    locale=locale,
                    context=context,
                    normalized_alias=" ".join(alias.split()).casefold(),
                    identity_id=identity_id,
                    alias=alias,
                )
            )

        def correct_alias(self, *args, **kwargs) -> None:
            return None

        def resolve(self, *args, **kwargs) -> list[CanonicalProduct | CanonicalUnit]:
            return []

        def validate_identities(self, kind: str, ids: set[UUID]) -> None:
            return None

        def list_products(self) -> list[CanonicalProduct]:
            return list(self._products.values())

        def list_units(self) -> list[CanonicalUnit]:
            return list(self._units.values())

        def list_aliases(self) -> list[IdentityAliasRecord]:
            return list(self._aliases)

    class PortRecipeMappingRepo:
        def __init__(self, recipe: Recipe, identity_repo: PortIdentityRepo):
            self._recipes = {recipe.id: recipe}
            self._identity_repo = identity_repo
            self.saved_calls: list[tuple[list[Recipe], int, UUID | None, str]] = []

        def get_recipe(self, recipe_id: UUID) -> Recipe:
            return self._recipes[recipe_id]

        def get_recipes(self, recipe_ids: list[UUID]) -> list[Recipe]:
            return [self._recipes[rid] for rid in recipe_ids if rid in self._recipes]

        def list_recipes(self) -> list[Recipe]:
            return list(self._recipes.values())

        def save_mapped_recipes(
            self,
            recipes: list[Recipe],
            *,
            mapped_occurrences: int,
            household_id: UUID | None,
            actor_id: str,
        ) -> None:
            for r in recipes:
                self._recipes[r.id] = r
            self.saved_calls.append((list(recipes), mapped_occurrences, household_id, actor_id))

        def identity_repository(self) -> PortIdentityRepo:
            return self._identity_repo

    id_repo = PortIdentityRepo()
    existing_garlic = id_repo.product("hvitløk")
    id_repo.add_alias("product", "en", "recipe", "garlic", existing_garlic.id)
    existing_clove = id_repo.unit("fedd")
    id_repo.add_alias("unit", "en", "recipe", "cloves", existing_clove.id)

    recipe = Recipe(
        id=uuid4(),
        title="Port Soup",
        servings=2,
        source_language="en",
        ingredients=[Ingredient(name="garlic", quantity="2", unit="cloves")],
        instructions=["Simmer"],
    )
    mapping_repo = PortRecipeMappingRepo(recipe, id_repo)
    household_id = uuid4()

    with patch(
        "home_kitchen.catalogue.ai_mapper.build_ingredient_mapper",
        side_effect=AssertionError("Deterministic resolution should not invoke AI"),
    ):
        result = auto_map_unmapped_ingredients(
            mapping_repo,
            _configured_settings(),
            household_id=household_id,
            actor_id="port-tester",
        )

    assert result == {
        "processed_unique": 1,
        "mapped_occurrences": 1,
        "created_products": 0,
        "created_aliases": 0,
        "remaining_unmapped": 0,
    }
    assert len(mapping_repo.saved_calls) == 1
    saved_recipes, saved_occ, saved_hh, saved_actor = mapping_repo.saved_calls[0]
    assert saved_occ == 1
    assert saved_hh == household_id
    assert saved_actor == "port-tester"
    assert saved_recipes[0].ingredients[0].canonical_product_id == existing_garlic.id
    assert saved_recipes[0].ingredients[0].canonical_unit_id == existing_clove.id


def test_engine_recipe_mapping_and_postgres_identity_catalogue_ports(tmp_path):
    from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
    from home_kitchen.store import EngineRecipeMappingRepository

    db_path = tmp_path / "ports_adapter.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    try:
        _create_import_sqlite_schema(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_canonical_products (
                        id TEXT PRIMARY KEY,
                        canonical_name TEXT NOT NULL,
                        variant TEXT NOT NULL DEFAULT '',
                        UNIQUE (canonical_name, variant)
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_canonical_units (
                        id TEXT PRIMARY KEY,
                        canonical_name TEXT NOT NULL UNIQUE
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_product_aliases (
                        locale TEXT NOT NULL,
                        context TEXT NOT NULL,
                        normalized_alias TEXT NOT NULL,
                        alias TEXT NOT NULL,
                        product_id TEXT NOT NULL,
                        reviewed BOOLEAN NOT NULL DEFAULT 0
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    CREATE TABLE kitchen_unit_aliases (
                        locale TEXT NOT NULL,
                        context TEXT NOT NULL,
                        normalized_alias TEXT NOT NULL,
                        alias TEXT NOT NULL,
                        unit_id TEXT NOT NULL,
                        reviewed BOOLEAN NOT NULL DEFAULT 0
                    )
                    """
                )
            )

            prod_id = uuid4()
            unit_id = uuid4()
            recipe_id = uuid4()
            conn.execute(
                text(
                    "INSERT INTO kitchen_canonical_products (id, canonical_name, variant) "
                    "VALUES (:id, 'gulrot', '')"
                ),
                {"id": prod_id.hex},
            )
            conn.execute(
                text("INSERT INTO kitchen_canonical_units (id, canonical_name) VALUES (:id, 'g')"),
                {"id": unit_id.hex},
            )
            conn.execute(
                text(
                    "INSERT INTO kitchen_product_aliases "
                    "(locale, context, normalized_alias, alias, product_id, reviewed) "
                    "VALUES ('en', 'recipe', 'carrot', 'carrot', :pid, 0)"
                ),
                {"pid": prod_id.hex},
            )
            conn.execute(
                text(
                    "INSERT INTO kitchen_unit_aliases "
                    "(locale, context, normalized_alias, alias, unit_id, reviewed) "
                    "VALUES ('en', 'recipe', 'grams', 'grams', :uid, 0)"
                ),
                {"uid": unit_id.hex},
            )
            conn.execute(
                text(
                    "INSERT INTO recipes "
                    "(id, position, title, servings, ingredients, instructions, source, source_language) "
                    "VALUES (:id, 1, 'Soup', 2, :ing, :inst, 'manual', 'en')"
                ),
                {
                    "id": recipe_id.hex,
                    "ing": json.dumps([{"name": "carrot", "quantity": "200", "unit": "grams"}]),
                    "inst": json.dumps(["Cook"]),
                },
            )

        id_repo = PostgresIdentityRepository(engine)
        assert [p.id for p in id_repo.list_products()] == [prod_id]
        assert [u.id for u in id_repo.list_units()] == [unit_id]
        aliases = id_repo.list_aliases()
        assert {(a.kind, a.normalized_alias, a.identity_id) for a in aliases} == {
            ("product", "carrot", prod_id),
            ("unit", "grams", unit_id),
        }

        mapping_repo = EngineRecipeMappingRepository(engine)
        assert mapping_repo.get_recipes([]) == []
        assert [r.id for r in mapping_repo.get_recipes([recipe_id, uuid4()])] == [recipe_id]
        assert [r.id for r in mapping_repo.list_recipes()] == [recipe_id]
        with pytest.raises(KeyError):
            mapping_repo.get_recipe(uuid4())

        result = auto_map_unmapped_ingredients(
            mapping_repo,
            _configured_settings(),
            recipe_id=recipe_id,
            model=TestModel(custom_output_args={"mappings": []}),
        )
        assert result == {
            "processed_unique": 1,
            "mapped_occurrences": 1,
            "created_products": 0,
            "created_aliases": 0,
            "remaining_unmapped": 0,
        }
        updated = mapping_repo.get_recipe(recipe_id)
        assert updated.ingredients[0].canonical_product_id == prod_id
        assert updated.ingredients[0].canonical_unit_id == unit_id
    finally:
        engine.dispose()
