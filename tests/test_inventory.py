from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from home_kitchen.context import HistoryContext, history_context
from home_kitchen.history import HistoryRepository
from home_kitchen.inventory import InventoryItemCreate, InventoryService
from home_kitchen.models import (
    Ingredient,
    MealCreate,
    RecipeCreate,
    ShoppingItemCreate,
    SourceIngredient,
)
from home_kitchen.service import KitchenService
from home_kitchen.shopping import need_id

START = date(2026, 11, 16)
END = date(2026, 11, 22)


@pytest.fixture
def services(test_database_url):
    kitchen = KitchenService(test_database_url)
    history = HistoryRepository(kitchen.store.engine)
    return kitchen, InventoryService(kitchen, history), history


def test_gross_owned_net_quantities_and_category_groups(services):
    kitchen, inventory, _ = services
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Yoghurt breakfast",
            servings=2,
            ingredients=[Ingredient(name="Yoghurt", quantity=8, category="Dairy")],
            instructions=["Serve"],
        )
    )
    kitchen.plan_meal(MealCreate(date=START, recipe_id=recipe.id, servings=4))
    kitchen.add_item(ShoppingItemCreate(name="Bread", quantity=2, category="Bakery"))
    inventory.upsert(InventoryItemCreate(name=" yoghurt ", quantity=4))
    plan = inventory.plan(START, END)
    yoghurt = next(line for line in plan.lines if line.name == "Yoghurt")
    assert yoghurt.required_quantity == Decimal(16)
    assert yoghurt.available_quantity == Decimal(4)
    assert yoghurt.covered_quantity == Decimal(4)
    assert yoghurt.to_buy_quantity == Decimal(12)
    assert yoghurt.id == need_id(START, END, "  YOGHURT", "ITEM")
    assert set(plan.groups) == {"Dairy", "Bakery"}
    assert yoghurt.origins[0].startswith("meal:")
    assert (
        next(
            line
            for line in kitchen.shopping_list(START, END, START).lines
            if line.name == "Yoghurt"
        ).quantity
        == 16
    )
    assert inventory.list()[0].quantity == 4
    assert inventory.plan(START, END) == plan
    with pytest.raises(ValueError, match="end must be on or after start"):
        inventory.plan(END, START)


def test_purchase_plan_preserves_approximate_source_evidence(services):
    kitchen, inventory, _ = services
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Boller",
            servings=2,
            ingredients=[Ingredient(name="sukker", quantity=1, unit="dl", source_indices=[0])],
            instructions=["Rør"],
            source_ingredients=[SourceIngredient(index=0, text="1 dl sukker")],
        )
    )
    kitchen.plan_meal(MealCreate(date=START, recipe_id=recipe.id, servings=2))
    sugar = next(line for line in inventory.plan(START, END).lines if line.name == "sukker")
    assert sugar.required_quantity == Decimal("90")
    assert sugar.approximate is True
    assert "1 dl sukker" in sugar.provenance
    assert str(sugar.source_url).startswith("https://www.matprat.no/")


def test_unit_separation_zero_net_and_absolute_quantity_upsert(services):
    kitchen, inventory, _ = services
    kitchen.add_item(ShoppingItemCreate(name="Carrot", quantity=2, unit="item"))
    kitchen.add_item(ShoppingItemCreate(name="Carrot", quantity=200, unit="g"))
    first = inventory.upsert(
        InventoryItemCreate(name="Carrot", quantity=4, unit=" ITEM ", category="Produce")
    )
    updated = inventory.upsert(
        InventoryItemCreate(name=" CARROT ", quantity=4, unit="item", category="Produce")
    )
    assert first.id == updated.id
    assert len(inventory.list()) == 1
    assert inventory.list()[0].quantity == 4
    plan = inventory.plan(START, END)
    by_unit = {line.unit: line for line in plan.lines}
    assert by_unit["item"].to_buy_quantity == 0
    assert by_unit["item"].covered_quantity == 2
    assert by_unit["item"].available_quantity == 4
    assert by_unit["item"].category == "Produce"
    assert by_unit["g"].to_buy_quantity == 200
    assert [line.unit for line in plan.purchase_lines] == ["g"]
    inventory.remove(first.id)
    assert inventory.list() == []
    with pytest.raises(KeyError):
        inventory.remove(uuid4())


def test_inventory_history_actor_and_atomic_failure(services, monkeypatch):
    _, inventory, history = services
    token = history_context.set(HistoryContext(actor_id="household-member", channel="mcp"))
    try:
        item = inventory.upsert(InventoryItemCreate(name="Rice", quantity=100, unit="g"))
        entry = history.list()[0]
        assert entry.actor_id == "household-member"
        assert entry.channel == "mcp"
        assert entry.structured["quantity"] == "100"
        assert entry.structured["action"] == "inventory_count_set"
        inventory.remove(item.id)
        assert history.list()[0].structured["action"] == "inventory_removed"
    finally:
        history_context.reset(token)

    def reject(*args, **kwargs):
        raise RuntimeError("simulated history failure")

    monkeypatch.setattr(history, "record", reject)
    with pytest.raises(RuntimeError):
        inventory.upsert(InventoryItemCreate(name="Rice", quantity=200, unit="g"))
    assert inventory.list() == []


def test_inventory_database_guards_and_category_confirmation(services):
    kitchen, inventory, _ = services
    with pytest.raises(ValidationError):
        InventoryItemCreate(name="Rice", quantity=-1)
    regular = kitchen.add_regular_item(ShoppingItemCreate(name="Milk", category="Dairy"))
    assert kitchen.confirm_suggestion(regular.id).category == "Dairy"
    assert inventory.plan(START, END).lines[0].category == "Dairy"
    inventory.upsert(InventoryItemCreate(name="Rice", quantity=0))
    with kitchen.store.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM kitchen_inventory")).scalar() == 1
