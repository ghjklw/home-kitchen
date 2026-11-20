"""Unit tests for recipe language detection and on-demand versioned translations."""

from __future__ import annotations

import builtins
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from home_kitchen.app import create_app
from home_kitchen.models import (
    Ingredient,
    Recipe,
    RecipeCreate,
    RecipeTranslation,
)
from home_kitchen.recipes.language import detect_recipe_language
from home_kitchen.recipes.translation import MockRecipeTranslator, compute_recipe_version
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


class MemoryStore:
    def __init__(self):
        self.recipes: dict = {}
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

    def save(self, kind: str, entity: object) -> None:
        if kind == "recipe" and isinstance(entity, Recipe):
            self.recipes[entity.id] = entity

    def get(self, kind: str, entity_id, model: type) -> object:
        if kind == "recipe":
            if entity_id not in self.recipes:
                raise KeyError(str(entity_id))
            return self.recipes[entity_id]
        raise KeyError(str(entity_id))

    def list(self, kind: str, model: type) -> builtins.list[object]:
        if kind == "recipe":
            return list(self.recipes.values())
        return []

    def delete(self, kind: str, entity_id) -> None:
        if kind == "recipe":
            self.recipes.pop(entity_id, None)

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
        self, recipe_id, target_locale: str, recipe_version: str | None = None
    ) -> RecipeTranslation | None:
        candidates = [
            t
            for t in self.translations
            if t.recipe_id == recipe_id and t.target_locale == target_locale
        ]
        if recipe_version is not None:
            candidates = [t for t in candidates if t.recipe_version == recipe_version]
        if not candidates:
            return None
        return candidates[-1]

    def list_recipe_translations(self, recipe_id) -> builtins.list[RecipeTranslation]:
        return [t for t in reversed(self.translations) if t.recipe_id == recipe_id]


class MemoryIdentityRepository:
    def __init__(self):
        self.reviewed = set()

    def resolve(self, kind: str, locale: str, context: str, alias: str):
        from home_kitchen.catalogue.identity import AliasResolution

        return AliasResolution(status="unmapped", candidates=[])

    def validate_identities(self, kind: str, ids: set) -> None:
        pass


class MemoryIdentities:
    def resolve(self, kind: str, locale: str, context: str, alias: str):
        from home_kitchen.catalogue.identity import AliasResolution

        return AliasResolution(status="unmapped", candidates=[])

    def validate_identities(self, kind: str, ids: set) -> None:
        pass


