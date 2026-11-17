"""Explicitly registered recipe providers; drafts never write to the recipe store."""

from collections.abc import Iterable
from typing import Protocol

from pydantic_ai import Agent

from home_kitchen import imports
from home_kitchen.ai import draft_recipe, recipe_agent
from home_kitchen.models import RecipeCreate, RecipeSource
from home_kitchen.recipes.models import (
    AdaptationProvenance,
    DraftIngredient,
    EditableRecipeDraft,
    RecipeContext,
    RecipeDraft,
    RecipeDraftRequest,
    RecipeProviderInfo,
    SourceIngredient,
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
    label = "Public recipe page (Matprat, Marmiton, Oda)"
    capabilities: tuple[str, ...] = ("url",)

    async def draft(self, request: RecipeDraftRequest, context: RecipeContext) -> RecipeDraft:
        require_input(request, "url")
        imported = await imports.import_recipe(str(request.url))
        return RecipeDraft(provider=self.provider_id, imported=imported)


class AIRecipeProvider:
    provider_id = "ai"
    label = "AI source adaptation"
    capabilities: tuple[str, ...] = (
        "source_recipe",
        "source_draft",
        "source_imported",
        "url",
        "modification_prompt",
        "household_context",
        "knowledge_context",
    )

    def __init__(
        self,
        settings: Settings,
        agent: Agent[None, EditableRecipeDraft] | Agent[None, RecipeCreate] | None = None,
    ):
        self._settings = settings
        self._agent = agent

    async def draft(self, request: RecipeDraftRequest, context: RecipeContext) -> RecipeDraft:
        request.require_ai_source()
        source = request.draft
        imported = (
            await imports.import_recipe(str(request.url))
            if request.url is not None
            else request.imported
        )
        if imported is not None:
            source = EditableRecipeDraft(
                title=imported.title,
                servings=imported.servings,
                ingredients=[
                    DraftIngredient(name=text, source_indices=[index])
                    for index, text in enumerate(imported.ingredients)
                ],
                instructions=imported.instructions,
                source_url=imported.source_url,
                image_url=imported.image_url,
                source_ingredients=[
                    SourceIngredient(index=index, text=text)
                    for index, text in enumerate(imported.ingredients)
                ],
                provenance=AdaptationProvenance(
                    original_source=RecipeSource.IMPORTED,
                    source_url=imported.source_url,
                ),
                recipe_type=imported.recipe_type,
                prep_time_minutes=imported.prep_time_minutes,
                prep_category=imported.prep_category,
            )
        elif request.recipe is not None:
            recipe = request.recipe
            source = EditableRecipeDraft(
                **recipe.model_dump(
                    exclude={"source", "ingredients", "source_ingredients", "provenance"}
                ),
                ingredients=[
                    DraftIngredient(
                        **item.model_dump(exclude={"source_indices"}),
                        source_indices=item.source_indices
                        if recipe.source_ingredients
                        else [index],
                    )
                    for index, item in enumerate(recipe.ingredients)
                ],
                source_ingredients=recipe.source_ingredients
                or [
                    SourceIngredient(index=index, text=f"{item.quantity} {item.unit} {item.name}")
                    for index, item in enumerate(recipe.ingredients)
                ],
                provenance=recipe.provenance
                or AdaptationProvenance(
                    original_source=recipe.source,
                    source_url=recipe.source_url,
                ),
            )
        assert source is not None
        if not source.source_ingredients:
            # Unsaved editable drafts can arrive before they have attribution records.
            # Retain their actual input fields, including unknown quantities and units.
            source = source.model_copy(
                update={
                    "source_ingredients": [
                        SourceIngredient(
                            index=index,
                            text=" ".join(
                                str(value)
                                for value in (item.quantity, item.unit, item.name)
                                if value is not None
                            ),
                        )
                        for index, item in enumerate(source.ingredients)
                    ],
                }
            )
        provenance = source.provenance or AdaptationProvenance(
            original_source=RecipeSource.AI,
            source_url=source.source_url,
        )
        source = source.model_copy(
            update={
                "provenance": provenance.model_copy(update={"source_url": source.source_url}),
            }
        )
        if self._agent is None:
            if not self._settings.ai_configured:
                raise ValueError("Configure an AI model and API credential to adapt recipes")
            self._agent = recipe_agent(self._settings)
        adapted = await draft_recipe(
            self._agent,
            context.household,
            request.prompt or "Normalize the supplied source into an editable recipe draft",
            source=source,
            evidence_context="\n".join(item.model_dump_json() for item in context.knowledge),
            actor_id=context.actor_id,
            preserve_source_measures=imported is not None and request.prompt is None,
        )
        return RecipeDraft(
            provider=self.provider_id,
            adapted=adapted,
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
    settings: Settings,
    ai_agent: Agent[None, EditableRecipeDraft] | Agent[None, RecipeCreate] | None = None,
) -> RecipeProviderRegistry:
    return RecipeProviderRegistry(
        [ManualRecipeProvider(), WebRecipeProvider(), AIRecipeProvider(settings, ai_agent)]
    )
