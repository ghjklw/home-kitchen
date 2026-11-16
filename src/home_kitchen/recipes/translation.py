"""On-demand recipe translations with content versioning and original preservation."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from home_kitchen.models import (
    Ingredient,
    NonBlank,
    Recipe,
    RecipeCreate,
)

if TYPE_CHECKING:
    from home_kitchen.settings import Settings


def compute_recipe_version(recipe: Recipe | RecipeCreate) -> str:
    """Compute a deterministic content hash of the recipe's translatable fields.

    Invalidates translations whenever title, instructions, or ingredients change.
    """
    content = {
        "title": recipe.title.strip(),
        "instructions": [step.strip() for step in recipe.instructions],
        "ingredients": [
            {
                "name": ing.name.strip(),
                "quantity": str(ing.quantity) if ing.quantity is not None else None,
                "unit": ing.unit.strip() if ing.unit else None,
                "category": ing.category.strip() if ing.category else "Other",
            }
            for ing in recipe.ingredients
        ],
    }
    dumped = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


class RecipeTranslationDraft(BaseModel):
    """Draft returned by AI model containing translated text only."""

    title: NonBlank
    instructions: list[NonBlank] = Field(min_length=1)
    ingredient_names: list[NonBlank] = Field(min_length=1)


class RecipeTranslationPayload(BaseModel):
    """Fully formed translated payload ready for persistence."""

    title: NonBlank
    instructions: list[NonBlank] = Field(min_length=1)
    ingredients: list[Ingredient] = Field(min_length=1)
    provider: NonBlank
    model: NonBlank


@runtime_checkable
class RecipeTranslator(Protocol):
    """Protocol for recipe translation backends."""

    async def translate(
        self,
        recipe: Recipe,
        target_locale: str,
    ) -> RecipeTranslationPayload: ...


class MockRecipeTranslator:
    """In-memory translator for offline testing without external AI dependencies."""

    def __init__(self, prefix: str = "[translated] "):
        self.prefix = prefix
        self.call_count = 0

    async def translate(
        self,
        recipe: Recipe,
        target_locale: str,
    ) -> RecipeTranslationPayload:
        self.call_count += 1
        tag = f"[{target_locale}] "
        translated_ingredients = [
            ing.model_copy(update={"name": f"{tag}{ing.name}"}) for ing in recipe.ingredients
        ]
        return RecipeTranslationPayload(
            title=f"{tag}{recipe.title}",
            instructions=[f"{tag}{step}" for step in recipe.instructions],
            ingredients=translated_ingredients,
            provider="mock",
            model="mock-translator",
        )


class AIRecipeTranslator:
    """Translates recipes using the configured household AI provider."""

    def __init__(self, settings: Settings, model: Any = None):
        self.settings = settings
        self.model = model

    def _agent(self):
        from pydantic_ai import Agent

        from home_kitchen.ai_credentials import anthropic_model

        model = self.model
        if model is None:
            model = anthropic_model(self.settings)

        return Agent(
            model,
            output_type=RecipeTranslationDraft,
            instructions=(
                "Translate the supplied recipe into the requested target locale. "
                "Preserve recipe structure, culinary terminology, temperatures, and timings. "
                "Translate each ingredient name accurately without changing quantities, units, "
                "or adding/removing ingredients. "
                "Return the translated title, each instruction step in order, and each ingredient name in order."
            ),
            retries=1,
            model_settings={"max_tokens": 3000, "timeout": 45},
        )

    async def translate(
        self,
        recipe: Recipe,
        target_locale: str,
    ) -> RecipeTranslationPayload:
        from pydantic_ai.usage import UsageLimits

        agent = self._agent()
        ingredients_text = "\n".join(
            f"{i + 1}. {ing.name} ({ing.quantity or ''} {ing.unit or ''})"
            for i, ing in enumerate(recipe.ingredients)
        )
        instructions_text = "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(recipe.instructions)
        )

        prompt = (
            f"Target locale: {target_locale}\n"
            f"Recipe title: {recipe.title}\n"
            f"Ingredients:\n{ingredients_text}\n"
            f"Instructions:\n{instructions_text}\n"
        )

        result = await agent.run(
            prompt,
            usage_limits=UsageLimits(request_limit=2, total_tokens_limit=12000),
        )
        draft = result.output

        # Re-attach original measurements and references; only names are translated
        translated_ingredients: list[Ingredient] = []
        for index, orig in enumerate(recipe.ingredients):
            if index < len(draft.ingredient_names):
                new_name = draft.ingredient_names[index]
            else:
                new_name = orig.name
            translated_ingredients.append(orig.model_copy(update={"name": new_name}))

        return RecipeTranslationPayload(
            title=draft.title,
            instructions=draft.instructions,
            ingredients=translated_ingredients,
            provider="anthropic",
            model=getattr(self.settings, "ai_model", "anthropic"),
        )
