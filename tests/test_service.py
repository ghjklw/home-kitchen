from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from home_kitchen.models import Ingredient, MealCreate, RecipeCreate, ShoppingItemCreate
from home_kitchen.service import KitchenService


@pytest.fixture
def service(tmp_path):
    return KitchenService(tmp_path / "kitchen.sqlite3")


def recipe(service):
    return service.create_recipe(
        RecipeCreate(
            title=" Soup ",
            servings=2,
            ingredients=[Ingredient(name=" Carrot ", quantity="100", unit=" G ")],
            instructions=[" Chop ", "Cook"],
        )
    )


def test_scaled_meals_capture_and_incompatible_units(service):
    soup = recipe(service)
    service.plan_meal(MealCreate(date=date(2026, 1, 1), recipe_id=soup.id, servings=3))
    service.add_item(ShoppingItemCreate(name="CARROT", quantity="25", unit="g"))
    service.add_item(ShoppingItemCreate(name="carrot", quantity="2", unit="item"))
    done = service.add_item(ShoppingItemCreate(name="carrot", quantity="9", unit="g"))
    service.complete_item(done.id, True)
    result = service.shopping_list(date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 1))
    assert {line.unit: line.quantity for line in result.lines} == {
        "g": Decimal("175"),
        "item": Decimal("2"),
    }
    assert len(result.lines[0].origins) == 2
    assert soup.title == "Soup"
    assert soup.instructions == ["Chop", "Cook"]


def test_meal_boundaries_replacement_and_unknown_recipe(service):
    soup = recipe(service)
    for day in (1, 2, 3):
        service.plan_meal(MealCreate(date=date(2026, 1, day), recipe_id=soup.id, servings=2))
    replacement = service.plan_meal(
        MealCreate(date=date(2026, 1, 2), recipe_id=soup.id, servings=4)
    )
    meals = service.list_meals(date(2026, 1, 2), date(2026, 1, 3))
    assert [meal.date.day for meal in meals] == [2, 3]
    assert meals[0] == replacement
    with pytest.raises(KeyError):
        service.plan_meal(MealCreate(date=date(2026, 1, 2), recipe_id=uuid4(), servings=2))
    with pytest.raises(ValueError):
        service.list_meals(date(2026, 1, 3), date(2026, 1, 2))
    with pytest.raises(ValueError):
        service.shopping_list(date(2026, 1, 3), date(2026, 1, 2), date(2026, 1, 1))


def test_regular_due_confirmation_and_explicit_purchase(service):
    milk = service.add_regular_item(ShoppingItemCreate(name="Milk"), interval_days=7)
    today = date(2026, 1, 8)
    assert service.shopping_list(today, today, today).lines[0].name == "Milk"
    service.record_purchase(milk.id, date(2026, 1, 1))
    assert service.shopping_list(today, today, date(2026, 1, 7)).suggestions == []
    due = service.shopping_list(today, today, today)
    assert due.lines == []
    assert [item.id for item in due.suggestions] == [milk.id]
    capture = service.confirm_suggestion(milk.id)
    assert service.confirm_suggestion(milk.id).id == capture.id
    confirmed = service.shopping_list(today, today, today)
    assert len(confirmed.lines) == 1
    assert confirmed.suggestions == []
    service.complete_item(capture.id, True)
    assert service.list_regular_items()[0].last_purchased == date(2026, 1, 1)
    assert len(service.shopping_list(today, today, today).suggestions) == 1
    assert service.confirm_suggestion(milk.id).id != capture.id


def test_sqlite_persistence_and_confirmation_across_instances(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    first = KitchenService(path)
    soup = recipe(first)
    meal = first.plan_meal(MealCreate(date=date(2026, 1, 1), recipe_id=soup.id, servings=1))
    regular = first.add_regular_item(ShoppingItemCreate(name="Bread", quantity="0.5"))
    capture = first.confirm_suggestion(regular.id)
    second = KitchenService(path)
    assert second.get_recipe(soup.id) == soup
    assert second.list_meals(date(2026, 1, 1), date(2026, 1, 1)) == [meal]
    assert second.confirm_suggestion(regular.id) == capture
    assert len(second.list_items()) == 1
    result = second.shopping_list(date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 1))
    assert next(line for line in result.lines if line.name == "Bread").quantity == Decimal("0.5")


def test_recipe_edits_and_deletes_preserve_meal_references(service):
    soup = recipe(service)
    updated = service.update_recipe(
        soup.id,
        RecipeCreate(
            title="New soup", servings=1, ingredients=soup.ingredients, instructions=["Boil"]
        ),
    )
    assert updated.id == soup.id
    assert service.get_recipe(soup.id).title == "New soup"
    meal = service.plan_meal(MealCreate(date=date(2026, 1, 1), recipe_id=soup.id, servings=1))
    with pytest.raises(ValueError, match="planned meals"):
        service.delete_recipe(soup.id)
    service.delete_meal(meal.id)
    service.delete_recipe(soup.id)
    with pytest.raises(KeyError):
        service.get_recipe(soup.id)
    item = service.add_item(ShoppingItemCreate(name="Eggs"))
    service.delete_item(item.id)
    assert service.list_items() == []


@pytest.mark.parametrize(
    "changes", [{"name": " "}, {"quantity": 0}, {"quantity": "NaN"}, {"unit": " "}]
)
def test_invalid_ingredients(changes):
    with pytest.raises(ValidationError):
        Ingredient.model_validate({"name": "salt", "quantity": 1, **changes})
