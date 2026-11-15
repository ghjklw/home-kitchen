from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from home_kitchen.context import HistoryContext, history_context
from home_kitchen.history import HistoryRepository
from home_kitchen.inventory import InventoryService
from home_kitchen.models import ShoppingItemCreate
from home_kitchen.purchasing_reconciliation import (
    PurchasedLineCreate,
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
