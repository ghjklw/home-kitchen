"""Unit tests for Past Purchases read model, filtering, category resolution, and API endpoint."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_kitchen.catalogue.identity import CanonicalProduct
from home_kitchen.catalogue.overview_repository import MemoryProductOverviewRepository
from home_kitchen.feature_api import feature_router
from home_kitchen.history import HistoryEntry
from home_kitchen.purchase_history_view import (
    PurchaseHistoryBrowseService,
    filter_orders_by_date,
    resolve_purchase_category,
)
from home_kitchen.purchase_sync import (
    PurchaseHistorySyncService,
    extract_normalized_lines,
)
from home_kitchen.purchase_sync_adapters import MemoryPurchaseOrderStore


def _build_fixture_sync_and_history():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    store = MemoryPurchaseOrderStore()
    # Populate raw source revisions directly so resolve_effective_orders resolves them
    milk_pid = str(uuid4())
    carrot_pid = str(uuid4())

    store._sources = {
        ("oda", "default", "oda-1", "r1"): {
            "id": uuid4(),
            "provider": "oda",
            "account_id": "default",
            "external_id": "oda-1",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "order": {
                    "orderNumber": "oda-1",
                    "deliveryDate": "2026-11-01",
                    "storeName": "Oda",
                    "totalAmount": "149.80",
                    "products": [
                        {
                            "product": {
                                "id": 101,
                                "name": "Tine Helmelk 1L",
                                "unitName": "l",
                                "category": "Dairy",
                            },
                            "quantity": 2,
                            "totalGrossAmount": "49.80",
                            "canonical_product_id": milk_pid,
                        },
                        {
                            "product": {
                                "id": 102,
                                "name": "Norske Gulrøtter 1kg",
                                "unitName": "kg",
                            },
                            "quantity": 1,
                            "totalGrossAmount": "29.90",
                            "canonical_product_id": carrot_pid,
                        },
                    ],
                },
                "tracking": {"orderNumber": "oda-1", "status": "delivered"},
            },
        },
        ("trumf", "default", "trumf-1", "r1"): {
            "id": uuid4(),
            "provider": "trumf",
            "account_id": "default",
            "external_id": "trumf-1",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "batchId": "trumf-1",
                "storeName": "MENY Majorstuen",
                "transaksjonsTidspunkt": "2026-11-15T15:30:00Z",
                "belop": 124.50,
                "hasReceipt": True,
                "varelinjer": [
                    {
                        "produktBeskrivelse": "Tine Helmelk 1L",
                        "antall": 1,
                        "belop": 24.90,
                        "ean": "7038010001000",
                        "canonical_product_id": milk_pid,
                        "kassal": {"name": "Tine Helmelk 1L", "category": "Dairy"},
                    },
                    {
                        "produktBeskrivelse": "Kaffe Evergood 250g",
                        "antall": 1,
                        "belop": 99.60,
                    },
                ],
            },
        },
        # Cancelled order should never appear as an active purchase
        ("oda", "default", "oda-cancelled", "r1"): {
            "id": uuid4(),
            "provider": "oda",
            "account_id": "default",
            "external_id": "oda-cancelled",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "order": {
                    "orderNumber": "oda-cancelled",
                    "deliveryDate": "2026-11-18",
                    "storeName": "Oda",
                    "products": [{"product": {"id": 999, "name": "Kansellert Ost"}, "quantity": 1}],
                },
                "tracking": {"orderNumber": "oda-cancelled", "status": "cancelled"},
            },
        },
        # Non-receipt stub should not create baskets, only contribute to window stub counts
        ("trumf", "default", "trumf-stub", "r1"): {
            "id": uuid4(),
            "provider": "trumf",
            "account_id": "default",
            "external_id": "trumf-stub",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "batchId": "trumf-stub",
                "storeName": "KIWI",
                "transaksjonsTidspunkt": "2026-11-10T10:00:00Z",
                "hasReceipt": False,
                "varelinjer": [{"produktBeskrivelse": "Ikke kvittering", "antall": 1, "belop": 10}],
            },
        },
    }

    sync_svc = PurchaseHistorySyncService(order_store=store)

    trip_id = str(uuid4())
    history_stub = SimpleNamespace(
        list_purchase_entries=lambda limit=1000: [
            HistoryEntry(
                id=uuid4(),
                occurred_at=datetime(2026, 11, 19, 17, 0, tzinfo=UTC),
                channel="app",
                kind="purchase",
                message="Purchased Surdeigsbrød",
                structured={
                    "action": "in_store_purchase",
                    "trip_id": trip_id,
                    "purchased_on": "2026-11-19",
                    "need": {"name": "Surdeigsbrød", "unit": "stk", "category": "Bakery"},
                    "actual_quantity": "1",
                    "actual_unit": "stk",
                },
            )
        ]
    )

    overview_repo = MemoryProductOverviewRepository(
        products=[
            CanonicalProduct(id=UUID_FROM(milk_pid), canonical_name="whole milk"),
            CanonicalProduct(id=UUID_FROM(carrot_pid), canonical_name="carrot"),
        ]
    )
    overview_repo.add_alias(UUID_FROM(milk_pid), "Helmelk", locale="nb", context="recipe")
    overview_repo.add_alias(UUID_FROM(carrot_pid), "Gulrot", locale="nb", context="recipe")

    return sync_svc, history_stub, overview_repo


def UUID_FROM(val: str):
    from uuid import UUID

    return UUID(val)


def test_extract_normalized_lines_preserves_category_metadata():
    lines = extract_normalized_lines(
        "trumf",
        {
            "varelinjer": [
                {
                    "produktBeskrivelse": "Tine Melk",
                    "antall": 1,
                    "belop": 22.5,
                    "kassal": {"name": "Tine Melk", "category": "Dairy"},
                }
            ]
        },
    )
    assert len(lines) == 1
    assert lines[0].category == "Dairy"


def test_resolve_purchase_category_uses_explicit_categories_without_substring_guessing():
    assert resolve_purchase_category(raw_category="Dairy") == ("Dairy", "Meieri")
    assert resolve_purchase_category(product_name="Ferske jordbær 400g") == ("Other", "Annet")
    assert resolve_purchase_category(product_name="Ukjent spesialvare") == ("Other", "Annet")


def test_browse_service_builds_baskets_products_and_rows_with_recency_frequency():
    sync_svc, history_stub, overview_repo = _build_fixture_sync_and_history()
    svc = PurchaseHistoryBrowseService(
        purchase_sync=sync_svc,
        history=history_stub,
        product_overview_repo=overview_repo,
    )

    res = svc.browse(as_of=datetime(2026, 11, 20, 12, 0, tzinfo=UTC))

    # 3 active baskets (oda-1, trumf-1, manual trip) — cancelled & stub excluded
    assert res.total_baskets == 3
    assert {b.provider for b in res.baskets} == {"oda", "trumf", "store"}
    assert all("Kansellert" not in line.name for b in res.baskets for line in b.lines)

    # Trumf window reports non_receipt_stub_count == 1 and retained_months == 12
    assert "trumf" in res.provider_windows
    assert res.provider_windows["trumf"].non_receipt_stub_count == 1
    assert res.provider_windows["trumf"].retained_months == 12

    # Product aggregation groups Helmelk across Oda and Trumf via canonical_product_id
    helmelk = next(p for p in res.products if p.product_name == "Helmelk")
    assert helmelk.category == "Dairy"
    assert helmelk.category_label == "Meieri"
    assert helmelk.purchase_count == 2
    assert set(helmelk.providers) == {"oda", "trumf"}
    assert set(helmelk.stores) == {"Oda", "MENY Majorstuen"}
    assert helmelk.total_quantity == Decimal("3")
    assert helmelk.total_spent == Decimal("74.70")
    assert helmelk.frequency.status == "known"
    assert helmelk.frequency.average_interval_days == 14.0
    assert helmelk.frequency.is_lower_bound is True

    # Flat rows include all 5 active lines
    assert res.total_rows == 5
    assert any(r.product_name == "Surdeigsbrød" and r.category_label == "Bakeri" for r in res.rows)


def test_browse_service_filters_by_query_provider_store_category_and_date():
    sync_svc, history_stub, overview_repo = _build_fixture_sync_and_history()
    svc = PurchaseHistoryBrowseService(
        purchase_sync=sync_svc,
        history=history_stub,
        product_overview_repo=overview_repo,
    )

    by_provider = svc.browse(provider="trumf")
    assert by_provider.total_baskets == 1
    assert by_provider.baskets[0].store_name == "MENY Majorstuen"
    # Facets remain populated so UI dropdowns don't lose other options
    assert {p.key for p in by_provider.providers} == {"oda", "store", "trumf"}

    by_query = svc.browse(q="kaffe")
    assert by_query.total_rows == 1
    assert by_query.rows[0].product_name == "Kaffe Evergood 250g"
    assert by_query.baskets[0].item_count == 1

    by_category = svc.browse(category="Bakery")
    assert by_category.total_rows == 1
    assert by_category.rows[0].product_name == "Surdeigsbrød"

    by_date = svc.browse(date_from=date(2026, 11, 15), date_to=date(2026, 11, 19))
    assert by_date.total_baskets == 2

    with pytest.raises(ValueError, match="date_to"):
        svc.browse(date_from=date(2026, 11, 20), date_to=date(2026, 11, 10))


def test_coop_without_transaction_time_does_not_use_observed_at_as_purchase_date():
    observed_at = datetime.now(UTC).replace(microsecond=0)
    line = SimpleNamespace(
        name="Synthetic milk",
        raw_name="Synthetic milk",
        canonical_product_id=None,
        quantity=Decimal("1"),
        unit="stk",
        amount=Decimal("10.00"),
        ean=None,
        oda_product_id=None,
        category=None,
        is_return=False,
    )
    coop_order = SimpleNamespace(
        provider="coop",
        external_id="coop-without-transaction-date",
        transaction_at=None,
        observed_at=observed_at,
        is_cancellation=False,
        is_return=False,
        is_active_purchase=True,
        provenance="provider_verified",
        lifecycle="purchase",
        store_name="Coop",
        lines=[line],
        total_amount=Decimal("10.00"),
        is_correction=False,
        revision_count=1,
        evidence_ref=None,
    )
    oda_order = SimpleNamespace(
        **{
            **vars(coop_order),
            "provider": "oda",
            "external_id": "oda-without-transaction-date",
            "store_name": "Oda",
        }
    )

    class PurchaseSync:
        def get_effective_orders(self, *, include_inactive=True):
            return [coop_order, oda_order]

    service = PurchaseHistoryBrowseService(purchase_sync=PurchaseSync())
    unfiltered = service.browse(as_of=observed_at)

    baskets_by_provider = {basket.provider: basket for basket in unfiltered.baskets}
    assert baskets_by_provider["coop"].purchased_at is None
    assert baskets_by_provider["oda"].purchased_at == observed_at
    assert unfiltered.provider_windows.get("coop") is None

    year_range = service.browse(
        date_from=date(observed_at.year, 1, 1),
        date_to=date(observed_at.year, 12, 31),
        as_of=observed_at,
    )
    assert {basket.provider for basket in year_range.baskets} == {"oda"}
    date_filtered_orders = filter_orders_by_date(
        [coop_order, oda_order],
        date_from=date(observed_at.year, 1, 1),
        date_to=date(observed_at.year, 12, 31),
    )
    assert [order.external_id for order in date_filtered_orders] == ["oda-without-transaction-date"]


def test_rest_api_purchase_history_endpoint():
    sync_svc, history_stub, overview_repo = _build_fixture_sync_and_history()

    class DummyFeatures:
        def __init__(self):
            self.purchase_sync = sync_svc
            self.history = history_stub
            self.product_overview_repo = overview_repo

    app = FastAPI()
    app.include_router(feature_router(DummyFeatures(), None))

    with TestClient(app) as client:
        res = client.get("/api/purchase-history")
        assert res.status_code == 200
        payload = res.json()
        assert payload["total_baskets"] == 3
        assert payload["total_rows"] == 5
        assert len(payload["products"]) == 4

        filtered = client.get("/api/purchase-history?provider=oda&category=Produce")
        assert filtered.status_code == 200
        # Unknown source categories stay unclassified until structured AI review.
        assert filtered.json()["total_rows"] == 0
        carrot = next(row for row in payload["rows"] if row["product_name"] == "Gulrot")
        assert carrot["category"] == "Other"

        invalid = client.get("/api/purchase-history?date_from=2026-11-20&date_to=2026-11-01")
        assert invalid.status_code == 422