def test_language_detection_accuracy():
    # Norwegian Bokmål
    nb_recipe = RecipeCreate(
        title="Tradisjonelle Pannekaker",
        servings=4,
        ingredients=[
            Ingredient(name="hvetemel", quantity="3", unit="dl"),
            Ingredient(name="melk", quantity="5", unit="dl"),
            Ingredient(name="egg", quantity="3", unit="item"),
            Ingredient(name="smør til steking", quantity="2", unit="ss"),
        ],
        instructions=[
            "Pisk egg og melk godt sammen i en bolle.",
            "Tilsett hvetemel litt etter litt og rør til en klumpfri røre.",
            "Stek tynne pannekaker i en varm stekepanne med smør.",
            "Server med sukker eller blåbærsyltetøy.",
        ],
    )
    assert detect_recipe_language(nb_recipe) == "nb"

    # English
    en_recipe = RecipeCreate(
        title="Classic Fluffy Pancakes",
        servings=4,
        ingredients=[
            Ingredient(name="all-purpose flour", quantity="2", unit="cups"),
            Ingredient(name="milk", quantity="2", unit="cups"),
            Ingredient(name="eggs", quantity="2", unit="item"),
            Ingredient(name="butter", quantity="2", unit="tbsp"),
        ],
        instructions=[
            "Whisk the eggs and milk together in a large mixing bowl.",
            "Add the flour and whisk until smooth.",
            "Heat a non-stick skillet over medium heat and melt a little butter.",
            "Pour batter and flip when bubbles appear; bake until golden brown.",
        ],
    )
    assert detect_recipe_language(en_recipe) == "en"

    # Danish
    da_recipe = RecipeCreate(
        title="Klassiske Frikadeller",
        servings=4,
        ingredients=[
            Ingredient(name="hakket svinekød", quantity="500", unit="g"),
            Ingredient(name="hvedemel", quantity="3", unit="spiseskefuld"),
            Ingredient(name="æg", quantity="1", unit="item"),
            Ingredient(name="mælk", quantity="1", unit="dl"),
        ],
        instructions=[
            "Rør kødet sejt med salt og tilsæt peber.",
            "Tilsæt æg og hvedemel og rør farsen godt sammen.",
            "Form frikadellerne og steg dem på panden i smør.",
            "Server med kogte kartofler og brun sovs.",
        ],
    )
    assert detect_recipe_language(da_recipe) == "da"

    # Swedish
    sv_recipe = RecipeCreate(
        title="Svenska Köttbullar",
        servings=4,
        ingredients=[
            Ingredient(name="blandfärs", quantity="500", unit="g"),
            Ingredient(name="vetemjöl", quantity="2", unit="matsked"),
            Ingredient(name="ägg", quantity="1", unit="item"),
            Ingredient(name="mjölk", quantity="1", unit="dl"),
        ],
        instructions=[
            "Blanda färs, ägg och vetemjöl i en bunke.",
            "Tillsätt mjölk och rör om till en jämn smet.",
            "Rulla köttbullar och stek dem i stekpanna med smör.",
            "Servera med potatismos och lingonsylt.",
        ],
    )
    assert detect_recipe_language(sv_recipe) == "sv"

    # Uncertain / short
    short_recipe = RecipeCreate(
        title="123",
        servings=1,
        ingredients=[Ingredient(name="foo", quantity="1", unit="item")],
        instructions=["bar"],
    )
    assert detect_recipe_language(short_recipe) == "und"


def test_recipe_create_and_update_language_detection_and_preservation():
    store = MemoryStore()
    kitchen = KitchenService(store=store, identity_service=MemoryIdentities())

    # Creating a recipe with no source_language automatically detects language
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Norsk Lapskaus",
            servings=4,
            ingredients=[
                Ingredient(name="storfekjøtt", quantity="500", unit="g"),
                Ingredient(name="poteter", quantity="4", unit="item"),
                Ingredient(name="gulrot", quantity="2", unit="item"),
            ],
            instructions=[
                "Skjær kjøttet i terninger og brun i stekepanne med smør.",
                "Tilsett grønnsaker og la det koke til alt er mørt.",
            ],
        )
    )
    assert recipe.source_language == "nb"
    # Original text is preserved exactly
    assert recipe.title == "Norsk Lapskaus"
    assert recipe.ingredients[0].name == "storfekjøtt"
    assert recipe.instructions[0].startswith("Skjær kjøttet")

    # Explicit source_language is respected
    overridden = kitchen.create_recipe(
        RecipeCreate(
            title="Norsk Lapskaus på Engelsk",
            servings=4,
            source_language="en",
            ingredients=[Ingredient(name="beef", quantity="500", unit="g")],
            instructions=["Cook beef"],
        )
    )
    assert overridden.source_language == "en"

    # Updating with explicit correction updates source_language
    updated = kitchen.update_recipe(
        recipe.id,
        RecipeCreate(
            title="Norsk Lapskaus (Korrigert)",
            servings=4,
            source_language="da",
            ingredients=recipe.ingredients,
            instructions=recipe.instructions,
        ),
    )
    assert updated.source_language == "da"


