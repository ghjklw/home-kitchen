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
    return create_recipe_providers(settings, recipe_agent(settings, TestModel()))


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


async def test_ai_provider_uses_typed_agent_and_tracks_supplied_evidence(context):
    context.knowledge = [EvidenceSnippet(evidence_id="entry-1", text="Prefer simple soups")]
    result = await registry().draft("ai", RecipeDraftRequest(prompt="Simple soup"), context)
    assert result.recipe is not None
    assert result.recipe.source == "ai"
    assert result.recipe.source_url is None
    assert result.evidence_ids == ["entry-1"]
    assert result.requires_review is True


async def test_ai_provider_passes_household_and_untrusted_evidence(monkeypatch, recipe, context):
    context.knowledge = [EvidenceSnippet(evidence_id="entry-1", text="Prefer simple soups")]
    generate = AsyncMock(return_value=recipe)
    monkeypatch.setattr("home_kitchen.recipes.providers.draft_recipe", generate)
    await registry().draft("ai", RecipeDraftRequest(prompt="Soup"), context)
    assert generate.await_args.args[1:] == (context.household, "Soup")
    assert '"evidence_id":"entry-1"' in generate.await_args.kwargs["evidence_context"]


@pytest.mark.parametrize("provider", ["manual", "web", "ai"])
async def test_providers_reject_missing_and_ambiguous_inputs(provider, recipe, context):
    with pytest.raises(ValueError, match="requires only"):
        await registry().draft(provider, RecipeDraftRequest(), context)
    with pytest.raises(ValueError, match="requires only"):
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
