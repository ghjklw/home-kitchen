from datetime import date
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from fastmcp import Client
from test_api import make_client

from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import Ingredient, MealCreate, RecipeCreate
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household


def seed(service):
    recipe = service.create_recipe(
        RecipeCreate(
            title="Rice",
            servings=3,
            ingredients=[Ingredient(name="Rice", quantity=Decimal("100"), unit="g")],
            instructions=["Cook 15 minutes at 180 C."],
        )
    )
    meal = service.plan_meal(MealCreate(date=date(2026, 12, 1), recipe_id=recipe.id, servings=7))
    return recipe, meal


def test_undated_meals_anchor_to_monday_of_current_week():
    meal = MealCreate(recipe_id=uuid4(), servings=2)
    today = date.today()
    expected_monday = date.fromordinal(today.toordinal() - today.weekday())
    assert meal.date == expected_monday
    assert meal.date.weekday() == 0


def test_cooking_and_shopping_share_scaling_without_changing_saved_recipe(test_database_url):
    service = KitchenService(test_database_url)
    recipe, meal = seed(service)
    with localcontext() as context:
        context.prec = 4
        view = service.meal_recipe(meal.id)
        shopping = service.shopping_list(meal.date, meal.date, meal.date)
    assert view.servings == 7
    assert view.ingredients[0].quantity == shopping.lines[0].quantity
    assert view.ingredients[0].quantity == Decimal("233.3333333333333333333333333")
    assert view.source_recipe == recipe == service.get_recipe(recipe.id)
    assert view.source_recipe.servings == 3
    assert view.source_recipe.instructions == ["Cook 15 minutes at 180 C."]


def test_meal_recipe_api_uses_planned_servings_and_missing_id_is_404(test_database_url):
    service = KitchenService(test_database_url)
    recipe, meal = seed(service)
    with make_client(test_database_url) as client:
        response = client.get(f"/api/meals/{meal.id}/recipe")
        assert response.status_code == 200
        assert response.json()["servings"] == 7
        assert response.json()["source_recipe"]["id"] == str(recipe.id)
        assert (
            client.get("/api/meals/00000000-0000-0000-0000-000000000001/recipe").status_code == 404
        )


@pytest.mark.asyncio
async def test_mcp_cooking_view_uses_shared_service(test_database_url):
    service = KitchenService(test_database_url)
    _, meal = seed(service)
    async with Client(build_mcp(service, Household)) as client:
        result = await client.call_tool("get_planned_meal_recipe", {"meal_id": str(meal.id)})
        data = result.structured_content
        assert data["servings"] == 7
        assert (
            Decimal(data["ingredients"][0]["quantity"])
            == service.meal_recipe(meal.id).ingredients[0].quantity
        )
