from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from home_kitchen.basket.models import ProductSnapshot, SelectedProduct
from home_kitchen.basket.repository import BasketService
from home_kitchen.history import HistoryRepository
from home_kitchen.models import ShoppingItemCreate
from home_kitchen.service import KitchenService


def product(product_id: str, name: str, image_url: str, price: str) -> ProductSnapshot:
    return ProductSnapshot(
        product_id=product_id,
        name=name,
        brand=name.split()[0],
        url=f"https://oda.com/no/products/{product_id}/",
        image_url=image_url,
        pack_quantity=8,
        pack_unit="cup",
        price=Decimal(price),
        currency="NOK",
        available=True,
        observed_at=datetime.now(UTC),
        # Public selection must downgrade any claimed provider verification.
        evidence_status="provider_verified",
    )


def test_selection_persists_across_service_restart_and_recomputes_changed_need(test_database_url):
    kitchen = KitchenService(test_database_url)
    kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=16, unit="cup"))
    history = HistoryRepository(kitchen.store.engine)
    basket = BasketService(kitchen, history)
    start, end = date(2026, 5, 4), date(2026, 5, 10)

    initial = basket.review(start, end)
    assert len(initial.needs) == 1
    need = initial.needs[0].need
    tine = product("tine-8", "Tine yoghurt", "https://images.oda.com/tine-8.jpg", "34.90")
    yoplait = product(
        "yoplait-8", "Yoplait yoghurt", "https://images.oda.com/yoplait-8.jpg", "31.50"
    )
    first = basket.select(start, end, SelectedProduct(need_id=need.id, product=tine, packages=1))
    saved = basket.select(
        start,
        end,
        SelectedProduct(
            need_id=need.id,
            product=yoplait,
            packages=1,
            alternatives=[tine],
        ),
    )
    assert first.needs[0].remaining_quantity == Decimal(8)
    assert saved.needs[0].allocated_quantity == Decimal(16)
    assert saved.needs[0].remaining_quantity == 0
    assert saved.price_complete is True
    assert saved.total_cost == Decimal("66.40")
    assert saved.ready_for_review is False
    assert any("unverified" in issue for issue in saved.issues)
    assert all(entry.product.evidence_status == "unverified" for entry in saved.needs[0].selections)
    assert all(
        alternative.evidence_status == "unverified"
        for entry in saved.needs[0].selections
        for alternative in entry.alternatives
    )

    restarted_kitchen = KitchenService(test_database_url)
    restarted = BasketService(restarted_kitchen, HistoryRepository(restarted_kitchen.store.engine))
    review = restarted.review(start, end)
    assert review.needs[0].need.id == need.id
    assert review.needs[0].allocated_quantity == Decimal(16)
    assert {entry.product.product_id for entry in review.needs[0].selections} == {
        "tine-8",
        "yoplait-8",
    }
    assert {str(entry.product.image_url) for entry in review.needs[0].selections} == {
        "https://images.oda.com/tine-8.jpg",
        "https://images.oda.com/yoplait-8.jpg",
    }

    restarted_kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=4, unit="cup"))
    changed = restarted.review(start, end)
    assert changed.needs[0].need.id == need.id
    assert changed.needs[0].need.quantity == Decimal(20)
    assert changed.needs[0].remaining_quantity == Decimal(4)

    with restarted_kitchen.store.engine.connect() as connection:
        events = (
            connection.execute(
                text(
                    "SELECT structured FROM kitchen_history "
                    "WHERE structured->>'action' = 'basket_product_selected'"
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 2
    assert {event["product_id"] for event in events} == {"tine-8", "yoplait-8"}
    assert all(event["allocated_quantity"] == "8" for event in events)
    kitchen.store.engine.dispose()
    restarted_kitchen.store.engine.dispose()


def test_unknown_need_and_incompatible_unit_leave_storage_unchanged(test_database_url):
    kitchen = KitchenService(test_database_url)
    kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=16, unit="cup"))
    basket = BasketService(kitchen, HistoryRepository(kitchen.store.engine))
    start, end = date(2026, 5, 4), date(2026, 5, 10)
    need = basket.review(start, end).needs[0].need
    candidate = product(
        "candidate", "Candidate yoghurt", "https://images.oda.com/candidate.jpg", "5"
    )

    with pytest.raises(ValueError, match="unknown need"):
        basket.select(
            start,
            end,
            SelectedProduct(need_id=UUID(int=1), product=candidate, packages=1),
        )
    incompatible = candidate.model_copy(update={"pack_unit": "kg"})
    with pytest.raises(ValueError, match="incompatible units"):
        basket.select(
            start, end, SelectedProduct(need_id=need.id, product=incompatible, packages=1)
        )

    with kitchen.store.engine.connect() as connection:
        assert (
            connection.execute(text("SELECT count(*) FROM kitchen_basket_selections")).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM kitchen_history "
                    "WHERE structured->>'action' IN ('basket_product_selected', 'basket_product_removed')"
                )
            ).scalar_one()
            == 0
        )
    kitchen.store.engine.dispose()


def test_removal_is_persisted_and_logged(test_database_url):
    kitchen = KitchenService(test_database_url)
    kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=8, unit="cup"))
    basket = BasketService(kitchen, HistoryRepository(kitchen.store.engine))
    start, end = date(2026, 5, 4), date(2026, 5, 10)
    need = basket.review(start, end).needs[0].need
    picked = product("tine-8", "Tine yoghurt", "https://images.oda.com/tine-8.jpg", "34.90")
    basket.select(start, end, SelectedProduct(need_id=need.id, product=picked, packages=1))

    removed = basket.remove(start, end, need.id, "tine-8")
    assert removed.needs[0].selections == []
    assert removed.needs[0].remaining_quantity == Decimal(8)
    with kitchen.store.engine.connect() as connection:
        action_names = (
            connection.execute(
                text(
                    "SELECT structured->>'action' FROM kitchen_history "
                    "WHERE structured->>'action' IS NOT NULL ORDER BY occurred_at"
                )
            )
            .scalars()
            .all()
        )
    assert action_names == ["basket_product_selected", "basket_product_removed"]
    kitchen.store.engine.dispose()
