from datetime import date, time, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic_ai.models.test import TestModel

from home_kitchen.ai import draft_recipe, recipe_agent
from home_kitchen.features import KitchenFeatures
from home_kitchen.models import Meal, MealEntry, Recipe, RecipeCreate
from home_kitchen.recipes.models import EditableRecipeDraft
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
        recipe_agent(
            Settings(local_dev=True),
            TestModel(
                custom_output_args={
                    "title": "Soup",
                    "servings": 99,
                    "ingredients": [{"name": "salt", "quantity": None, "unit": None}],
                    "instructions": ["Simmer"],
                    "source_url": "https://model.example/forged",
                }
            ),
        ),
        Household(),
        "A simple soup",
        source=EditableRecipeDraft(
            title="Source soup",
            servings=2,
            ingredients=[{"name": "salt"}],
            instructions=["Simmer"],
            source_url="https://www.matprat.no/soup/",
        ),
    )
    assert isinstance(draft, EditableRecipeDraft)
    assert draft.source == "ai"
    assert str(draft.source_url) == "https://www.matprat.no/soup/"
    assert draft.servings == 2
    assert draft.ingredients[0].quantity is None
    assert draft.ingredients[0].unit is None


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
async def test_recommendation_allows_duplicate_date_and_recipe():
    item = recipe()
    meal = {"date": "2026-03-02", "slot": "dinner", "recipe_id": str(item.id), "servings": 4}
    agent = build_planner(
        Settings(local_dev=True), TestModel(custom_output_args={"meals": [meal, meal]})
    )
    draft = await recommend_plan(agent, Household(), [item], date(2026, 3, 2))
    assert len(draft.meals) == 2


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


@pytest.mark.asyncio
async def test_recommendation_supports_additions_and_explicit_meal_id_edits_separately():
    soup = recipe()
    pasta = Recipe(
        id=uuid4(),
        title="Tomato pasta",
        servings=2,
        ingredients=[{"name": "pasta", "quantity": 200, "unit": "g"}],
        instructions=["Boil pasta"],
    )
    start = date(2026, 3, 2)
    existing = Meal(
        id=uuid4(),
        date=start,
        recipe_id=soup.id,
        servings=2,
        label="Middag",
        scheduled_time=time(17, 30),
    )
    agent = build_planner(
        Settings(local_dev=True),
        TestModel(
            custom_output_args={
                "meal_entries": [
                    # Edit existing meal on 2026-03-02
                    {
                        "meal_id": str(existing.id),
                        "date": start.isoformat(),
                        "recipe_id": str(pasta.id),
                        "servings": 4,
                        "label": "Middag",
                    },
                    # Addition sharing the exact same date and repeating pasta twice distinctly
                    {
                        "meal_id": None,
                        "date": start.isoformat(),
                        "recipe_id": str(pasta.id),
                        "servings": 2,
                        "label": "Lunsj",
                    },
                    {
                        "meal_id": None,
                        "date": (start + timedelta(days=1)).isoformat(),
                        "recipe_id": str(pasta.id),
                        "servings": 3,
                    },
                ],
                "notes": ["Behold to pasta-dager etter ønske"],
            }
        ),
    )
    draft = await recommend_plan(
        agent,
        Household(),
        [soup, pasta],
        start,
        days=7,
        existing_meals=[existing],
    )
    assert len(draft.meal_entries) == 3
    assert len(draft.additions) == 2
    assert len(draft.edits) == 1
    assert draft.edits[0].meal_id == existing.id
    assert draft.edits[0].recipe_id == pasta.id
    assert all(entry.meal_id is None for entry in draft.additions)
    assert [entry.date for entry in draft.additions] == [start, start + timedelta(days=1)]
    assert draft.existing_meals[str(existing.id)] == existing


@pytest.mark.asyncio
async def test_recommendation_rejects_unknown_or_duplicate_meal_id_edits():
    item = recipe()
    start = date(2026, 3, 2)
    existing = Meal(id=uuid4(), date=start, recipe_id=item.id, servings=2)
    unknown_agent = build_planner(
        Settings(local_dev=True),
        TestModel(
            custom_output_args={
                "meal_entries": [
                    {
                        "meal_id": str(uuid4()),
                        "date": start.isoformat(),
                        "recipe_id": str(item.id),
                        "servings": 2,
                    }
                ]
            }
        ),
    )
    with pytest.raises(ValueError, match="unknown existing meal ID"):
        await recommend_plan(
            unknown_agent, Household(), [item], start, days=7, existing_meals=[existing]
        )

    duplicate_agent = build_planner(
        Settings(local_dev=True),
        TestModel(
            custom_output_args={
                "meal_entries": [
                    {
                        "meal_id": str(existing.id),
                        "date": start.isoformat(),
                        "recipe_id": str(item.id),
                        "servings": 2,
                    },
                    {
                        "meal_id": str(existing.id),
                        "date": start.isoformat(),
                        "recipe_id": str(item.id),
                        "servings": 4,
                    },
                ]
            }
        ),
    )
    with pytest.raises(ValueError, match="duplicate edits"):
        await recommend_plan(
            duplicate_agent, Household(), [item], start, days=7, existing_meals=[existing]
        )


@pytest.mark.asyncio
async def test_recommend_week_passes_existing_meals_without_mutating(monkeypatch):
    item = recipe()
    start = date(2026, 3, 2)
    existing = Meal(id=uuid4(), date=start, recipe_id=item.id, servings=2, label="Middag")
    fake_store = MagicMock()
    fake_store.engine = None
    fake_store.ensure_setting.return_value = str(uuid4())
    fake_kitchen = MagicMock(spec=KitchenService)
    fake_kitchen.store = fake_store
    fake_kitchen.household_id = uuid4()
    fake_kitchen.settings = Settings(local_dev=True)
    fake_kitchen.identities = MagicMock()
    fake_kitchen.list_recipes.return_value = [item]
    fake_kitchen.list_meals.return_value = [existing]

    features = KitchenFeatures(fake_kitchen, Settings(local_dev=True), Household)
    features.evidence = MagicMock(return_value=[])  # type: ignore[method-assign]
    test_agent = build_planner(
        Settings(local_dev=True),
        TestModel(
            custom_output_args={
                "meal_entries": [
                    {
                        "meal_id": str(existing.id),
                        "date": start.isoformat(),
                        "recipe_id": str(item.id),
                        "servings": 5,
                    },
                    {
                        "meal_id": None,
                        "date": start.isoformat(),
                        "recipe_id": str(item.id),
                        "servings": 2,
                    },
                ]
            }
        ),
    )
    monkeypatch.setattr("home_kitchen.features.build_planner", lambda _settings: test_agent)

    draft = await features.recommend_week(start, days=7)
    fake_kitchen.list_meals.assert_called_once_with(start, start + timedelta(days=6))
    assert len(draft.additions) == 1
    assert len(draft.edits) == 1
    assert isinstance(draft.edits[0], MealEntry)
    assert draft.existing_meals[str(existing.id)] == existing
    fake_kitchen.plan_meal.assert_not_called()
    fake_kitchen.update_meal.assert_not_called()
