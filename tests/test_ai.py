from datetime import date, timedelta
from uuid import uuid4

import pytest
from pydantic_ai.models.test import TestModel

from home_kitchen.ai import draft_recipe, recipe_agent
from home_kitchen.models import Recipe, RecipeCreate
from home_kitchen.recommendations import PlanDraft, build_planner, recommend_plan
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings


def recipe() -> Recipe:
    return Recipe(
        id=uuid4(),
        title="Vegetable soup",
        servings=4,
        ingredients=[{"name": "carrot", "quantity": 2}],
        instructions=["Simmer until tender"],
    )


@pytest.mark.asyncio
async def test_recipe_agent_returns_typed_draft():
    draft = await draft_recipe(
        recipe_agent(Settings(local_dev=True), TestModel()),
        Household(),
        "A simple soup",
    )
    assert isinstance(draft, RecipeCreate)
    assert draft.source == "ai"
    assert draft.source_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("meal_overrides", "message"),
    [
        ({"recipe_id": str(uuid4())}, "outside the supplied catalog"),
        ({"date": "2026-03-09"}, "outside the requested range"),
    ],
)
async def test_recommendation_rejects_hallucinated_recipe_or_date(meal_overrides, message):
    item = recipe()
    meal = {
        "date": "2026-03-02",
        "slot": "dinner",
        "recipe_id": str(item.id),
        "servings": 4,
        **meal_overrides,
    }
    agent = build_planner(Settings(local_dev=True), TestModel(custom_output_args={"meals": [meal]}))
    with pytest.raises(ValueError, match=message):
        await recommend_plan(agent, Household(), [item], date(2026, 3, 2), days=7)


@pytest.mark.asyncio
async def test_recommendation_rejects_duplicate_date_and_slot():
    item = recipe()
    meal = {"date": "2026-03-02", "slot": "dinner", "recipe_id": str(item.id), "servings": 4}
    agent = build_planner(
        Settings(local_dev=True), TestModel(custom_output_args={"meals": [meal, meal]})
    )
    with pytest.raises(ValueError, match="duplicate meal slot"):
        await recommend_plan(agent, Household(), [item], date(2026, 3, 2))


@pytest.mark.asyncio
async def test_recommendation_returns_draft_without_mutating_service(test_database_url):
    item = recipe()
    service = KitchenService(test_database_url)
    saved = service.create_recipe(RecipeCreate.model_validate(item.model_dump(exclude={"id"})))
    start = date(2026, 3, 2)
    agent = build_planner(
        Settings(local_dev=True),
        TestModel(
            custom_output_args={
                "meals": [
                    {
                        "date": (start + timedelta(days=idx)).isoformat(),
                        "slot": "dinner",
                        "recipe_id": str(saved.id),
                        "servings": 3,
                    }
                    for idx in range(7)
                ],
                "notes": ["Review portions"],
            }
        ),
    )
    draft = await recommend_plan(agent, Household(children_ages=[8]), [saved], start)
    assert isinstance(draft, PlanDraft)
    assert len(draft.meals) == 7
    assert draft.notes == ["Review portions"]
    assert service.list_meals(start, start + timedelta(days=6)) == []


@pytest.mark.asyncio
async def test_recommendation_requires_catalog_and_valid_day_count():
    agent = build_planner(Settings(local_dev=True), TestModel())
    with pytest.raises(ValueError, match="at least one recipe"):
        await recommend_plan(agent, Household(), [], date(2026, 3, 2))
    with pytest.raises(ValueError, match="between 1 and 14"):
        await recommend_plan(agent, Household(), [recipe()], date(2026, 3, 2), days=15)
