from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
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


@pytest.mark.integration
def test_flexible_meal_migration_preserves_rows_and_translates_labels(test_database_url):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = test_database_url
    command.downgrade(config, "0016_recipe_image_attribution")
    recipe_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    engine = create_engine(test_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO recipes(id, title, servings, ingredients, instructions, source)
                    VALUES (:id, 'Suppe', 2, CAST('[]' AS jsonb), CAST('[]' AS jsonb), 'manual')
                """),
                {"id": recipe_id},
            )
            connection.execute(
                text("""
                    INSERT INTO meals(id, date, slot, recipe_id, servings)
                    VALUES (:id, DATE '2027-02-03', :slot, :recipe_id, :servings)
                """),
                [
                    {"id": first_id, "slot": "breakfast", "recipe_id": recipe_id, "servings": 2},
                    {"id": second_id, "slot": "dinner", "recipe_id": recipe_id, "servings": 4},
                ],
            )
        command.upgrade(config, "head")
        with engine.begin() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id, label, scheduled_time, recipe_id, servings FROM meals ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
            assert {row["id"] for row in rows} == {first_id, second_id}
            assert {row["label"] for row in rows} == {"Frokost", "Middag"}
            assert all(row["scheduled_time"] is None for row in rows)
            assert all(row["recipe_id"] == recipe_id for row in rows)
            assert {row["servings"] for row in rows} == {2, 4}

        command.downgrade(config, "0016_recipe_image_attribution")
        with engine.begin() as connection:
            rows = (
                connection.execute(
                    text("SELECT id, slot, recipe_id, servings FROM meals ORDER BY id")
                )
                .mappings()
                .all()
            )
            assert {row["id"] for row in rows} == {first_id, second_id}
            assert {row["slot"] for row in rows} == {"breakfast", "dinner"}
            assert all(row["recipe_id"] == recipe_id for row in rows)
            assert {row["servings"] for row in rows} == {2, 4}

        command.upgrade(config, "head")
        custom_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO meals(id, date, label, scheduled_time, recipe_id, servings)
                    VALUES (:id, DATE '2027-02-04', 'Kveldsmat', TIME '20:00', :recipe_id, 1)
                """),
                {"id": custom_id, "recipe_id": recipe_id},
            )
            assert connection.execute(text("SELECT count(*) FROM meals")).scalar_one() == 3
        with pytest.raises(RuntimeError, match="custom label/time"):
            command.downgrade(config, "0016_recipe_image_attribution")
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM meals WHERE id = :id"), {"id": custom_id})
            connection.execute(
                text("""
                    INSERT INTO meals(id, date, label, recipe_id, servings)
                    VALUES (:id, DATE '2027-02-03', 'Frokost', :recipe_id, 1)
                """),
                {"id": uuid4(), "recipe_id": recipe_id},
            )
        with pytest.raises(RuntimeError, match="duplicate legacy date/slot"):
            command.downgrade(config, "0016_recipe_image_attribution")
    finally:
        engine.dispose()


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


def test_database_enforces_recipe_reference_and_allows_multiple_date_entries(test_database_url):
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
    duplicate = meal.model_copy(update={"id": uuid4(), "servings": 4})
    service.store.save("meal", duplicate)
    assert service.list_meals(meal.date, meal.date) == [meal, duplicate]
    with pytest.raises(IntegrityError):
        service.store.delete("recipe", recipe.id)
    with pytest.raises(IntegrityError):
        service.store.save(
            "meal",
            meal.model_copy(update={"id": uuid4(), "recipe_id": uuid4(), "date": date(2026, 1, 2)}),
        )


def test_sqlite_database_url_is_rejected():
    with pytest.raises(ValueError, match="postgresql"):
        Store("sqlite:///kitchen.sqlite3")


def test_concurrent_meal_creation_allows_date_duplicates(test_database_url):
    service = KitchenService(test_database_url)
    recipe = service.create_recipe(
        RecipeCreate(
            title="Soup",
            servings=2,
            ingredients=[Ingredient(name="Carrot", quantity=1)],
            instructions=["Cook"],
        )
    )

    def plan(servings):
        instance = KitchenService(test_database_url)
        try:
            meal = Meal(id=uuid4(), date=date(2027, 1, 11), recipe_id=recipe.id, servings=servings)
            instance.store.save("meal", meal)
            return meal
        finally:
            instance.store.engine.dispose()

    with ThreadPoolExecutor(max_workers=4) as workers:
        winners = [meal for meal in workers.map(plan, range(1, 9)) if meal is not None]
    assert len(winners) == 8
    assert len(service.list_meals(date(2027, 1, 11), date(2027, 1, 11))) == 8
