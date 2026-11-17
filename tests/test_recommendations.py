"""Unit tests for meal-plan recommendations with large recipe catalogs."""

from datetime import date
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic_ai.usage import UsageLimits

from home_kitchen.models import Ingredient, Recipe
from home_kitchen.recommendations import PlanDraft, recommend_plan
from home_kitchen.settings import Household


def _make_recipe(title: str, ingredient_count: int = 3) -> Recipe:
    return Recipe(
        id=uuid4(),
        title=title,
        servings=4,
        ingredients=[
            Ingredient(name=f"ingrediens_{idx}_{title}", quantity="100", unit="g")
            for idx in range(ingredient_count)
        ],
        instructions=["Kok opp og server"],
    )


@pytest.mark.asyncio
async def test_recommend_plan_uses_high_token_limit_and_compacts_ingredients():
    recipes = [_make_recipe(f"Oppskrift {idx}", ingredient_count=12) for idx in range(200)]
    captured_prompt: str | None = None
    captured_limits: UsageLimits | None = None

    class CaptureAgent:
        async def run(self, prompt, **kwargs):
            nonlocal captured_prompt, captured_limits
            captured_prompt = prompt
            captured_limits = kwargs.get("usage_limits")
            res = MagicMock()
            res.output = PlanDraft(
                meals=[
                    {
                        "date": "2026-04-06",
                        "slot": "dinner",
                        "recipe_id": str(recipes[0].id),
                        "servings": 4,
                    }
                ]
            )
            return res

    draft = await recommend_plan(
        CaptureAgent(),  # type: ignore[arg-type]
        Household(),
        recipes,
        date(2026, 4, 6),
        days=7,
    )

    assert len(draft.meals) == 1
    assert captured_limits is not None
    assert captured_limits.total_tokens_limit is not None
    assert captured_limits.total_tokens_limit >= 100000
    assert captured_prompt is not None
    assert "ingrediens_0_Oppskrift 0" in captured_prompt
    assert "ingrediens_5_Oppskrift 0" in captured_prompt
    assert "(+6)" in captured_prompt
    assert "ingrediens_6_Oppskrift 0" not in captured_prompt
    assert "ingrediens_11_Oppskrift 0" not in captured_prompt
