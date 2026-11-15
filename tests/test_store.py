from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from home_kitchen.models import Ingredient, Meal, RecipeCreate, ShoppingItemCreate
from home_kitchen.service import KitchenService
from home_kitchen.store import Store


def test_migration_creates_relational_tables(test_database_url):
    store = Store(test_database_url)
    tables = set(inspect(store.engine).get_table_names())
    assert {"recipes", "meals", "items", "regulars", "confirmations", "settings"} <= tables
    assert "entities" not in tables
    assert inspect(store.engine).get_foreign_keys("meals")[0]["referred_table"] == "recipes"
    store.health()
    store.set_setting("test", "value")
    assert store.get_setting("test") == "value"
    store.set_setting("test", "changed")
    assert store.get_setting("test") == "changed"
    assert store.get_setting("missing") is None


def test_concurrent_confirmation_reuses_one_capture(test_database_url):
    service = KitchenService(test_database_url)
    regular = service.add_regular_item(ShoppingItemCreate(name="Milk"))

    def confirm(_):
        instance = KitchenService(test_database_url)
        try:
            return instance.confirm_suggestion(regular.id).id
        finally:
            instance.store.engine.dispose()

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(confirm, range(12)))
    assert len(set(results)) == 1
    assert len(service.list_items()) == 1


def test_database_enforces_recipe_reference_and_slot_upsert(test_database_url):
    service = KitchenService(test_database_url)
    recipe = service.create_recipe(
        RecipeCreate(
            title="Soup",
            image_url="https://images.example.test/soup.jpg",
            servings=2,
            ingredients=[Ingredient(name="Carrot", quantity=1)],
            instructions=["Cook"],
        )
    )
    assert str(service.get_recipe(recipe.id).image_url) == "https://images.example.test/soup.jpg"
    meal = Meal(id=uuid4(), date=date(2026, 1, 1), recipe_id=recipe.id, servings=2)
    service.store.save("meal", meal)
    replacement = meal.model_copy(update={"id": uuid4(), "servings": 4})
    service.store.save("meal", replacement)
    assert service.list_meals(meal.date, meal.date) == [replacement]
    with pytest.raises(IntegrityError):
        service.store.delete("recipe", recipe.id)
    with pytest.raises(IntegrityError):
        service.store.save("meal", meal.model_copy(update={"recipe_id": uuid4()}))


def test_sqlite_database_url_is_rejected():
    with pytest.raises(ValueError, match="postgresql"):
        Store("sqlite:///kitchen.sqlite3")
