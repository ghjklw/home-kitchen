from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from home_kitchen.basket.models import ProductSnapshot, SelectedProduct
from home_kitchen.basket.repository import BasketService
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.history import HistoryRepository
from home_kitchen.inventory import InventoryService
from home_kitchen.models import Ingredient, MealCreate, MealUpdate, RecipeCreate, ShoppingItemCreate
from home_kitchen.purchasing_reconciliation import (
    PurchasedLineCreate,
    PurchasedProductCreate,
    TripCompletionCreate,
    TripCompletionService,
)
from home_kitchen.service import KitchenService


@pytest.fixture
def services(test_database_url):
    kitchen = KitchenService(test_database_url)
    history = HistoryRepository(kitchen.store.engine)
    service = TripCompletionService(InventoryService(kitchen, history), history)
    return kitchen, service, history


def request(service, purchases=None, **kwargs):
    today = date.today()
    review = service.review(today, today)
    return TripCompletionCreate(
        start=today,
        end=today,
        review_token=review.review_token,
        reviewed=True,
        idempotency_key=uuid4(),
        purchased_on=today,
        purchases=purchases
        or [
            PurchasedLineCreate(
                need_id=review.lines[0].id,
                quantity=review.lines[0].to_buy_quantity,
                unit=review.lines[0].unit,
            )
        ],
        **kwargs,
    )


def select_coffee_package(kitchen, history, quantity):
    today = date.today()
    capture = kitchen.add_item(ShoppingItemCreate(name="Coffee", quantity=quantity, unit="pack"))
    basket = BasketService(kitchen, history)
    target = basket.review(today, today).needs[0].need
    coffee = ProductSnapshot(
        provider="oda",
        product_id="coffee-500g",
        name="Coffee 500 g",
        url="https://oda.com/no/products/coffee-500g",
        pack_quantity=Decimal("500"),
        pack_unit="g",
        observed_at=datetime.now(UTC),
    )
    basket.select(
        today,
        today,
        SelectedProduct(need_id=target.id, product=coffee, packages=int(quantity)),
    )
    return capture, target


def test_explicit_completion_closes_only_selected_capture_and_records_actual(services):
    kitchen, service, history = services
    bought = kitchen.add_item(ShoppingItemCreate(name="Milk", quantity=2, unit="l"))
    left = kitchen.add_item(ShoppingItemCreate(name="Bread", quantity=1))
    data = request(service)
    milk = next(line for line in service.review(data.start, data.end).lines if line.name == "Milk")
    data.purchases = [
        PurchasedLineCreate(need_id=milk.id, quantity=2, unit="l", close_capture_ids=[bought.id])
    ]
    token = history_context.set(HistoryContext(actor_id="malo", channel="app"))
    try:
        data.review_token = service.review(data.start, data.end).review_token
        result = service.complete(data)
        assert service.complete(data) == result
    finally:
        history_context.reset(token)
    assert {item.id: item.completed for item in kitchen.list_items()} == {
        bought.id: True,
        left.id: False,
    }
    assert result.lines[0].purchase.quantity == Decimal(2)
    purchases = [entry for entry in history.list() if entry.kind == "purchase"]
    assert len(purchases) == 1
    assert purchases[0].actor_id == "malo"
    assert purchases[0].channel == "app"
    assert service.list() == [result]
    assert service.inventory.list() == []
    with kitchen.store.engine.begin() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            connection.execute(text("DELETE FROM kitchen_completed_trips"))


def test_stale_review_and_reused_key_cannot_mutate(services):
    kitchen, service, history = services
    item = kitchen.add_item(ShoppingItemCreate(name="Milk"))
    data = request(service)
    kitchen.add_item(ShoppingItemCreate(name="Milk"))
    with pytest.raises(ValueError, match="changed"):
        service.complete(data)
    assert service.list() == []
    data = request(service)
    result = service.complete(data)
    changed = data.model_copy(deep=True)
    changed.purchases[0].quantity = Decimal(9)
    with pytest.raises(ValueError, match="Idempotency"):
        service.complete(changed)
    assert service.list() == [result]
    assert not kitchen.store.get("item", item.id, type(item)).completed


def test_partial_incompatible_or_untraced_sources_roll_back_all_lines(services):
    kitchen, service, history = services
    capture = kitchen.add_item(ShoppingItemCreate(name="Milk", quantity=2, unit="l"))
    data = request(service)
    data.purchases[0].close_capture_ids = [capture.id]
    data.purchases[0].quantity = Decimal(1)
    data.purchases[0].unit = "l"
    with pytest.raises(ValueError, match="fully cover"):
        service.complete(data)
    data.purchases[0].quantity = Decimal(2)
    data.purchases[0].unit = "kg"
    with pytest.raises(ValueError, match="unit family"):
        service.complete(data)
    data.purchases[0].unit = "l"
    data.purchases[0].regular_ids = [uuid4()]
    with pytest.raises(ValueError, match="provenance"):
        service.complete(data)
    assert not kitchen.list_items()[0].completed
    assert service.list() == []
    assert not [entry for entry in history.list() if entry.kind == "purchase"]