@pytest.mark.asyncio
async def test_on_demand_translation_lifecycle_and_invalidation():
    store = MemoryStore()
    mock_translator = MockRecipeTranslator()
    kitchen = KitchenService(
        store=store, identity_service=MemoryIdentities(), translator=mock_translator
    )

    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Sjokoladekake",
            servings=8,
            ingredients=[
                Ingredient(name="hvetemel", quantity="300", unit="g"),
                Ingredient(name="sukker", quantity="200", unit="g"),
                Ingredient(name="kakao", quantity="50", unit="g"),
            ],
            instructions=[
                "Bland sammen mel, sukker og kakao.",
                "Stek i ovn på 180 grader i 30 minutter.",
            ],
        )
    )
    assert recipe.source_language == "nb"

    # Step 1: Initial on-demand translation to English
    translation = await kitchen.translate_recipe(recipe.id, "en")
    assert mock_translator.call_count == 1
    assert translation.recipe_id == recipe.id
    assert translation.target_locale == "en"
    assert translation.is_stale is False
    assert translation.title == "[en] Sjokoladekake"
    assert translation.instructions == [
        "[en] Bland sammen mel, sukker og kakao.",
        "[en] Stek i ovn på 180 grader i 30 minutter.",
    ]
    # Quantities and units are preserved!
    assert translation.ingredients[0].name == "[en] hvetemel"
    assert translation.ingredients[0].quantity == 300
    assert translation.ingredients[0].unit == "g"

    # Original recipe is immutable and completely unchanged
    loaded = kitchen.get_recipe(recipe.id)
    assert loaded.title == "Sjokoladekake"
    assert loaded.ingredients[0].name == "hvetemel"

    # Step 2: Requesting saved translation does NOT trigger translator call
    reopened = await kitchen.translate_recipe(recipe.id, "en")
    assert mock_translator.call_count == 1
    assert reopened.title == translation.title

    fetched = kitchen.get_recipe_translation(recipe.id, "en")
    assert fetched is not None
    assert fetched.is_stale is False
    assert fetched.title == translation.title

    # Step 3: Editing the original recipe marks translation stale
    kitchen.update_recipe(
        recipe.id,
        RecipeCreate(
            title="Ekstra god Sjokoladekake",
            servings=8,
            source_language="nb",
            ingredients=recipe.ingredients,
            instructions=[
                "Bland sammen mel, sukker og kakao med smeltet smør.",
                "Stek i forvarmet ovn på 180 grader.",
            ],
        ),
    )

    # Translation is now detected as stale
    stale_check = kitchen.get_recipe_translation(recipe.id, "en")
    assert stale_check is not None
    assert stale_check.is_stale is True

    # Step 4: Translating refreshed stale recipe invokes translator and restores fresh state
    refreshed = await kitchen.translate_recipe(recipe.id, "en")
    assert mock_translator.call_count == 2
    assert refreshed.is_stale is False
    assert refreshed.title == "[en] Ekstra god Sjokoladekake"
    assert refreshed.instructions[0] == "[en] Bland sammen mel, sukker og kakao med smeltet smør."

    # Step 5: Same-language translation creates immediate identity copy without translator
    same_lang = await kitchen.translate_recipe(recipe.id, "nb")
    assert same_lang.provider == "source"
    assert same_lang.model == "source-language"
    assert same_lang.title == "Ekstra god Sjokoladekake"
    # Translator was NOT called for same-language
    assert mock_translator.call_count == 2


