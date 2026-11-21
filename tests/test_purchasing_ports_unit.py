"""Pure unit tests for Step 4b purchasing, inventory, and basket Protocol ports."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from home_kitchen.basket.adapters import MemoryBasketSelectionStore
from home_kitchen.basket.bootstrap import create_in_memory_basket_service
from home_kitchen.basket.matching import NeedMatchResult
from home_kitchen.basket.models import ProductSnapshot, SelectedProduct
from home_kitchen.basket.repository import BasketService
from home_kitchen.bootstrap import (
    create_in_memory_store_trip_service,
    create_in_memory_trip_completion_service,
)
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.history import HistoryEntryCreate
from home_kitchen.inventory import InventoryService
from home_kitchen.models import ShoppingLine, ShoppingSourceQuantity, UnquantifiedNeed, WeekShopping
from home_kitchen.purchasing import OdaMode
from home_kitchen.purchasing_adapters import MemoryTripCompletionUnitOfWork
from home_kitchen.purchasing_reconciliation import (
    PurchasedLineCreate,
    PurchasedProductCreate,
    SourceClosureCreate,
    TripCompletionCreate,
)
from home_kitchen.shopping import ClosedSourceKey, PurchasePlan, PurchasePlanLine, need_id


class _RecordingHistory:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.entries: list[HistoryEntryCreate] = []
        self.fail_on_call = fail_on_call
        self.calls = 0

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection: Any = None,
    ) -> HistoryEntryCreate:
        self.calls += 1
        if self.fail_on_call is not None and self.calls == self.fail_on_call:
            raise RuntimeError("simulated history failure")
        self.entries.append(data)
        return data


class _StubPurchasePlanPort:
    def __init__(self, plans: dict[tuple[date, date], PurchasePlan] | None = None) -> None:
        self.plans = plans or {}

    def plan(self, start: date, end: date) -> PurchasePlan:
        return self.plans[(start, end)]


def _oda_product(
    product_id: str,
    name: str,
    *,
    pack_quantity: str = "500",
    pack_unit: str = "g",
    price: str = "34.90",
) -> ProductSnapshot:
    return ProductSnapshot(
        provider="oda",
        product_id=product_id,
        name=name,
        url=f"https://oda.com/no/products/{product_id}/",
        pack_quantity=Decimal(pack_quantity),
        pack_unit=pack_unit,
        price=Decimal(price),
        currency="NOK",
        available=True,
        observed_at=datetime.now(UTC),
        evidence_status="provider_verified",
    )


@pytest.mark.parametrize(
    ("relative_path", "forbidden_prefixes"),
    [
        (
            "src/home_kitchen/basket/repository.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.inventory",
            ),
        ),
        (
            "src/home_kitchen/basket/matching.py",
            ("home_kitchen.service", "home_kitchen.store", "home_kitchen.inventory"),
        ),
        (
            "src/home_kitchen/basket/jobs.py",
            ("home_kitchen.service", "home_kitchen.store", "home_kitchen.inventory"),
        ),
        (
            "src/home_kitchen/basket/ports.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.inventory",
            ),
        ),
        (
            "src/home_kitchen/purchasing_reconciliation.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.inventory",
            ),
        ),
        (
            "src/home_kitchen/purchasing.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.inventory",
                "home_kitchen.oda",
            ),
        ),
        (
            "src/home_kitchen/purchasing_ports.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.inventory",
                "home_kitchen.oda",
            ),
        ),
        (
            "src/home_kitchen/inventory.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/shopping.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.inventory"),
        ),
    ],
)
def test_purchasing_and_basket_modules_have_no_forbidden_top_level_imports(
    relative_path: str, forbidden_prefixes: tuple[str, ...]
) -> None:
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert not any(
            mod == prefix or mod.startswith(f"{prefix}.") for prefix in forbidden_prefixes
        ), f"{relative_path} still imports {mod}"
    if relative_path in {
        "src/home_kitchen/basket/repository.py",
        "src/home_kitchen/purchasing_reconciliation.py",
        "src/home_kitchen/purchasing.py",
        "src/home_kitchen/inventory.py",
    }:
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "home_kitchen.history":
                names = {alias.name for alias in node.names}
                assert "HistoryRepository" not in names, (
                    f"{relative_path} must not import concrete HistoryRepository at top level"
                )


def test_in_memory_basket_service_select_batch_remove_and_rollback() -> None:
    start, end = date(2026, 11, 16), date(2026, 11, 22)
    flour_id = need_id(start, end, "Flour", "g")
    milk_id = need_id(start, end, "Milk", "l")
    plan = PurchasePlan(
        start=start,
        end=end,
        lines=[
            PurchasePlanLine(
                id=flour_id,
                name="Flour",
                unit="g",
                category="Baking",
                required_quantity=Decimal("1000"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("1000"),
                origins=["meal:1"],
            ),
            PurchasePlanLine(
                id=milk_id,
                name="Milk",
                unit="l",
                category="Dairy",
                required_quantity=Decimal("2"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("2"),
                origins=["item:1"],
            ),
        ],
        suggestions=[],
        unquantified=[UnquantifiedNeed(name="Salt", unit="klype", origins=["meal:1"])],
    )
    plan_port = _StubPurchasePlanPort({(start, end): plan})
    history = _RecordingHistory()
    basket, store = create_in_memory_basket_service(plan_port, history)

    initial = basket.review(start, end)
    assert initial.ready_for_review is False
    assert any("Salt" in issue for issue in initial.issues)

    flour_sku = _oda_product("flour-1kg", "Wheat flour", pack_quantity="1", pack_unit="kg")
    selected = basket.select(
        start,
        end,
        SelectedProduct(need_id=flour_id, product=flour_sku, packages=1),
        channel="mcp",
    )
    flour_entry = next(entry for entry in selected.needs if entry.need.id == flour_id)
    assert flour_entry.allocated_quantity == Decimal("1000")
    assert flour_entry.selections[0].product.evidence_status == "unverified"
    assert history.entries[-1].channel == "mcp"
    assert history.entries[-1].structured["action"] == "basket_product_selected"

    # Reject unknown need without mutating persisted rows or recording history
    before_count = len(store.rows)
    with pytest.raises(ValueError, match="unknown need"):
        basket.select(
            start,
            end,
            SelectedProduct(need_id=uuid4(), product=flour_sku, packages=1),
        )
    assert len(store.rows) == before_count

    # Batch replace Milk while keeping Flour, and skip unchanged re-saves
    milk_sku = _oda_product(
        "milk-1l", "Whole milk", pack_quantity="1", pack_unit="l", price="22.50"
    )
    milk_match = NeedMatchResult(
        need_id=milk_id,
        name="Milk",
        quantity=Decimal("2"),
        unit="l",
        page=1,
        has_more=False,
        candidates=[],
    )
    batch_review = basket.select_batch(
        start,
        end,
        [SelectedProduct(need_id=milk_id, product=milk_sku, packages=2)],
        {milk_id: milk_match},
    )
    assert {entry.need.id: len(entry.selections) for entry in batch_review.needs} == {
        flour_id: 1,
        milk_id: 1,
    }
    history_before_retry = len(history.entries)
    basket.select_batch(
        start,
        end,
        [SelectedProduct(need_id=milk_id, product=milk_sku, packages=2)],
        {milk_id: milk_match},
    )
    assert len(history.entries) == history_before_retry

    # Rollback on history failure inside batch
    failing_history = _RecordingHistory(fail_on_call=1)
    failing_store = MemoryBasketSelectionStore(failing_history)
    failing_store.rows = dict(store.rows)
    failing_basket = BasketService(plan_port, failing_history, selection_store=failing_store)
    replacement = _oda_product(
        "milk-alt", "Oat milk", pack_quantity="1", pack_unit="l", price="29.00"
    )
    with pytest.raises(RuntimeError, match="simulated history failure"):
        failing_basket.select_batch(
            start,
            end,
            [SelectedProduct(need_id=milk_id, product=replacement, packages=2)],
            {milk_id: milk_match},
        )
    restored = failing_basket.review(start, end)
    restored_milk = next(entry for entry in restored.needs if entry.need.id == milk_id)
    assert restored_milk.selections[0].product.product_id == "milk-1l"

    # Removal deletes selection and records history
    after_remove = basket.remove(start, end, flour_id, "flour-1kg", channel="api")
    after_flour = next(entry for entry in after_remove.needs if entry.need.id == flour_id)
    assert after_flour.selections == []
    assert history.entries[-1].structured["action"] == "basket_product_removed"
    with pytest.raises(KeyError):
        basket.remove(start, end, flour_id, "flour-1kg")


def test_in_memory_trip_completion_service_covers_sku_unknown_closures_and_rollback() -> None:
    start, end = date(2026, 11, 16), date(2026, 11, 22)
    today = date.today()
    capture_milk = uuid4()
    regular_milk = uuid4()
    capture_coffee = uuid4()
    milk_id = need_id(start, end, "Milk", "ml")
    coffee_id = need_id(start, end, "Coffee", "pack")

    plan = PurchasePlan(
        start=start,
        end=end,
        lines=[
            PurchasePlanLine(
                id=milk_id,
                name="Milk",
                unit="ml",
                category="Dairy",
                required_quantity=Decimal("2000"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("2000"),
                origins=[f"item:{capture_milk}", f"regular:{regular_milk}"],
                source_quantities=[
                    ShoppingSourceQuantity(
                        kind="capture", source_id=capture_milk, quantity=Decimal("2000"), unit="ml"
                    )
                ],
            ),
            PurchasePlanLine(
                id=coffee_id,
                name="Coffee",
                unit="pack",
                category="Pantry",
                required_quantity=Decimal("1"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("1"),
                origins=[f"item:{capture_coffee}"],
                source_quantities=[
                    ShoppingSourceQuantity(
                        kind="capture", source_id=capture_coffee, quantity=Decimal("1"), unit="pack"
                    )
                ],
            ),
        ],
        suggestions=[],
    )
    plan_port = _StubPurchasePlanPort({(start, end): plan})
    history = _RecordingHistory()
    uow = MemoryTripCompletionUnitOfWork(history)
    uow.captures[capture_milk] = {
        "quantity": Decimal("2"),
        "unit": "l",
        "regular_id": regular_milk,
        "completed": False,
    }
    uow.regulars[regular_milk] = {
        "quantity": Decimal("2"),
        "unit": "l",
        "last_purchased": None,
    }
    uow.captures[capture_coffee] = {
        "quantity": Decimal("1"),
        "unit": "pack",
        "regular_id": None,
        "completed": False,
    }
    coffee_sku = _oda_product("coffee-500g", "Coffee 500 g", pack_quantity="500", pack_unit="g")
    uow.selected_products[(start, end)] = {
        coffee_id: [SelectedProduct(need_id=coffee_id, product=coffee_sku, packages=1)]
    }

    service, _ = create_in_memory_trip_completion_service(plan_port, history, uow=uow)

    token = history_context.set(HistoryContext(actor_id="malo", channel="mcp"))
    try:
        review = service.review(start, end)
        assert len(review.lines) == 2

        # Rollback check when history fails mid-complete
        uow.history = _RecordingHistory(fail_on_call=1)
        failing_req = TripCompletionCreate(
            start=start,
            end=end,
            review_token=review.review_token,
            reviewed=True,
            idempotency_key=uuid4(),
            purchased_on=today,
            purchases=[
                PurchasedLineCreate(
                    need_id=milk_id,
                    quantity=Decimal("2"),
                    unit="l",
                    close_capture_ids=[capture_milk],
                    regular_ids=[regular_milk],
                )
            ],
        )
        with pytest.raises(RuntimeError, match="simulated history failure"):
            service.complete(failing_req)
        assert uow.captures[capture_milk]["completed"] is False
        assert uow.regulars[regular_milk]["last_purchased"] is None
        assert service.list() == []

        # Restore working history and complete with SKU + source closure
        uow.history = history
        idem_key = uuid4()
        req = TripCompletionCreate(
            start=start,
            end=end,
            review_token=review.review_token,
            reviewed=True,
            idempotency_key=idem_key,
            purchased_on=today,
            purchases=[
                PurchasedLineCreate(
                    need_id=coffee_id,
                    product=PurchasedProductCreate(
                        provider="oda", product_id="coffee-500g", packages=1
                    ),
                    close_capture_ids=[capture_coffee],
                )
            ],
            source_closures=[
                SourceClosureCreate(
                    need_id=milk_id,
                    source_kind="regular",
                    source_id=regular_milk,
                    resolution="already_have",
                )
            ],
        )
        completed = service.complete(req)
        assert completed == service.complete(req)
        assert completed.lines[0].actual_quantity == Decimal("500")
        assert completed.lines[0].actual_unit == "g"
        assert uow.captures[capture_coffee]["completed"] is True
        # Closing the regular also closes its linked capture
        assert uow.captures[capture_milk]["completed"] is True
        assert service.list(limit=10) == [completed]
        assert any(e.kind == "purchase" and e.channel == "mcp" for e in history.entries)
        assert any(
            e.structured.get("action") == "shopping_source_resolved" for e in history.entries
        )

        # Idempotency conflict check
        conflicting = req.model_copy(update={"source_closures": []})
        with pytest.raises(ValueError, match="Idempotency"):
            service.complete(conflicting)
    finally:
        history_context.reset(token)


def test_in_memory_store_trip_and_oda_mode_ports() -> None:
    start, end = date(2026, 11, 16), date(2026, 11, 22)
    milk_id = need_id(start, end, "Milk", "l")
    plan = PurchasePlan(
        start=start,
        end=end,
        lines=[
            PurchasePlanLine(
                id=milk_id,
                name="Milk",
                unit="l",
                category="Dairy",
                required_quantity=Decimal("2"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("2"),
                origins=["item:1"],
            )
        ],
        suggestions=[],
    )
    history = _RecordingHistory()
    trip_service, repo = create_in_memory_store_trip_service(
        _StubPurchasePlanPort({(start, end): plan}), history
    )

    initial = trip_service.review(start, end)
    assert initial.total_count == 1
    assert initial.checked_count == 0

    checked = trip_service.check(start, end, milk_id, True)
    assert checked.checked_count == 1
    assert checked.lines[0].checked_quantity == Decimal("2")
    assert len(history.entries) == 1

    # Repeating same check state does not emit duplicate history event
    trip_service.check(start, end, milk_id, True)
    assert len(history.entries) == 1
    assert len(repo.recorded_events) == 1

    with pytest.raises(ValueError, match="no longer needs buying"):
        trip_service.check(start, end, uuid4(), True)

    class _StubOdaStatus:
        def __init__(self, connected: bool) -> None:
            self._connected = connected

        def status(self, actor_id: str) -> dict[str, Any]:
            return {"connected": self._connected, "actor_id": actor_id}

    assert OdaMode(_StubOdaStatus(True)).info().available is True
    assert OdaMode(_StubOdaStatus(False)).info().available is False


def test_inventory_service_works_with_shopping_list_port_and_sqlite_without_kitchen_service(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'inv.db'}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE kitchen_inventory ("
                "id TEXT PRIMARY KEY, "
                "name TEXT NOT NULL, "
                "normalized_name TEXT NOT NULL, "
                "unit TEXT NOT NULL, "
                "normalized_unit TEXT NOT NULL, "
                "quantity NUMERIC NOT NULL, "
                "category TEXT NOT NULL, "
                "note TEXT, "
                "UNIQUE (normalized_name, normalized_unit)"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE kitchen_trip_source_closures ("
                "week_start DATE NOT NULL, "
                "week_end DATE NOT NULL, "
                "need_id TEXT NOT NULL, "
                "source_kind TEXT NOT NULL, "
                "source_id TEXT NOT NULL"
                ")"
            )
        )

    class _StubShoppingListPort:
        def __init__(self) -> None:
            self.calls: list[tuple[date, date, set[ClosedSourceKey] | None]] = []

        def shopping_list(
            self,
            start: date,
            end: date,
            today: date,
            *,
            closed_sources: set[ClosedSourceKey] | None = None,
        ) -> WeekShopping:
            self.calls.append((start, end, closed_sources))
            return WeekShopping(
                lines=[
                    ShoppingLine(
                        name="Oats",
                        quantity=Decimal("500"),
                        unit="g",
                        category="Pantry",
                        origins=["meal:1"],
                    )
                ],
                suggestions=[],
            )

    shopping_port = _StubShoppingListPort()
    history = _RecordingHistory()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO kitchen_inventory "
                "(id, name, normalized_name, unit, normalized_unit, quantity, category) "
                "VALUES (:id, 'Oats', 'oats', 'g', 'g', 200, 'Pantry')"
            ),
            {"id": str(uuid4())},
        )
    inventory = InventoryService(shopping_port, history, engine=engine)

    start, end = date(2026, 11, 16), date(2026, 11, 22)
    plan = inventory.plan(start, end)
    assert len(plan.lines) == 1
    assert plan.lines[0].to_buy_quantity == Decimal("300")
    assert plan.lines[0].available_quantity == Decimal("200")
    assert shopping_port.calls == [(start, end, set())]
    assert inventory.list()[0].name == "Oats"

    # Legacy BasketService(SimpleNamespace(store=SimpleNamespace(engine=engine)), history) compat
    legacy_basket = BasketService(SimpleNamespace(store=SimpleNamespace(engine=engine)), history)
    assert legacy_basket.engine is engine
    engine.dispose()
