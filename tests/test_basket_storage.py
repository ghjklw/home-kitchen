from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from home_kitchen.basket.matching import NeedMatchResult
from home_kitchen.basket.models import ProductSnapshot, SelectedProduct
from home_kitchen.basket.repository import BasketService
from home_kitchen.history import HistoryRepository
from home_kitchen.models import (
    Ingredient,
    MealCreate,
    RecipeCreate,
    ShoppingItemCreate,
    SourceIngredient,
)
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


def test_whitespace_variations_merge_into_one_stable_basket_need(test_database_url):
    kitchen = KitchenService(test_database_url)
    basket = BasketService(kitchen, HistoryRepository(kitchen.store.engine))
    start, end = date(2026, 5, 4), date(2026, 5, 10)
    kitchen.add_item(ShoppingItemCreate(name="olive oil", quantity=1, unit="bottle"))
    original = basket.review(start, end).needs[0].need
    kitchen.add_item(ShoppingItemCreate(name="Olive  oil", quantity=2, unit="bottle"))
    review = basket.review(start, end)
    assert len(review.needs) == 1
    assert review.needs[0].need.id == original.id
    assert review.needs[0].need.quantity == Decimal(3)
    assert len(review.needs[0].need.origins) == 2
    kitchen.store.engine.dispose()


def test_basket_need_preserves_approximate_source_evidence(test_database_url):
    kitchen = KitchenService(test_database_url)
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Boller",
            servings=2,
            ingredients=[Ingredient(name="sukker", quantity=1, unit="dl", source_indices=[0])],
            instructions=["Rør"],
            source_ingredients=[SourceIngredient(index=0, text="1 dl sukker")],
        )
    )
    kitchen.plan_meal(MealCreate(date=date(2026, 5, 4), recipe_id=recipe.id, servings=2))
    basket = BasketService(kitchen, HistoryRepository(kitchen.store.engine))
    need = basket.review(date(2026, 5, 4), date(2026, 5, 10)).needs[0].need
    assert need.approximate is True
    assert "1 dl sukker" in need.provenance
    assert str(need.source_url).startswith("https://www.matprat.no/")
    kitchen.store.engine.dispose()


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


def test_batch_rechecks_need_snapshot_and_idempotently_saves_reviewed_candidates(test_database_url):
    kitchen = KitchenService(test_database_url)
    kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=16, unit="item"))
    basket = BasketService(kitchen, HistoryRepository(kitchen.store.engine))
    start, end = date(2026, 5, 4), date(2026, 5, 10)
    need = basket.review(start, end).needs[0].need
    snapshot = NeedMatchResult(
        need_id=need.id,
        name=need.name,
        quantity=need.quantity,
        unit=need.unit,
        page=1,
        has_more=False,
        candidates=[],
    )
    picked = ProductSnapshot(
        provider="oda",
        product_id="yoghurt-1",
        name="Yoghurt",
        pack_quantity=Decimal(1),
        pack_unit="item",
        observed_at=datetime.now(UTC),
    )
    selection = SelectedProduct(need_id=need.id, product=picked, packages=16)
    changed_snapshot = snapshot.model_copy(update={"quantity": Decimal(17)})
    with pytest.raises(ValueError, match="Shopping needs changed"):
        basket.select_batch(start, end, [selection], {need.id: changed_snapshot})
    assert basket.review(start, end).needs[0].selections == []

    first = basket.select_batch(start, end, [selection], {need.id: snapshot})
    assert len(first.needs[0].selections) == 1
    with kitchen.store.engine.connect() as connection:
        before = connection.execute(
            text(
                "SELECT count(*) FROM kitchen_history WHERE structured->>'action' = 'basket_product_selected'"
            )
        ).scalar_one()
    repeated = basket.select_batch(start, end, [selection], {need.id: snapshot})
    with kitchen.store.engine.connect() as connection:
        after = connection.execute(
            text(
                "SELECT count(*) FROM kitchen_history WHERE structured->>'action' = 'basket_product_selected'"
            )
        ).scalar_one()
    assert repeated.needs[0].selections == first.needs[0].selections
    assert after == before == 1
    kitchen.store.engine.dispose()