@pytest.mark.asyncio
async def test_rest_api_recipe_translation_endpoints():
    store = MemoryStore()
    mock_translator = MockRecipeTranslator()
    kitchen = KitchenService(
        store=store, identity_service=MemoryIdentities(), translator=mock_translator
    )

    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Blåbærpai",
            servings=6,
            ingredients=[Ingredient(name="blåbær", quantity="400", unit="g")],
            instructions=["Stek paien gyllen."],
        )
    )

    # Test version hash determinism
    v1 = compute_recipe_version(recipe)
    v2 = compute_recipe_version(recipe)
    assert v1 == v2

    modified = recipe.model_copy(update={"title": "Blåbærpai med vanilje"})
    v3 = compute_recipe_version(modified)
    assert v1 != v3

    app = create_app(Settings(local_dev=True), service=kitchen)
    client = TestClient(app, client=("127.0.0.1", 5000))

    # 404 on non-existent recipe translation
    res = client.get(f"/api/recipes/{recipe.id}/translations/en")
    assert res.status_code == 404

    # Post translation
    post_res = client.post(
        f"/api/recipes/{recipe.id}/translate",
        json={"target_locale": "en"},
    )
    assert post_res.status_code == 200
    data = post_res.json()
    assert data["target_locale"] == "en"
    assert data["title"] == "[en] Blåbærpai"
    assert data["is_stale"] is False

    # Get translation now returns 200
    get_res = client.get(f"/api/recipes/{recipe.id}/translations/en")
    assert get_res.status_code == 200
    assert get_res.json()["title"] == "[en] Blåbærpai"

    # List translations
    list_res = client.get(f"/api/recipes/{recipe.id}/translations")
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # Missing recipe returns 404
    missing_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/recipes/{missing_id}/translations/en").status_code == 404
    assert (
        client.post(
            f"/api/recipes/{missing_id}/translate", json={"target_locale": "en"}
        ).status_code
        == 404
    )

    # PUT manual translation edit updates saved translation without modifying original recipe
    put_res = client.put(
        f"/api/recipes/{recipe.id}/translations/en",
        json={
            "title": "Blueberry Pie (Edited)",
            "ingredients": [{"name": "blueberries", "quantity": "400", "unit": "g"}],
            "instructions": ["Bake the pie until golden brown."],
        },
    )
    assert put_res.status_code == 200
    edited = put_res.json()
    assert edited["title"] == "Blueberry Pie (Edited)"
    assert edited["ingredients"][0]["name"] == "blueberries"
    assert edited["instructions"] == ["Bake the pie until golden brown."]
    assert edited["provider"] == "manual"
    assert edited["model"] == "manual"
    assert edited["is_stale"] is False

    # GET reflects the manually edited translation
    get_edited = client.get(f"/api/recipes/{recipe.id}/translations/en")
    assert get_edited.status_code == 200
    assert get_edited.json()["title"] == "Blueberry Pie (Edited)"

    # Original recipe remains completely unchanged
    orig = client.get(f"/api/recipes/{recipe.id}").json()
    assert orig["title"] == "Blåbærpai"
    assert orig["ingredients"][0]["name"] == "blåbær"
    assert orig["instructions"] == ["Stek paien gyllen."]

    # PUT also works to create a manual translation for a new locale without AI
    put_fr = client.put(
        f"/api/recipes/{recipe.id}/translations/fr",
        json={
            "title": "Tarte aux myrtilles",
            "ingredients": [{"name": "myrtilles", "quantity": "400", "unit": "g"}],
            "instructions": ["Cuire la tarte jusqu'à ce qu'elle soit dorée."],
        },
    )
    assert put_fr.status_code == 200
    assert put_fr.json()["target_locale"] == "fr"
    assert put_fr.json()["provider"] == "manual"

    # PUT for missing recipe returns 404; invalid payload returns 422
    assert (
        client.put(
            f"/api/recipes/{missing_id}/translations/en",
            json={
                "title": "Missing",
                "ingredients": [{"name": "flour", "quantity": "100", "unit": "g"}],
                "instructions": ["Mix."],
            },
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/recipes/{recipe.id}/translations/en",
            json={"title": "   ", "ingredients": [], "instructions": []},
        ).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_fastmcp_translation_tools():
    from fastmcp import Client

    from home_kitchen.mcp_server import build_mcp

    store = MemoryStore()
    mock_translator = MockRecipeTranslator()
    kitchen = KitchenService(
        store=store, identity_service=MemoryIdentities(), translator=mock_translator
    )

    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Vafler",
            servings=4,
            ingredients=[Ingredient(name="hvetemel", quantity="200", unit="g")],
            instructions=["Stek i vaffeljern."],
        )
    )

    mcp = build_mcp(kitchen, lambda: Household(name="Test"))

    async with Client(mcp) as client:
        # Call translate_recipe tool
        translated = await client.call_tool(
            "translate_recipe",
            {"recipe_id": recipe.id, "target_locale": "en"},
        )
        content = translated.structured_content
        result = content.get("result", content)
        assert result["target_locale"] == "en"
        assert result["title"] == "[en] Vafler"
        assert result["is_stale"] is False

        # Call get_recipe_translation tool
        fetched = await client.call_tool(
            "get_recipe_translation",
            {"recipe_id": recipe.id, "target_locale": "en"},
        )
        content_f = fetched.structured_content
        result_f = content_f.get("result", content_f)
        assert result_f["title"] == "[en] Vafler"
        assert result_f["is_stale"] is False


