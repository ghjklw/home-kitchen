from datetime import date
from decimal import Decimal

from home_kitchen.inventory import InventoryItemCreate, InventoryService
from home_kitchen.models import Ingredient, MealCreate, RecipeCreate, ShoppingItemCreate
from home_kitchen.service import KitchenService
from home_kitchen.shopping import need_id
from home_kitchen.units import add_exact, canonicalize_quantity


def test_mass_and_volume_shifts_are_exact_and_unknown_units_are_preserved():
    assert canonicalize_quantity(Decimal("1.250"), " KG ") == (Decimal("1250.000"), "g")
    assert canonicalize_quantity(Decimal("2.5"), "dL") == (Decimal("250.0"), "ml")
    assert canonicalize_quantity(Decimal("3"), "cups") == (Decimal("3"), "cups")
    assert canonicalize_quantity(Decimal("2"), "item") == (Decimal("2"), "item")
    assert add_exact(Decimal("12345678901234567890123456789"), Decimal("0.1")) == Decimal(
        "12345678901234567890123456789.1"
    )


def test_shopping_aggregates_compatible_units_without_rewriting_sources(test_database_url):
    kitchen = KitchenService(test_database_url)
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Flour loaf",
            servings=2,
            ingredients=[Ingredient(name="Flour", quantity="0.5", unit="kg")],
            instructions=["Bake"],
        )
    )
    kitchen.plan_meal(MealCreate(date=date(2026, 11, 16), recipe_id=recipe.id, servings=2))
    kitchen.add_item(ShoppingItemCreate(name=" flour ", quantity="250", unit="G"))
    kitchen.add_item(ShoppingItemCreate(name="Flour", quantity="1", unit="item"))
    kitchen.add_item(ShoppingItemCreate(name="Flour", quantity="2", unit="cups"))
    kitchen.add_item(ShoppingItemCreate(name="Flour", quantity="2", unit="cups"))
    kitchen.add_item(ShoppingItemCreate(name="Milk", quantity="1", unit="L"))
    kitchen.add_item(ShoppingItemCreate(name="Milk", quantity="2", unit="dl"))

    week = kitchen.shopping_list(date(2026, 11, 16), date(2026, 11, 16), date(2026, 11, 16))
    totals = {(line.name.casefold(), line.unit): line.quantity for line in week.lines}
    assert totals[("flour", "g")] == Decimal("750")
    assert totals[("flour", "item")] == Decimal("1")
    assert totals[("flour", "cups")] == Decimal("4")
    assert totals[("milk", "ml")] == Decimal("1200")
    assert recipe.ingredients[0].quantity == Decimal("0.5")
    assert recipe.ingredients[0].unit == "kg"


def test_need_id_uses_canonical_units():
    start = date(2026, 11, 16)
    end = date(2026, 11, 22)
    assert need_id(start, end, " Flour ", "kg") == need_id(start, end, "flour", "g")
    assert need_id(start, end, "Milk", "L") == need_id(start, end, "milk", "ml")
    assert need_id(start, end, "milk", "cup") != need_id(start, end, "milk", "ml")


def test_inventory_reads_legacy_compatible_units_and_upserts_without_duplicate_counts(
    test_database_url,
):
    kitchen = KitchenService(test_database_url)
    inventory = InventoryService(kitchen)
    start = date(2026, 11, 16)
    end = date(2026, 11, 22)
    kitchen.add_item(ShoppingItemCreate(name="Flour", quantity="200", unit="g"))

    with kitchen.store.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO kitchen_inventory "
            "(id,name,normalized_name,unit,normalized_unit,quantity,category,note) "
            "VALUES "
            "('00000000-0000-0000-0000-000000000001','Flour','flour','kg','kg',1,'Baking',NULL),"
            "('00000000-0000-0000-0000-000000000002','Flour','flour','g','g',250,'Baking',NULL)"
        )

    legacy = inventory.list()
    assert len(legacy) == 1
    assert legacy[0].unit == "g"
    assert legacy[0].quantity == Decimal("1250")
    plan = inventory.plan(start, end)
    flour = plan.lines[0]
    assert flour.required_quantity == Decimal("200")
    assert flour.available_quantity == Decimal("1250")
    assert flour.to_buy_quantity == Decimal("0")
    assert flour.id == need_id(start, end, "flour", "g")

    saved = inventory.upsert(InventoryItemCreate(name="FLour", quantity="500", unit="kg"))
    assert saved.id.hex.endswith("0002")  # The existing canonical-g row keeps its identity.
    assert saved.unit == "g"
    assert saved.quantity == Decimal("500000")
    assert len(inventory.list()) == 1
    assert inventory.list()[0].quantity == Decimal("500000")
    with kitchen.store.engine.connect() as connection:
        persisted = connection.exec_driver_sql(
            "SELECT id,unit,quantity FROM kitchen_inventory ORDER BY id"
        ).all()
    assert len(persisted) == 2
    assert persisted[0][2] == Decimal(0)  # Legacy kg identity is retained as zero stock.


def test_converted_inventory_covers_partial_and_full_need_without_rounding(test_database_url):
    kitchen = KitchenService(test_database_url)
    inventory = InventoryService(kitchen)
    start = date(2026, 11, 16)
    end = date(2026, 11, 22)
    kitchen.add_item(ShoppingItemCreate(name="Flour", quantity="250", unit="g"))

    stock = inventory.upsert(InventoryItemCreate(name="Flour", quantity="0.2", unit="kg"))
    partial = inventory.plan(start, end).lines[0]
    assert partial.id == need_id(start, end, "Flour", "g")
    assert partial.available_quantity == Decimal("200")
    assert partial.covered_quantity == Decimal("200")
    assert partial.to_buy_quantity == Decimal("50")

    updated = inventory.upsert(InventoryItemCreate(name="flour", quantity="1", unit="kg"))
    full = inventory.plan(start, end).lines[0]
    assert updated.id == stock.id
    assert updated.quantity == Decimal("1000")
    assert full.available_quantity == Decimal("1000")
    assert full.covered_quantity == Decimal("250")
    assert full.to_buy_quantity == Decimal("0")


def test_removing_aggregated_legacy_stock_removes_the_visible_quantity(test_database_url):
    kitchen = KitchenService(test_database_url)
    inventory = InventoryService(kitchen)
    with kitchen.store.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO kitchen_inventory "
            "(id,name,normalized_name,unit,normalized_unit,quantity,category,note) VALUES "
            "('00000000-0000-0000-0000-000000000001','Flour','flour','kg','kg',1,'Baking',NULL),"
            "('00000000-0000-0000-0000-000000000002','Flour','flour','g','g',250,'Baking',NULL)"
        )
    displayed = inventory.list()[0]
    assert displayed.quantity == Decimal("1250")
    inventory.remove(displayed.id)
    assert inventory.list() == []