def _product_purchase(service, need_id, packages, *, close_capture_ids=()):
    today = date.today()
    review = service.review(today, today)
    return TripCompletionCreate(
        start=today,
        end=today,
        review_token=review.review_token,
        reviewed=True,
        idempotency_key=uuid4(),
        purchased_on=today,
        purchases=[
            PurchasedLineCreate(
                need_id=need_id,
                product=PurchasedProductCreate(
                    provider="oda", product_id="coffee-500g", packages=packages
                ),
                close_capture_ids=list(close_capture_ids),
            )
        ],
    )


def test_explicit_sku_package_purchase_closes_pack_need_and_keeps_physical_amount(services):
    kitchen, service, history = services
    today = date.today()
    capture, need = select_coffee_package(kitchen, history, 1)
    result = service.complete(
        _product_purchase(service, need.id, 1, close_capture_ids=[capture.id])
    )

    line = result.lines[0]
    assert line.actual_quantity == Decimal("500")
    assert line.actual_unit == "g"
    assert line.product_snapshot.pack_quantity == Decimal("500")
    assert line.product_snapshot.pack_unit == "g"
    assert line.purchase.product.packages == 1
    assert service.inventory.list() == []
    assert kitchen.list_items()[0].completed is True
    with kitchen.store.engine.connect() as connection:
        closure = connection.execute(
            text(
                "SELECT resolution FROM kitchen_trip_source_closures "
                "WHERE week_start=:start AND week_end=:end AND need_id=:need "
                "AND source_kind='capture' AND source_id=:source"
            ),
            {"start": today, "end": today, "need": need.id, "source": capture.id},
        ).scalar_one()
    assert closure == "purchased"


def test_pack_purchase_cannot_close_more_sources_than_packages_and_no_closure_is_allowed(
    services,
):
    kitchen, service, history = services
    capture, need = select_coffee_package(kitchen, history, 2)

    insufficient = _product_purchase(service, need.id, 1, close_capture_ids=[capture.id])
    with pytest.raises(ValueError, match="fully cover"):
        service.complete(insufficient)
    assert service.list() == []
    assert kitchen.list_items()[0].completed is False

    no_closure = service.complete(_product_purchase(service, need.id, 1))
    assert no_closure.lines[0].actual_quantity == Decimal("500")
    assert no_closure.lines[0].actual_unit == "g"
    assert no_closure.lines[0].purchase.product.packages == 1
    assert no_closure.source_closures == []
    assert kitchen.list_items()[0].completed is False
    assert service.inventory.list() == []


def test_pack_need_manual_units_stay_strict_and_unknown_purchase_remains_unknown(services):
    kitchen, service, history = services
    capture, need = select_coffee_package(kitchen, history, 1)
    review = service.review(date.today(), date.today())
    data = TripCompletionCreate(
        start=date.today(),
        end=date.today(),
        review_token=review.review_token,
        reviewed=True,
        idempotency_key=uuid4(),
        purchased_on=date.today(),
        purchases=[
            PurchasedLineCreate(
                need_id=need.id,
                quantity=500,
                unit="g",
                close_capture_ids=[capture.id],
            )
        ],
    )
    with pytest.raises(ValueError, match="unit family"):
        service.complete(data)
    assert service.list() == []

    unknown = service.complete(
        TripCompletionCreate(
            start=date.today(),
            end=date.today(),
            review_token=service.review(date.today(), date.today()).review_token,
            reviewed=True,
            idempotency_key=uuid4(),
            purchased_on=date.today(),
            purchases=[PurchasedLineCreate(need_id=need.id, close_capture_ids=[capture.id])],
        )
    )
    assert unknown.lines[0].actual_quantity is None
    assert unknown.lines[0].actual_unit is None
    assert kitchen.list_items()[0].completed is True
    assert service.inventory.list() == []
    assert service.list() == [unknown]
    assert len([entry for entry in history.list() if entry.kind == "purchase"]) == 1


def test_confirmed_regular_requires_explicit_trace_and_coverage(services):
    kitchen, service, history = services
    regular = kitchen.add_regular_item(ShoppingItemCreate(name="Milk", quantity=2, unit="l"))
    capture = kitchen.confirm_suggestion(regular.id)
    data = request(service)
    data.purchases[0].close_capture_ids = [capture.id]
    data.purchases[0].regular_ids = [regular.id]
    service.complete(data)
    assert kitchen.list_regular_items()[0].last_purchased == date.today()
    assert kitchen.list_items()[0].completed
    assert not service.review(data.start, data.end).lines


