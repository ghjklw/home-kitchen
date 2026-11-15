"""Explicitly registered recipe providers; drafts never write to the recipe store."""

from collections.abc import Iterable
from typing import Protocol

from pydantic_ai import Agent

from home_kitchen import imports
from home_kitchen.ai import draft_recipe, recipe_agent
from home_kitchen.models import RecipeCreate, RecipeSource
from home_kitchen.recipes.models import (
    RecipeContext,
    RecipeDraft,
    RecipeDraftRequest,
    RecipeProviderInfo,
)
from home_kitchen.settings import Settings


class RecipeProvider(Protocol):
    provider_id: str
    label: str
    capabilities: tuple[str, ...]

    async def draft(self, request: RecipeDraftRequest, context: RecipeContext) -> RecipeDraft: ...


def require_input(request: RecipeDraftRequest, field: str) -> None:
    supplied = request.model_dump(exclude_none=True)
    if set(supplied) != {field}:
        raise ValueError(f"This recipe provider requires only the '{field}' input")


class ManualRecipeProvider:
    provider_id = "manual"
    label = "Manual recipe"
    capabilities: tuple[str, ...] = ("recipe",)

    async def draft(self, request: RecipeDraftRequest, context: RecipeContext) -> RecipeDraft:
        require_input(request, "recipe")
        assert request.recipe is not None
        recipe = request.recipe.model_copy(update={"source": RecipeSource.MANUAL})
        return RecipeDraft(provider=self.provider_id, recipe=recipe)


class WebRecipeProvider:
    provider_id = "web"
    label = "Recipe website"
    capabilities: tuple[str, ...] = ("url",)

    async def draft(self, request: RecipeDraftRequest, context: RecipeContext) -> RecipeDraft:
        require_input(request, "url")
        imported = await imports.import_recipe(str(request.url))
        return RecipeDraft(provider=self.provider_id, imported=imported)


class AIRecipeProvider:
    provider_id = "ai"
    label = "AI recipe draft"
    capabilities: tuple[str, ...] = ("prompt", "household_context", "knowledge_context")

    def __init__(self, settings: Settings, agent: Agent[None, RecipeCreate] | None = None):
        self._settings = settings
        self._agent = agent

    async def draft(self, request: RecipeDraftRequest, context: RecipeContext) -> RecipeDraft:
        require_input(request, "prompt")
        assert request.prompt is not None
        if self._agent is None:
            self._agent = recipe_agent(self._settings)
        recipe = await draft_recipe(
            self._agent,
            context.household,
            request.prompt,
            evidence_context="\n".join(item.model_dump_json() for item in context.knowledge),
        )
        return RecipeDraft(
            provider=self.provider_id,
            recipe=recipe,
            evidence_ids=list(dict.fromkeys(item.evidence_id for item in context.knowledge)),
        )


class RecipeProviderRegistry:
    def __init__(self, providers: Iterable[RecipeProvider] = ()):
        self._providers: dict[str, RecipeProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: RecipeProvider) -> None:
        if not provider.provider_id.strip():
            raise ValueError("Recipe provider ID must not be blank")
        if provider.provider_id in self._providers:
            raise ValueError(f"Recipe provider already registered: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> RecipeProvider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ValueError(f"Unknown recipe provider: {provider_id}") from None

    def catalog(self) -> list[RecipeProviderInfo]:
        return [
            RecipeProviderInfo(
                id=provider.provider_id,
                label=provider.label,
                capabilities=list(provider.capabilities),
            )
            for provider in self._providers.values()
        ]

    async def draft(
        self, provider_id: str, request: RecipeDraftRequest, context: RecipeContext
    ) -> RecipeDraft:
        return await self.get(provider_id).draft(request, context)


def create_recipe_providers(
    settings: Settings, ai_agent: Agent[None, RecipeCreate] | None = None
) -> RecipeProviderRegistry:
    return RecipeProviderRegistry(
        [ManualRecipeProvider(), WebRecipeProvider(), AIRecipeProvider(settings, ai_agent)]
    )
