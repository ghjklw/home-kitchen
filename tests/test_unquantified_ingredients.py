"""Unknown source quantities stay explicit throughout saving and purchasing."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from home_kitchen.basket.repository import BasketService
from home_kitchen.history import HistoryRepository
from home_kitchen.inventory import InventoryItemCreate, InventoryService
from home_kitchen.models import Ingredient, MealCreate, RecipeCreate, SourceIngredient
from home_kitchen.purchasing import StoreTripService
from home_kitchen.service import KitchenService

START = date(2026, 11, 16)
END = date(2026, 11, 22)


@pytest.fixture
def saved_meal(test_database_url):
    kitchen = KitchenService(test_database_url)
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Seasoned rice",
            servings=2,
            ingredients=[
                Ingredient(name="Rice", quantity=100, unit="g", source_indices=[0]),
                Ingredient(name="Salt", quantity=None, unit=None, source_indices=[1]),
                Ingredient(name="Oil", quantity=None, unit="ml", source_indices=[2]),
            ],
            instructions=["Cook 100 g rice, then season to taste."],
            source_ingredients=[
                SourceIngredient(index=0, text="100 g rice"),
                SourceIngredient(index=1, text="Salt to taste"),
                SourceIngredient(index=2, text="Oil as needed"),
            ],
        )
    )
    meal = kitchen.plan_meal(MealCreate(date=START, recipe_id=recipe.id, servings=4))
    return kitchen, recipe, meal


def test_explicit_null_save_reload_and_original_wording(saved_meal):
    kitchen, recipe, _ = saved_meal
    reloaded = kitchen.get_recipe(recipe.id)
    assert reloaded == recipe
    assert reloaded.ingredients[1].quantity is None
    assert reloaded.ingredients[1].unit is None
    assert reloaded.ingredients[1].source_indices == [1]
    assert reloaded.source_ingredients[1].text == "Salt to taste"
    assert reloaded.source_ingredients[2].text == "Oil as needed"
    with kitchen.store.engine.connect() as connection:
        payload = connection.execute(
            text("SELECT ingredients FROM recipes WHERE id=:id"), {"id": recipe.id}
        ).scalar_one()
    assert payload[1]["quantity"] is None
    assert payload[1]["unit"] is None
    assert payload[0]["quantity"] == "100"


def test_measured_validation_and_legacy_shape_are_preserved():
    assert Ingredient.model_validate({"name": "Rice", "quantity": "100"}).unit == "item"
    assert Ingredient(name="Seasoning", quantity=None, unit=None).quantity is None
    with pytest.raises(ValidationError):
        Ingredient.model_validate({"name": "Seasoning", "unit": None})
    with pytest.raises(ValidationError, match="require a unit"):
        Ingredient(name="Rice", quantity=100, unit=None)
    for quantity in [0, -1, "NaN", "Infinity"]:
        with pytest.raises(ValidationError):
            Ingredient(name="Rice", quantity=quantity)


def test_unknown_need_is_separate_from_numeric_need_and_stock(saved_meal):
    kitchen, _, meal = saved_meal
    history = HistoryRepository(kitchen.store.engine)
    inventory = InventoryService(kitchen, history)
    inventory.upsert(InventoryItemCreate(name="Salt", quantity=1000, unit="g"))
    inventory.upsert(InventoryItemCreate(name="Oil", quantity=1000, unit="ml"))
    inventory.upsert(InventoryItemCreate(name="Rice", quantity=50, unit="g"))
    week = kitchen.shopping_list(START, END, today=START)
    assert [(line.name, line.quantity) for line in week.lines] == [("Rice", Decimal(200))]
    assert [need.name for need in week.unquantified] == ["Salt", "Oil"]
    assert week.unquantified[0].source_indices == [1]
    assert week.unquantified[0].origins == [f"meal:{meal.id}"]
    plan = inventory.plan(START, END)
    assert plan.unquantified == week.unquantified
    assert [(line.name, line.to_buy_quantity) for line in plan.purchase_lines] == [
        ("Rice", Decimal(150))
    ]
    assert inventory.list()[0].quantity > 0


def test_cooking_scales_only_measured_ingredients(saved_meal):
    kitchen, recipe, meal = saved_meal
    view = kitchen.meal_recipe(meal.id)
    assert [ingredient.quantity for ingredient in view.ingredients] == [Decimal(200), None, None]
    assert view.source_recipe == recipe
    assert view.source_recipe.instructions == ["Cook 100 g rice, then season to taste."]
    assert view.ingredients[1].source_indices == [1]
    assert view.source_recipe.source_ingredients[1].text == "Salt to taste"


def test_unknowns_block_basket_even_when_measured_need_is_covered(saved_meal):
    kitchen, _, _ = saved_meal
    history = HistoryRepository(kitchen.store.engine)
    inventory = InventoryService(kitchen, history)
    inventory.upsert(InventoryItemCreate(name="Rice", quantity=200, unit="g"))
    basket = BasketService(kitchen, history).review(START, END)
    assert basket.needs == []
    assert basket.ready_for_review is False
    assert [need.name for need in basket.unquantified] == ["Salt", "Oil"]
    assert any("Salt" in issue and "Quantity" in issue for issue in basket.issues)
    trip = StoreTripService(inventory, history).review(START, END)
    assert trip.lines == []
    assert trip.checked_count == trip.total_count == 0
    assert trip.unquantified == basket.unquantified


def test_same_name_unknown_is_not_merged_into_measured_amount(test_database_url):
    kitchen = KitchenService(test_database_url)
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Salt in stages",
            servings=1,
            ingredients=[
                Ingredient(name="Salt", quantity=2, unit="g"),
                Ingredient(name="Salt", quantity=None, unit="g"),
            ],
            instructions=["Season to taste after adding the measured salt."],
        )
    )
    kitchen.plan_meal(MealCreate(date=START, recipe_id=recipe.id, servings=2))
    week = kitchen.shopping_list(START, END, today=START)
    assert week.lines[0].quantity == 4
    assert len(week.lines) == len(week.unquantified) == 1
    assert week.unquantified[0].name == "Salt"
    assert "quantity" not in week.unquantified[0].model_dump()