def test_concurrent_retry_records_once(services):
    kitchen, service, history = services
    kitchen.add_item(ShoppingItemCreate(name="Milk"))
    data = request(service)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: service.complete(data), range(3)))
    assert len({trip.id for trip in results}) == 1
    assert len([entry for entry in history.list() if entry.kind == "purchase"]) == 1


def test_confirmation_and_actual_date_are_required():
    values = dict(
        start=date.today(),
        end=date.today(),
        review_token="token",
        reviewed=False,
        idempotency_key=uuid4(),
        purchased_on=date.today(),
        purchases=[dict(need_id=uuid4(), quantity=1, unit="item")],
    )
    with pytest.raises(ValidationError):
        TripCompletionCreate(**values)
    values.update(reviewed=True, purchased_on=date.today() + timedelta(days=1))
    with pytest.raises(ValidationError, match="future"):
        TripCompletionCreate(**values)


def test_history_failure_rolls_back_capture_and_snapshot(services, monkeypatch):
    kitchen, service, history = services
    capture = kitchen.add_item(ShoppingItemCreate(name="Milk"))
    data = request(service)
    data.purchases[0].close_capture_ids = [capture.id]

    def unavailable(*args, **kwargs):
        raise RuntimeError("History unavailable")

    monkeypatch.setattr(history, "record", unavailable)
    with pytest.raises(RuntimeError, match="History unavailable"):
        service.complete(data)
    assert not kitchen.list_items()[0].completed
    assert service.list() == []


def test_pickup_is_not_a_purchase_and_actor_change_requires_new_review(services):
    from home_kitchen.purchasing import StoreTripService

    kitchen, service, history = services
    kitchen.add_item(ShoppingItemCreate(name="Milk"))
    data = request(service)
    StoreTripService(service.inventory, history).check(
        data.start, data.end, data.purchases[0].need_id, True
    )
    assert service.list() == []
    assert not [entry for entry in history.list() if entry.kind == "purchase"]
    assert service.review(data.start, data.end).review_token == data.review_token
    token = history_context.set(HistoryContext(actor_id="other"))
    try:
        with pytest.raises(ValueError, match="changed"):
            service.complete(data)
    finally:
        history_context.reset(token)


def test_extras_and_store_trip_checkmarks_survive_meal_plan_mutations(services):
    from home_kitchen.purchasing import StoreTripService

    kitchen, service, history = services
    today = date.today()
    trip_service = StoreTripService(service.inventory, history)

    soap = kitchen.add_item(
        ShoppingItemCreate(name="Soap", quantity=1, unit="pack", category="Household")
    )
    milk = kitchen.add_item(ShoppingItemCreate(name="Milk", quantity=2, unit="l", category="Dairy"))
    soup = kitchen.create_recipe(
        RecipeCreate(
            title="Carrot soup",
            servings=2,
            ingredients=[Ingredient(name="Carrot", quantity=2, unit="kg", category="Produce")],
            instructions=["Cook"],
        )
    )
    pasta = kitchen.create_recipe(
        RecipeCreate(
            title="Tomato pasta",
            servings=2,
            ingredients=[Ingredient(name="Tomato", quantity=4, unit="item", category="Produce")],
            instructions=["Boil"],
        )
    )

    soup_meal = kitchen.plan_meal(MealCreate(date=today, recipe_id=soup.id, servings=2))
    initial_trip = trip_service.review(today, today)
    soap_line = next(line for line in initial_trip.lines if line.name == "Soap")
    checked_trip = trip_service.check(today, today, soap_line.id, True)
    assert next(line for line in checked_trip.lines if line.id == soap_line.id).checked is True

    # Add another meal, edit the first meal, then delete the added meal.
    pasta_meal = kitchen.plan_meal(MealCreate(date=today, recipe_id=pasta.id, servings=2))
    kitchen.update_meal(
        soup_meal.id,
        MealUpdate(
            date=today,
            recipe_id=soup.id,
            servings=4,
            expected_version=soup_meal.version,
        ),
    )
    kitchen.delete_meal(pasta_meal.id)

    after_trip = trip_service.review(today, today)
    after_soap = next(line for line in after_trip.lines if line.name == "Soap")
    assert after_soap.id == soap_line.id
    assert after_soap.checked is True
    assert after_soap.checked_quantity == Decimal("1")
    assert {item.id for item in kitchen.list_items()} == {soap.id, milk.id}
    assert all(not item.completed for item in kitchen.list_items())
    assert next(
        line for line in after_trip.lines if line.name == "Carrot"
    ).to_buy_quantity == Decimal("4000")
    assert all(line.name != "Tomato" for line in after_trip.lines)