@pytest.mark.asyncio
async def test_ai_recipe_translator_with_test_model():
    from pydantic_ai.models.test import TestModel

    from home_kitchen.recipes.translation import AIRecipeTranslator

    settings = Settings(local_dev=True, ai_model="claude-3-7-sonnet")
    test_model = TestModel(
        custom_output_args={
            "title": "Translated Pancakes",
            "instructions": ["Step one in English", "Step two in English"],
            "ingredient_names": ["Flour", "Milk"],
        }
    )
    translator = AIRecipeTranslator(settings, model=test_model)

    recipe = Recipe(
        id=uuid4(),
        title="Pannekaker",
        servings=4,
        source_language="nb",
        ingredients=[
            Ingredient(name="hvetemel", quantity="200", unit="g"),
            Ingredient(name="melk", quantity="4", unit="dl"),
        ],
        instructions=["Trinn en på norsk", "Trinn to på norsk"],
    )

    payload = await translator.translate(recipe, "en")
    assert payload.title == "Translated Pancakes"
    assert payload.instructions == ["Step one in English", "Step two in English"]
    assert len(payload.ingredients) == 2
    assert payload.ingredients[0].name == "Flour"
    assert payload.ingredients[0].quantity == 200
    assert payload.ingredients[0].unit == "g"
    assert payload.ingredients[1].name == "Milk"
    assert payload.ingredients[1].quantity == 4
    assert payload.ingredients[1].unit == "dl"
    assert payload.provider == "anthropic"


def test_postgres_recipe_translations_persistence_and_cascade(test_database_url):
    service = KitchenService(test_database_url)
    recipe = service.create_recipe(
        RecipeCreate(
            title="Pannekaker",
            servings=4,
            ingredients=[Ingredient(name="hvetemel", quantity="200", unit="g")],
            instructions=["Pisk og stek i panne."],
        )
    )
    assert recipe.source_language == "nb"

    # Store translation
    version = compute_recipe_version(recipe)
    translation = RecipeTranslation(
        id=uuid4(),
        recipe_id=recipe.id,
        target_locale="en",
        recipe_version=version,
        title="Pancakes",
        instructions=["Whisk and fry in pan."],
        ingredients=[Ingredient(name="flour", quantity="200", unit="g")],
        provider="test",
        model="test-model",
    )
    service.store.save_recipe_translation(translation)

    # Query translation
    loaded = service.store.get_recipe_translation(recipe.id, "en")
    assert loaded is not None
    assert loaded.title == "Pancakes"
    assert loaded.recipe_version == version

    # Query via service
    service_loaded = service.get_recipe_translation(recipe.id, "en")
    assert service_loaded is not None
    assert service_loaded.is_stale is False

    # Update recipe
    service.update_recipe(
        recipe.id,
        RecipeCreate(
            title="Oppdaterte Pannekaker",
            servings=4,
            ingredients=[Ingredient(name="hvetemel", quantity="250", unit="g")],
            instructions=["Pisk og stek."],
        ),
    )
    stale_loaded = service.get_recipe_translation(recipe.id, "en")
    assert stale_loaded is not None
    assert stale_loaded.is_stale is True

    # Delete recipe -> translations cascade
    service.delete_recipe(recipe.id)
    assert service.store.get_recipe_translation(recipe.id, "en") is None
