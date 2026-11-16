"""Planned source quantities remain distinct from stock and actual purchases."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from home_kitchen.inventory import InventoryItem, InventoryService
from home_kitchen.models import Ingredient, Meal, Recipe, RegularItem, ShoppingItem
from home_kitchen.service import KitchenService
from home_kitchen.shopping import ShoppingContext, default_shopping_builder, need_id

START = date(2026, 11, 16)
END = date(2026, 11, 22)


def context():
    recipe = Recipe(
        id=uuid4(),
        title="Soup",
        servings=2,
        ingredients=[
            Ingredient(name="Carrot", quantity="0.1", unit="kg"),
            Ingredient(name="CARROT", quantity="25", unit="g"),
            Ingredient(name="Salt", quantity=None, unit="etter smak"),
        ],
        instructions=["Cook"],
    )
    meal = Meal(id=uuid4(), date=START, recipe_id=recipe.id, servings=3)
    capture = ShoppingItem(id=uuid4(), name="carrot", quantity="0.05", unit="kg")
    regular = RegularItem(id=uuid4(), name="Carrot", quantity="10", unit="g")
    return ShoppingContext(
        meals=[meal],
        recipes={recipe.id: recipe},
        captures=[capture],
        regulars=[regular],
        active_confirmations=set(),
        today=START,
        start=START,
        end=END,
    )


def test_exact_sources_are_aggregated_by_need_and_stable_source():
    ctx = context()
    result = default_shopping_builder().build(ctx)
    line = result.lines[0]
    assert (line.name, line.quantity, line.unit) == ("Carrot", Decimal("247.5"), "g")
    assert [
        (source.kind, source.source_id, source.quantity, source.unit)
        for source in line.source_quantities
    ] == [
        ("meal", ctx.meals[0].id, Decimal("187.5"), "g"),
        ("capture", ctx.captures[0].id, Decimal("50"), "g"),
        ("regular", ctx.regulars[0].id, Decimal("10"), "g"),
    ]
    assert line.quantity == sum(source.quantity for source in line.source_quantities)
    assert len(line.origins) == 3
    assert result.unquantified[0].name == "Salt"
    assert default_shopping_builder().build(ctx) == result


def test_closed_source_matches_only_its_window_need_and_source():
    ctx = context()
    meal = ctx.meals[0]
    ctx.closed_sources.add((START, END, need_id(START, END, "carrot", "g"), "meal", meal.id))
    # Wrong source kind, need, or shopping window cannot close the capture.
    capture = ctx.captures[0]
    ctx.closed_sources.update(
        {
            (START, END, need_id(START, END, "carrot", "g"), "regular", capture.id),
            (START, END, need_id(START, END, "potato", "g"), "capture", capture.id),
            (START, START, need_id(START, END, "carrot", "g"), "capture", capture.id),
        }
    )
    result = default_shopping_builder().build(ctx)
    assert result.lines[0].quantity == Decimal("60")
    assert [source.kind for source in result.lines[0].source_quantities] == ["capture", "regular"]
    assert result.lines[0].origins == [f"item:{capture.id}", f"regular:{ctx.regulars[0].id}"]
    assert result.unquantified[0].origins == [f"meal:{meal.id}"]


def test_closing_every_quantified_source_removes_only_that_need():
    ctx = context()
    line = default_shopping_builder().build(ctx).lines[0]
    ctx.closed_sources.update(
        (START, END, need_id(START, END, line.name, line.unit), source.kind, source.source_id)
        for source in line.source_quantities
    )
    result = default_shopping_builder().build(ctx)
    assert result.lines == []
    assert len(result.unquantified) == 1


def test_inventory_preserves_gross_source_amounts_and_stock_offset():
    ctx = context()
    week = default_shopping_builder().build(ctx)
    kitchen = SimpleNamespace(
        store=SimpleNamespace(engine=None),
        shopping_list=lambda *args, **kwargs: week,
    )
    inventory = InventoryService(kitchen, history=object())
    inventory.list = lambda: [InventoryItem(id=uuid4(), name="Carrot", unit="g", quantity=100)]
    line = inventory.plan(START, END, closed_sources=set()).lines[0]
    assert line.source_quantities == week.lines[0].source_quantities
    assert line.required_quantity == Decimal("247.5")
    assert line.covered_quantity == Decimal("100")
    assert line.to_buy_quantity == Decimal("147.5")
    assert sum(source.quantity for source in line.source_quantities) == line.required_quantity


def test_service_passes_closed_source_context_without_database():
    ctx = context()
    closed = {(START, END, need_id(START, END, "carrot", "g"), "meal", ctx.meals[0].id)}
    service = KitchenService.__new__(KitchenService)
    service.list_meals = lambda *args: ctx.meals
    service.list_recipes = lambda: list(ctx.recipes.values())
    service.list_items = lambda: ctx.captures
    service.list_regular_items = lambda: ctx.regulars
    service.store = SimpleNamespace(active_confirmations=lambda: set())
    service.shopping_builder = default_shopping_builder()
    result = service.shopping_list(START, END, START, closed_sources=closed)
    assert result.lines[0].quantity == Decimal("60")


def test_inventory_loads_closures_for_window_and_explicit_set_overrides():
    from unittest.mock import MagicMock

    ctx = context()
    source_key = (START, END, need_id(START, END, "carrot", "g"), "meal", ctx.meals[0].id)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value = [
        dict(
            zip(
                ("week_start", "week_end", "need_id", "source_kind", "source_id"),
                source_key,
                strict=True,
            )
        )
    ]
    kitchen = KitchenService.__new__(KitchenService)
    kitchen.list_meals = lambda *args: ctx.meals
    kitchen.list_recipes = lambda: list(ctx.recipes.values())
    kitchen.list_items = lambda: ctx.captures
    kitchen.list_regular_items = lambda: ctx.regulars
    kitchen.store = SimpleNamespace(engine=engine, active_confirmations=lambda: set())
    kitchen.shopping_builder = default_shopping_builder()
    inventory = InventoryService(kitchen, history=object())
    inventory.list = lambda: []
    assert inventory.plan(START, END).lines[0].required_quantity == Decimal("60")
    assert connection.execute.call_args.args[1] == {"start": START, "end": END}
    engine.reset_mock()
    assert inventory.plan(START, END, closed_sources=set()).lines[0].required_quantity == Decimal(
        "247.5"
    )
    engine.connect.assert_not_called()
