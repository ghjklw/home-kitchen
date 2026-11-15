from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pydantic_ai.models.test import TestModel

from home_kitchen.ai import recipe_agent
from home_kitchen.imports import ImportedDraft
from home_kitchen.models import RecipeCreate
from home_kitchen.recipes import (
    EvidenceSnippet,
    RecipeContext,
    RecipeDraft,
    RecipeDraftRequest,
    create_recipe_providers,
)
from home_kitchen.recipes.models import EditableRecipeDraft
from home_kitchen.settings import Household, Settings


@pytest.fixture
def recipe():
    return RecipeCreate(
        title="Soup",
        servings=2,
        ingredients=[{"name": "carrot", "quantity": 2}],
        instructions=["Simmer"],
        source="ai",
    )


@pytest.fixture
def context():
    return RecipeContext(household=Household(dietary_preferences=["vegetarian"]))


def registry():
    settings = Settings(local_dev=True)
    return create_recipe_providers(settings, recipe_agent(settings, TestModel(custom_output_args={
        "title": "Adapted soup", "servings": 9,
        "ingredients": [{"name": "salt", "quantity": None, "unit": None, "source_indices": []}],
        "instructions": ["Simmer"],
    })))


async def test_manual_provider_forces_provenance_and_preserves_input(recipe, context):
    result = await registry().draft("manual", RecipeDraftRequest(recipe=recipe), context)
    assert result.recipe is not None
    assert result.recipe.source == "manual"
    assert recipe.source == "ai"
    assert result.requires_review is True
    assert result.evidence_ids == []


async def test_web_provider_preserves_unparsed_ingredients(monkeypatch, context):
    imported = ImportedDraft(
        title="Soup",
        servings=2,
        ingredients=["a pinch of salt"],
        instructions=["Simmer"],
        source_url="https://www.matprat.no/oppskrifter/soup/",
    )
    fetch = AsyncMock(return_value=imported)
    monkeypatch.setattr("home_kitchen.recipes.providers.imports.import_recipe", fetch)
    result = await registry().draft("web", RecipeDraftRequest(url=imported.source_url), context)
    assert result.imported == imported
    assert result.recipe is None
    fetch.assert_awaited_once_with(str(imported.source_url))


async def test_ai_provider_uses_typed_agent_and_tracks_supplied_evidence(recipe, context):
    context.knowledge = [EvidenceSnippet(evidence_id="entry-1", text="Prefer simple soups")]
    result = await registry().draft("ai", RecipeDraftRequest(recipe=recipe, prompt="Simple soup"), context)
    assert result.adapted is not None
    assert result.adapted.source == "ai"
    assert result.adapted.servings == recipe.servings
    assert result.adapted.ingredients[0].quantity is None
    assert result.adapted.source_ingredients[0].text == "2 item carrot"
    assert result.evidence_ids == ["entry-1"]
    assert result.requires_review is True


async def test_ai_provider_passes_household_and_untrusted_evidence(monkeypatch, recipe, context):
    context.knowledge = [EvidenceSnippet(evidence_id="entry-1", text="Prefer simple soups")]
    generate = AsyncMock(return_value=EditableRecipeDraft(
        title="Adapted soup", servings=2, ingredients=[{"name": "carrot"}],
        instructions=["Simmer"],
    ))
    monkeypatch.setattr("home_kitchen.recipes.providers.draft_recipe", generate)
    await registry().draft("ai", RecipeDraftRequest(recipe=recipe, prompt="Soup"), context)
    assert generate.await_args.args[1:] == (context.household, "Soup")
    assert '"evidence_id":"entry-1"' in generate.await_args.kwargs["evidence_context"]


@pytest.mark.parametrize("provider", ["manual", "web", "ai"])
async def test_providers_reject_missing_and_ambiguous_inputs(provider, recipe, context):
    with pytest.raises(ValueError, match="requires"):
        await registry().draft(provider, RecipeDraftRequest(), context)
    with pytest.raises(ValueError, match="requires"):
        await registry().draft(
            provider,
            RecipeDraftRequest(prompt="Soup", recipe=recipe, url="https://www.matprat.no/"),
            context,
        )