def test_batch_failure_rolls_back_all_rows_and_preserves_existing_explicit_choice(
    test_database_url,
):
    kitchen = KitchenService(test_database_url)
    kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=2, unit="item"))
    kitchen.add_item(ShoppingItemCreate(name="Milk", quantity=1, unit="item"))
    history = HistoryRepository(kitchen.store.engine)
    basket = BasketService(kitchen, history)
    start, end = date(2026, 5, 4), date(2026, 5, 10)
    before = basket.review(start, end)
    old_need, second_need = [item.need for item in before.needs]
    old = ProductSnapshot(
        provider="oda",
        product_id="old-yoghurt",
        name="Old choice",
        pack_quantity=1,
        pack_unit="item",
        observed_at=datetime.now(UTC),
    )
    basket.select(start, end, SelectedProduct(need_id=old_need.id, product=old, packages=2))

    def match(need):
        return NeedMatchResult(
            need_id=need.id,
            name=need.name,
            quantity=need.quantity,
            unit=need.unit,
            page=1,
            has_more=False,
            candidates=[],
        )

    replacement = ProductSnapshot(
        provider="oda",
        product_id="new-yoghurt",
        name="New choice",
        pack_quantity=1,
        pack_unit="item",
        observed_at=datetime.now(UTC),
    )
    milk = ProductSnapshot(
        provider="oda",
        product_id="milk",
        name="Milk",
        pack_quantity=1,
        pack_unit="item",
        observed_at=datetime.now(UTC),
    )
    selections = [
        SelectedProduct(need_id=old_need.id, product=replacement, packages=2),
        SelectedProduct(need_id=second_need.id, product=milk, packages=1),
    ]
    evidence = {old_need.id: match(old_need), second_need.id: match(second_need)}
    original_record = history.record
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated batch write failure")
        return original_record(*args, **kwargs)

    history.record = fail_second
    with pytest.raises(RuntimeError, match="simulated batch write failure"):
        basket.select_batch(start, end, selections, evidence)
    history.record = original_record
    review = basket.review(start, end)
    assert [entry.product.product_id for entry in review.needs[0].selections] == ["old-yoghurt"]
    assert review.needs[1].selections == []
    kitchen.store.engine.dispose()


def test_batch_replaces_only_submitted_needs_and_preserves_unsubmitted_choices(test_database_url):
    kitchen = KitchenService(test_database_url)
    kitchen.add_item(ShoppingItemCreate(name="Yoghurt", quantity=2, unit="item"))
    kitchen.add_item(ShoppingItemCreate(name="Milk", quantity=1, unit="item"))
    basket = BasketService(kitchen, HistoryRepository(kitchen.store.engine))
    start, end = date(2026, 5, 4), date(2026, 5, 10)
    needs = [item.need for item in basket.review(start, end).needs]

    def product(product_id):
        return ProductSnapshot(
            provider="oda",
            product_id=product_id,
            name=product_id,
            pack_quantity=1,
            pack_unit="item",
            observed_at=datetime.now(UTC),
        )

    for need in needs:
        basket.select(
            start,
            end,
            SelectedProduct(need_id=need.id, product=product(f"old-{need.name}"), packages=1),
        )
    submitted = needs[0]
    evidence = NeedMatchResult(
        need_id=submitted.id,
        name=submitted.name,
        quantity=submitted.quantity,
        unit=submitted.unit,
        page=1,
        has_more=False,
        candidates=[],
    )
    basket.select_batch(
        start,
        end,
        [SelectedProduct(need_id=submitted.id, product=product("new-choice"), packages=1)],
        {submitted.id: evidence},
    )
    review = basket.review(start, end)
    by_id = {item.need.id: item for item in review.needs}
    assert [item.product.product_id for item in by_id[submitted.id].selections] == ["new-choice"]
    untouched = next(need for need in needs if need.id != submitted.id)
    assert [item.product.product_id for item in by_id[untouched.id].selections] == [
        f"old-{untouched.name}"
    ]
    kitchen.store.engine.dispose()