async def test_registry_is_explicit_and_rejects_duplicate_or_unknown_providers(context):
    providers = registry()
    assert [item.id for item in providers.catalog()] == ["manual", "web", "ai"]
    with pytest.raises(ValueError, match="already registered"):
        providers.register(providers.get("manual"))
    with pytest.raises(ValueError, match="Unknown recipe provider"):
        await providers.draft("unregistered", RecipeDraftRequest(), context)


def test_draft_requires_exactly_one_payload(recipe):
    with pytest.raises(ValidationError, match="exactly one"):
        RecipeDraft(provider="manual")
    imported = ImportedDraft(
        title="Soup",
        servings=2,
        ingredients=["carrot"],
        instructions=["Simmer"],
        source_url="https://www.matprat.no/",
    )
    with pytest.raises(ValidationError, match="exactly one"):
        RecipeDraft(provider="manual", recipe=recipe, imported=imported)


async def test_prompt_only_rejected_before_agent_creation(monkeypatch, context):
    def forbidden(*args, **kwargs):
        pytest.fail("Prompt-only requests must never create a model")

    monkeypatch.setattr("home_kitchen.recipes.providers.recipe_agent", forbidden)
    providers = create_recipe_providers(Settings(local_dev=True))
    with pytest.raises(ValueError, match="requires exactly one source"):
        await providers.draft("ai", RecipeDraftRequest(prompt="Invent soup"), context)
    assert "prompt" not in providers.get("ai").capabilities


async def test_url_shape_keeps_original_words_and_locked_attribution(monkeypatch, context):
    imported = ImportedDraft(
        title="Soup", servings=2, ingredients=["a pinch of salt"],
        instructions=["Simmer"], source_url="https://www.matprat.no/soup/",
    )
    fetch = AsyncMock(return_value=imported)
    monkeypatch.setattr("home_kitchen.recipes.providers.imports.import_recipe", fetch)
    settings = Settings(local_dev=True)
    model = TestModel(custom_output_args={
        "title": "Shaped soup", "servings": 20,
        "ingredients": [{"name": "salt", "quantity": None, "unit": None,
                         "source_indices": [0]}],
        "instructions": ["Simmer"],
        "source_ingredients": [{"index": 0, "text": "model-forged source wording"}],
        "source_url": "https://model.example/forged/",
        "provenance": {"original_source": "manual"},
    })
    providers = create_recipe_providers(settings, recipe_agent(settings, model))
    result = await providers.draft("ai", RecipeDraftRequest(url=imported.source_url), context)
    adapted = result.adapted
    assert adapted is not None
    assert adapted.servings == 2
    assert adapted.source_ingredients[0].text == "a pinch of salt"
    assert adapted.ingredients[0].quantity is None
    assert adapted.ingredients[0].unit is None
    assert adapted.source_url == imported.source_url
    assert adapted.provenance.original_source == "imported"
    assert adapted.provenance.source_url == imported.source_url
    fetch.assert_awaited_once()
    # Iteration consumes the editable draft, without refetching or losing attribution.
    iteration = await providers.draft("ai", RecipeDraftRequest(draft=adapted, prompt="Simplify"), context)
    assert iteration.adapted.source_ingredients == adapted.source_ingredients
    assert iteration.adapted.source_url == imported.source_url
    fetch.assert_awaited_once()


async def test_failed_iteration_preserves_previous_draft(monkeypatch, recipe, context):
    providers = registry()
    prior = (await providers.draft("ai", RecipeDraftRequest(recipe=recipe), context)).adapted
    before = prior.model_dump_json()
    monkeypatch.setattr("home_kitchen.recipes.providers.draft_recipe", AsyncMock(side_effect=RuntimeError))
    with pytest.raises(RuntimeError):
        await providers.draft("ai", RecipeDraftRequest(draft=prior, prompt="Simplify"), context)
    assert prior.model_dump_json() == before


def test_draft_rejects_invalid_source_references():
    with pytest.raises(ValidationError, match="must reference"):
        EditableRecipeDraft(
            title="Soup", servings=2,
            ingredients=[{"name": "salt", "source_indices": [7]}], instructions=["Simmer"],
            source_ingredients=[{"index": 0, "text": "a pinch of salt"}],
        )
