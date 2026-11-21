"""Offline unit tests for Product Overview, translations, stock, frequency, and Oda preferences."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from fixtures.synthetic_kitchen import (
    SYNTHETIC_BARLEY_PRODUCT_ID,
    SYNTHETIC_CARROT_PRODUCT_ID,
    SYNTHETIC_CHICKPEAS_PRODUCT_ID,
    SYNTHETIC_COFFEE_PRODUCT_ID,
    SYNTHETIC_TOMATO_FRESH_PRODUCT_ID,
    SYNTHETIC_TOMATO_TINNED_PRODUCT_ID,
    StaticIdentityRepository,
)
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from home_kitchen.basket.matching import BasketCandidate
from home_kitchen.catalogue.identity import CanonicalProduct
from home_kitchen.catalogue.overview_models import (
    ExcludedOdaSkuInput,
    PreferredOdaSkuInput,
    ProductPreferencesUpdate,
    ProductStockUpdate,
    PurchaseEvidenceRecord,
)
from home_kitchen.catalogue.overview_repository import (
    MemoryProductOverviewRepository,
    PostgresProductOverviewRepository,
)
from home_kitchen.catalogue.overview_service import (
    ProductOverviewService,
    compute_purchase_frequency,
)
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools

sqlite3.register_adapter(UUID, str)


@pytest.fixture
def memory_repo() -> MemoryProductOverviewRepository:
    static_id_repo = StaticIdentityRepository()
    return MemoryProductOverviewRepository(identity_repo=static_id_repo)


@pytest.fixture
def overview_service(memory_repo: MemoryProductOverviewRepository) -> ProductOverviewService:
    return ProductOverviewService(repository=memory_repo)


def test_product_overview_aggregates_localized_display_name_and_aliases(
    overview_service: ProductOverviewService,
):
    """Verify localized display name (nb by default) and translations by locale."""
    carrot = overview_service.get_product_overview(SYNTHETIC_CARROT_PRODUCT_ID, locale="nb")
    assert carrot.canonical_name == "carrot"
    assert carrot.variant == ""
    assert carrot.display_name == "gulrot"
    assert "nb" in carrot.translations_by_locale
    assert "gulrot" in carrot.translations_by_locale["nb"]
    assert "en" in carrot.translations_by_locale
    assert "carrot" in carrot.translations_by_locale["en"]

    # When requested in English
    carrot_en = overview_service.get_product_overview(SYNTHETIC_CARROT_PRODUCT_ID, locale="en")
    assert carrot_en.display_name == "carrot"


def test_product_variants_remain_distinct(
    overview_service: ProductOverviewService,
):
    """Distinct product variants (e.g. fresh vs tinned tomato) remain separate identities."""
    fresh = overview_service.get_product_overview(SYNTHETIC_TOMATO_FRESH_PRODUCT_ID)
    tinned = overview_service.get_product_overview(SYNTHETIC_TOMATO_TINNED_PRODUCT_ID)

    assert fresh.id != tinned.id
    assert fresh.variant == "fresh"
    assert tinned.variant == "tinned"
    assert fresh.display_name != tinned.display_name
    assert "fresh" in fresh.display_name
    assert "tinned" in tinned.display_name


def test_sparse_data_displays_safely_without_errors(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """Sparse product data (no aliases, disconnected Oda, no purchases, no stock) is valid."""
    sparse_id = uuid4()
    sparse_product = CanonicalProduct(id=sparse_id, canonical_name="quinoa", variant="")
    memory_repo.add_product(sparse_product)

    overview = overview_service.get_product_overview(sparse_id)
    assert overview.id == sparse_id
    assert overview.canonical_name == "quinoa"
    assert overview.display_name == "quinoa"
    assert overview.aliases == []
    assert overview.preferred_oda_skus == []
    assert overview.excluded_oda_skus == []
    assert overview.stock.in_stock is False
    assert overview.stock.total_quantity == Decimal(0)
    assert overview.purchase_frequency.status == "unknown"
    assert overview.purchase_frequency.frequency_display == "unknown"


def test_ordered_preferred_oda_skus_and_reordering(
    overview_service: ProductOverviewService,
):
    """Preferred Oda SKUs are stored in an explicit order and can be reordered."""
    product_id = SYNTHETIC_CARROT_PRODUCT_ID

    # 1. Add preferred SKUs in order
    update = ProductPreferencesUpdate(
        preferred_oda_skus=["oda-sku-1", "oda-sku-2", "oda-sku-3"],
        excluded_oda_skus=[],
    )
    result = overview_service.update_preferences(product_id, update)
    assert len(result.preferred_oda_skus) == 3
    assert [p.oda_product_id for p in result.preferred_oda_skus] == [
        "oda-sku-1",
        "oda-sku-2",
        "oda-sku-3",
    ]
    assert [p.rank for p in result.preferred_oda_skus] == [1, 2, 3]

    # 2. Reorder preferred SKUs
    reorder = ProductPreferencesUpdate(
        preferred_oda_skus=["oda-sku-3", "oda-sku-1"],
        excluded_oda_skus=[],
    )
    reordered = overview_service.update_preferences(product_id, reorder)
    assert len(reordered.preferred_oda_skus) == 2
    assert reordered.preferred_oda_skus[0].oda_product_id == "oda-sku-3"
    assert reordered.preferred_oda_skus[0].rank == 1
    assert reordered.preferred_oda_skus[1].oda_product_id == "oda-sku-1"
    assert reordered.preferred_oda_skus[1].rank == 2
    # oda-sku-2 was removed
    assert not any(p.oda_product_id == "oda-sku-2" for p in reordered.preferred_oda_skus)


def test_explicit_exclusions_take_precedence_over_preferred(
    overview_service: ProductOverviewService,
):
    """Exclusions strictly take precedence over preferred rankings."""
    product_id = SYNTHETIC_CARROT_PRODUCT_ID

    update = ProductPreferencesUpdate(
        preferred_oda_skus=[
            PreferredOdaSkuInput(oda_product_id="oda-carrot-a", name="Carrot A"),
            PreferredOdaSkuInput(oda_product_id="oda-carrot-b", name="Carrot B"),
        ],
        excluded_oda_skus=[
            ExcludedOdaSkuInput(oda_product_id="oda-carrot-b", reason="Poor quality packaging")
        ],
    )
    result = overview_service.update_preferences(product_id, update)

    # oda-carrot-b was excluded, so it cannot be preferred
    assert len(result.preferred_oda_skus) == 1
    assert result.preferred_oda_skus[0].oda_product_id == "oda-carrot-a"
    assert result.preferred_oda_skus[0].rank == 1

    assert len(result.excluded_oda_skus) == 1
    assert result.excluded_oda_skus[0].oda_product_id == "oda-carrot-b"
    assert result.excluded_oda_skus[0].reason == "Poor quality packaging"


def test_exclusion_can_be_deliberately_reversed(
    overview_service: ProductOverviewService,
):
    """An exclusion survives until deliberately reversed by updating preferences."""
    product_id = SYNTHETIC_BARLEY_PRODUCT_ID

    # Add exclusion
    overview_service.update_preferences(
        product_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-barley-1"],
            excluded_oda_skus=["oda-barley-2"],
        ),
    )
    res1 = overview_service.get_product_overview(product_id)
    assert any(e.oda_product_id == "oda-barley-2" for e in res1.excluded_oda_skus)

    # Deliberately reverse exclusion by omitting it
    overview_service.update_preferences(
        product_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-barley-1", "oda-barley-2"],
            excluded_oda_skus=[],
        ),
    )
    res2 = overview_service.get_product_overview(product_id)
    assert len(res2.excluded_oda_skus) == 0
    assert len(res2.preferred_oda_skus) == 2
    assert [p.oda_product_id for p in res2.preferred_oda_skus] == [
        "oda-barley-1",
        "oda-barley-2",
    ]


def test_filter_and_rank_candidates_applies_preferred_order_and_drops_exclusions(
    overview_service: ProductOverviewService,
):
    """Candidate filtering drops excluded SKUs and promotes preferred SKUs in rank order."""
    product_id = SYNTHETIC_COFFEE_PRODUCT_ID

    overview_service.update_preferences(
        product_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["sku-filterkaffe-fav", "sku-filterkaffe-alt"],
            excluded_oda_skus=["sku-whitener-bad"],
        ),
    )

    candidates = [
        BasketCandidate(
            id="oda:sku-generic",
            provider="oda",
            product_id="sku-generic",
            name="Generic Coffee",
            description="",
            match_query="coffee",
            match_reason="Search match",
        ),
        BasketCandidate(
            id="oda:sku-whitener-bad",
            provider="oda",
            product_id="sku-whitener-bad",
            name="Coffee Whitener",
            description="",
            match_query="coffee",
            match_reason="Search match",
        ),
        BasketCandidate(
            id="oda:sku-filterkaffe-alt",
            provider="oda",
            product_id="sku-filterkaffe-alt",
            name="Alternative Filterkaffe",
            description="",
            match_query="coffee",
            match_reason="Search match",
        ),
        BasketCandidate(
            id="oda:sku-filterkaffe-fav",
            provider="oda",
            product_id="sku-filterkaffe-fav",
            name="Favorite Filterkaffe",
            description="",
            match_query="coffee",
            match_reason="Search match",
        ),
    ]

    filtered = overview_service.filter_and_rank_candidates(product_id, candidates)

    # 1. sku-whitener-bad is completely excluded
    assert not any(c.product_id == "sku-whitener-bad" for c in filtered)
    assert len(filtered) == 3

    # 2. Preferred SKUs are ranked at top in rank order
    assert filtered[0].product_id == "sku-filterkaffe-fav"
    assert filtered[0].preference_rank == 1
    assert filtered[0].is_preferred is True
    assert "Suggested from household preferred Oda SKU" in filtered[0].match_reason

    assert filtered[1].product_id == "sku-filterkaffe-alt"
    assert filtered[1].preference_rank == 2
    assert filtered[1].is_preferred is True

    # 3. Non-preferred non-excluded candidate is last
    assert filtered[2].product_id == "sku-generic"
    assert filtered[2].is_preferred is False


def test_suggest_preferred_sku_returns_highest_available_preference(
    overview_service: ProductOverviewService,
):
    """suggest_preferred_sku picks the top-ranked preference while respecting availability."""
    product_id = SYNTHETIC_CARROT_PRODUCT_ID

    overview_service.update_preferences(
        product_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["c-rank-1", "c-rank-2", "c-rank-3"],
            excluded_oda_skus=["c-rank-1"],  # rank 1 excluded!
        ),
    )

    # When all are theoretically available
    best, reason = overview_service.suggest_preferred_sku(product_id)
    assert best is not None
    # c-rank-1 was excluded, so c-rank-2 is the highest available preferred SKU
    assert best.oda_product_id == "c-rank-2"
    assert "rank 1" in reason  # c-rank-2 was assigned rank 1 because c-rank-1 was excluded

    # When only c-rank-3 is in stock / available
    best_avail, _ = overview_service.suggest_preferred_sku(
        product_id, available_sku_ids=["c-rank-3", "other-sku"]
    )
    assert best_avail is not None
    assert best_avail.oda_product_id == "c-rank-3"


def test_zero_inference_of_stock_from_purchase_history(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """CRITICAL: Stock is strictly from kitchen_inventory and NEVER inferred from purchases."""
    product_id = SYNTHETIC_CARROT_PRODUCT_ID

    # Record 10 verified purchases in history
    now = datetime.now(UTC)
    for i in range(10):
        memory_repo.record_purchase_evidence(
            product_id=product_id,
            purchased_at=now - timedelta(days=i * 7),
            provenance="provider_verified",
            evidence_source="receipt",
            quantity=Decimal("2"),
            unit="kg",
        )

    overview = overview_service.get_product_overview(product_id)

    # Stock MUST be 0 and in_stock=False because kitchen_inventory has no entry
    assert overview.stock.in_stock is False
    assert overview.stock.total_quantity == Decimal(0)
    assert overview.stock.items == []
    assert overview.stock.provenance == "kitchen_inventory"

    # Purchases are counted in purchase frequency, NOT stock
    assert overview.purchase_frequency.sample_size == 10
    assert overview.purchase_frequency.status == "known"


def test_explicit_stock_management(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """Explicit inventory entries appear with quantity, unit, and provenance."""
    product_id = SYNTHETIC_CHICKPEAS_PRODUCT_ID

    # Add stock item to inventory
    memory_repo.add_stock_item(
        product_id=product_id,
        name="kikerter",
        quantity=Decimal("3"),
        unit="can",
        category="Pantry",
        note="Top shelf",
    )

    overview = overview_service.get_product_overview(product_id)
    assert overview.stock.in_stock is True
    assert overview.stock.total_quantity == Decimal("3")
    assert overview.stock.unit == "can"
    assert len(overview.stock.items) == 1
    assert overview.stock.items[0].note == "Top shelf"
    assert overview.stock.items[0].provenance == "kitchen_inventory"


def test_purchase_frequency_insufficient_history_returns_unknown():
    """0 or 1 purchase event or same-day events display unknown rather than an invented estimate."""
    now = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)

    # 0 events
    zero_freq = compute_purchase_frequency([])
    assert zero_freq.status == "unknown"
    assert zero_freq.average_interval_days is None
    assert zero_freq.sample_size == 0
    assert zero_freq.frequency_display == "unknown"

    # 1 event
    one_event = [
        PurchaseEvidenceRecord(
            purchased_at=now,
            provenance="provider_verified",
            evidence_source="receipt",
        )
    ]
    one_freq = compute_purchase_frequency(one_event)
    assert one_freq.status == "unknown"
    assert one_freq.average_interval_days is None
    assert one_freq.sample_size == 1
    assert one_freq.frequency_display == "unknown"
    assert one_freq.first_purchased_at == now
    assert one_freq.last_purchased_at == now

    # Multiple events on the exact same date
    same_day_events = [
        PurchaseEvidenceRecord(
            purchased_at=now,
            provenance="provider_verified",
            evidence_source="receipt",
        ),
        PurchaseEvidenceRecord(
            purchased_at=now + timedelta(hours=2),
            provenance="user_confirmed",
            evidence_source="manual_confirmation",
        ),
    ]
    same_day_freq = compute_purchase_frequency(same_day_events)
    assert same_day_freq.status == "unknown"
    assert same_day_freq.average_interval_days is None
    assert same_day_freq.sample_size == 2
    assert same_day_freq.frequency_display == "unknown"


def test_purchase_frequency_calculated_from_confirmed_evidence():
    """Observed average purchase frequency is computed over the date range for confirmed purchases."""
    day1 = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)
    day8 = datetime(2026, 4, 8, 11, 0, tzinfo=UTC)
    day15 = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)

    records = [
        PurchaseEvidenceRecord(
            purchased_at=day1,
            provenance="provider_verified",
            evidence_source="receipt",
        ),
        PurchaseEvidenceRecord(
            purchased_at=day8,
            provenance="user_confirmed",
            evidence_source="kitchen_history",
        ),
        PurchaseEvidenceRecord(
            purchased_at=day15,
            provenance="provider_verified",
            evidence_source="receipt",
        ),
    ]

    freq = compute_purchase_frequency(records)
    assert freq.status == "known"
    assert freq.sample_size == 3
    assert freq.date_range_days == 14
    assert freq.average_interval_days == 7.0
    assert freq.frequency_display == "every 7 days"
    assert freq.counts_by_provenance.provider_verified == 2
    assert freq.counts_by_provenance.user_confirmed == 1
    assert freq.counts_by_provenance.uncertain == 0


def test_uncertain_orders_are_never_asserted_as_confirmed_purchases():
    """Unconfirmed orders (e.g. pending Oda orders, Trumf without receipt) are tracked as uncertain and never counted in sample_size."""
    day1 = datetime(2026, 4, 1, 10, 0, tzinfo=UTC)

    records = [
        # Only 1 confirmed purchase
        PurchaseEvidenceRecord(
            purchased_at=day1,
            provenance="provider_verified",
            evidence_source="receipt",
        ),
        # 4 uncertain / pending orders
        PurchaseEvidenceRecord(
            purchased_at=day1 + timedelta(days=3),
            provenance="uncertain",
            evidence_source="oda_pending_order",
        ),
        PurchaseEvidenceRecord(
            purchased_at=day1 + timedelta(days=7),
            provenance="uncertain",
            evidence_source="trumf_no_receipt",
        ),
    ]

    freq = compute_purchase_frequency(records)
    # Since only 1 confirmed purchase exists, frequency MUST be unknown!
    assert freq.status == "unknown"
    assert freq.sample_size == 1
    assert freq.average_interval_days is None
    assert freq.frequency_display == "unknown"
    assert freq.counts_by_provenance.provider_verified == 1
    assert freq.counts_by_provenance.user_confirmed == 0
    assert freq.counts_by_provenance.uncertain == 2


def test_catalogue_overview_search_query(
    overview_service: ProductOverviewService,
):
    """Search query filters catalogue overview matching localized aliases or canonical names."""
    # Search by Norwegian alias
    carrot_results = overview_service.get_overview(query="gulrot")
    assert any(p.canonical_name == "carrot" for p in carrot_results)

    # Search by English canonical name
    coffee_results = overview_service.get_overview(query="coffee")
    assert any(p.canonical_name == "coffee" for p in coffee_results)

    # Search with no match
    none_results = overview_service.get_overview(query="nonexistent_xyz")
    assert len(none_results) == 0


def test_rest_api_overview_and_preference_endpoints(
    overview_service: ProductOverviewService,
):
    """Test REST endpoints GET /catalogue/overview, GET /overview/{id}, and PUT /preferences."""

    class DummyFeatures:
        def __init__(self, svc):
            self.product_overview = svc

    app = FastAPI()
    app.include_router(feature_router(DummyFeatures(overview_service), None))

    with TestClient(app) as client:
        # 1. GET /api/catalogue/overview
        overview_res = client.get("/api/catalogue/overview")
        assert overview_res.status_code == 200
        items = overview_res.json()
        assert len(items) > 0

        # 2. GET /api/catalogue/overview/{product_id}
        carrot_id = str(SYNTHETIC_CARROT_PRODUCT_ID)
        prod_res = client.get(f"/api/catalogue/overview/{carrot_id}")
        assert prod_res.status_code == 200
        carrot_data = prod_res.json()
        assert carrot_data["id"] == carrot_id
        assert carrot_data["canonical_name"] == "carrot"

        # 404 for unknown product
        bad_id = str(uuid4())
        assert client.get(f"/api/catalogue/overview/{bad_id}").status_code == 404

        # 3. PUT /api/catalogue/products/{product_id}/preferences
        pref_res = client.put(
            f"/api/catalogue/products/{carrot_id}/preferences",
            json={
                "preferred_oda_skus": ["oda-101", "oda-102"],
                "excluded_oda_skus": ["oda-999"],
            },
        )
        assert pref_res.status_code == 200
        updated = pref_res.json()
        assert [p["oda_product_id"] for p in updated["preferred_oda_skus"]] == [
            "oda-101",
            "oda-102",
        ]
        assert [e["oda_product_id"] for e in updated["excluded_oda_skus"]] == ["oda-999"]

        # 4. GET /api/catalogue/products/{product_id}/preferences
        get_pref_res = client.get(f"/api/catalogue/products/{carrot_id}/preferences")
        assert get_pref_res.status_code == 200
        pref_payload = get_pref_res.json()
        assert len(pref_payload["preferred_oda_skus"]) == 2
        assert len(pref_payload["excluded_oda_skus"]) == 1


@pytest.mark.asyncio
async def test_mcp_overview_and_preference_tools(
    overview_service: ProductOverviewService,
):
    """Test MCP tools get_catalogue_overview, get_catalogue_product_overview, and set_catalogue_product_preferences."""

    class DummyFeatures:
        def __init__(self, svc):
            self.product_overview = svc

    dummy_features = DummyFeatures(overview_service)
    mcp = FastMCP("test-catalogue")
    install_feature_tools(mcp, dummy_features, None)

    token = history_context.set(HistoryContext(actor_id="mcp-test-actor", channel="mcp"))
    try:
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert "get_catalogue_overview" in tools
            assert "get_catalogue_product_overview" in tools
            assert "set_catalogue_product_preferences" in tools

            assert tools["get_catalogue_overview"].annotations.readOnlyHint is True
            assert tools["get_catalogue_product_overview"].annotations.readOnlyHint is True
            assert tools["set_catalogue_product_preferences"].annotations.readOnlyHint is False

            # Call get_catalogue_overview
            overview_call = await client.call_tool("get_catalogue_overview", {"query": "carrot"})
            results = overview_call.structured_content["result"]
            assert len(results) >= 1
            assert results[0]["canonical_name"] == "carrot"

            # Call set_catalogue_product_preferences
            pref_call = await client.call_tool(
                "set_catalogue_product_preferences",
                {
                    "product_id": str(SYNTHETIC_CARROT_PRODUCT_ID),
                    "preferred_oda_skus": ["mcp-carrot-1"],
                    "excluded_oda_skus": ["mcp-carrot-bad"],
                },
            )
            updated_carrot = pref_call.structured_content.get(
                "result", pref_call.structured_content
            )
            assert updated_carrot["preferred_oda_skus"][0]["oda_product_id"] == "mcp-carrot-1"
            assert updated_carrot["excluded_oda_skus"][0]["oda_product_id"] == "mcp-carrot-bad"
    finally:
        history_context.reset(token)


def test_product_overview_surfaces_distinct_remembered_and_previously_purchased_skus(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """Remembered Oda SKUs and previously purchased store/receipt SKUs stay distinct from preferences and stock."""
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    pid = SYNTHETIC_CARROT_PRODUCT_ID

    memory_repo.record_remembered_sku(
        pid,
        "oda-rem-1",
        name="Bama Gulrot 1 kg",
        brand="Bama",
        price="24.90",
        last_selected_at=now,
    )
    memory_repo.record_previously_purchased_sku(
        pid,
        "7032069704999",
        "rema1000",
        name="REMA 1000 Gulrøtter 1 kg",
        store_name="REMA 1000 Vulkan",
        ean="7032069704999",
        last_purchased_at=now - timedelta(days=2),
        provenance="provider_verified",
    )
    overview_service.update_preferences(
        pid,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-pref-1"],
            excluded_oda_skus=["oda-excl-1"],
        ),
    )

    overview = overview_service.get_product_overview(pid)
    assert [s.oda_product_id for s in overview.preferred_oda_skus] == ["oda-pref-1"]
    assert [s.oda_product_id for s in overview.excluded_oda_skus] == ["oda-excl-1"]
    assert [s.oda_product_id for s in overview.remembered_oda_skus] == ["oda-rem-1"]
    assert [s.sku_id for s in overview.previously_purchased_skus] == ["7032069704999"]
    assert overview.previously_purchased_skus[0].provider == "rema1000"
    # Neither remembered nor purchased SKUs ever invent kitchen stock
    assert overview.stock.in_stock is False
    assert overview.stock.total_quantity == Decimal(0)


def test_update_stock_via_service_and_rest_api(
    overview_service: ProductOverviewService,
):
    """PUT /api/catalogue/products/{id}/stock creates and updates stock directly on a product."""

    class DummyFeatures:
        def __init__(self, svc):
            self.product_overview = svc

    app = FastAPI()
    app.include_router(feature_router(DummyFeatures(overview_service), None))

    carrot_id = str(SYNTHETIC_CARROT_PRODUCT_ID)
    with TestClient(app) as client:
        create_res = client.put(
            f"/api/catalogue/products/{carrot_id}/stock",
            json={"quantity": "2.5", "unit": "kg", "note": "Skuffen"},
        )
        assert create_res.status_code == 200
        created = create_res.json()
        assert created["stock"]["in_stock"] is True
        assert Decimal(str(created["stock"]["total_quantity"])) == Decimal("2500")
        assert created["stock"]["unit"] == "g"
        assert created["stock"]["items"][0]["note"] == "Skuffen"

        update_res = client.put(
            f"/api/catalogue/products/{carrot_id}/stock",
            json={"quantity": "4", "unit": "stk", "note": "Oppdatert"},
        )
        assert update_res.status_code == 200
        updated = update_res.json()
        assert Decimal(str(updated["stock"]["total_quantity"])) == Decimal("4")
        assert updated["stock"]["unit"] == "stk"

        missing_res = client.put(
            f"/api/catalogue/products/{uuid4()}/stock",
            json={"quantity": "1", "unit": "stk"},
        )
        assert missing_res.status_code == 404


def test_merge_products_via_service_rest_and_mcp(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """Merging duplicate canonical products moves aliases, stock, preferences, SKUs, and removes source."""
    source_id = uuid4()
    target_id = SYNTHETIC_CARROT_PRODUCT_ID
    memory_repo.add_product(CanonicalProduct(id=source_id, canonical_name="gulrøtter", variant=""))
    memory_repo.add_alias(source_id, "gulrøtter", locale="nb", context="recipe", reviewed=True)
    memory_repo.add_alias(source_id, "gulrot", locale="nb", context="recipe", reviewed=True)
    memory_repo.add_stock_item(source_id, "gulrøtter", Decimal("3"), "stk")
    memory_repo.record_remembered_sku(source_id, "oda-src-rem", name="Kilde Gulrot")
    memory_repo.record_previously_purchased_sku(
        source_id, "700001", "coop", name="Coop Gulrot", provenance="provider_verified"
    )
    overview_service.update_preferences(
        target_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-target-1", "oda-conflict"],
            excluded_oda_skus=[],
        ),
    )
    overview_service.update_preferences(
        source_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-src-pref", "oda-target-1"],
            excluded_oda_skus=["oda-conflict"],
        ),
    )

    class DummyFeatures:
        def __init__(self, svc):
            self.product_overview = svc

    app = FastAPI()
    app.include_router(feature_router(DummyFeatures(overview_service), None))

    with TestClient(app) as client:
        # Self-merge rejected
        assert (
            client.post(
                f"/api/catalogue/products/{target_id}/merge",
                json={"source_product_id": str(target_id)},
            ).status_code
            == 422
        )

        merge_res = client.post(
            f"/api/catalogue/products/{target_id}/merge",
            json={"source_product_id": str(source_id)},
        )
        assert merge_res.status_code == 200
        merged = merge_res.json()
        assert merged["id"] == str(target_id)
        assert any(a["alias"] == "gulrøtter" for a in merged["aliases"])
        assert merged["stock"]["in_stock"] is True
        assert Decimal(str(merged["stock"]["total_quantity"])) == Decimal("3")
        # Exclusion from source wins over target preference on oda-conflict
        assert [p["oda_product_id"] for p in merged["preferred_oda_skus"]] == [
            "oda-target-1",
            "oda-src-pref",
        ]
        assert [p["rank"] for p in merged["preferred_oda_skus"]] == [1, 2]
        assert [e["oda_product_id"] for e in merged["excluded_oda_skus"]] == ["oda-conflict"]
        assert any(r["oda_product_id"] == "oda-src-rem" for r in merged["remembered_oda_skus"])
        assert any(p["sku_id"] == "700001" for p in merged["previously_purchased_skus"])

        # Source product is removed
        assert client.get(f"/api/catalogue/overview/{source_id}").status_code == 404


def test_merge_preview_returns_exact_counts_and_variant_mismatch(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """Pre-merge preview reports exact impact counts and variant mismatches without mutating data."""
    target_same = uuid4()
    target_diff = uuid4()
    source_id = uuid4()
    memory_repo.add_product(
        CanonicalProduct(id=target_same, canonical_name="tomato", variant="tinned")
    )
    memory_repo.add_product(
        CanonicalProduct(id=target_diff, canonical_name="tomato", variant="fresh")
    )
    memory_repo.add_product(
        CanonicalProduct(id=source_id, canonical_name="hermetisk tomat", variant="tinned")
    )
    memory_repo.add_alias(
        source_id, "hermetisk tomat", locale="nb", context="recipe", reviewed=True
    )
    memory_repo.add_alias(source_id, "tomat på boks", locale="nb", context="recipe", reviewed=False)
    memory_repo.add_stock_item(source_id, "hermetisk tomat", Decimal("4"), "boks")
    memory_repo.record_recipe_link(source_id, count=2)
    memory_repo.record_purchase_evidence(
        source_id,
        datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        "provider_verified",
        "oda",
        product_name="Mutti Hakkede Tomater",
        oda_product_id="oda-mutti",
    )
    overview_service.update_preferences(
        source_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-mutti", "oda-eldorado"],
            excluded_oda_skus=["oda-bad"],
        ),
    )

    class DummyFeatures:
        def __init__(self, svc):
            self.product_overview = svc

    app = FastAPI()
    app.include_router(feature_router(DummyFeatures(overview_service), None))

    with TestClient(app) as client:
        # Self-preview rejected
        assert (
            client.get(
                f"/api/catalogue/products/{target_same}/merge-preview?source_id={target_same}"
            ).status_code
            == 422
        )
        # Missing product rejected
        assert (
            client.get(
                f"/api/catalogue/products/{target_same}/merge-preview?source_id={uuid4()}"
            ).status_code
            == 404
        )

        same_res = client.get(
            f"/api/catalogue/products/{target_same}/merge-preview?source_id={source_id}"
        )
        assert same_res.status_code == 200
        same_data = same_res.json()
        assert same_data["variant_mismatch"] is False
        assert same_data["target_variant"] == "tinned"
        assert same_data["source_variant"] == "tinned"
        assert same_data["aliases_to_move"] == 2
        assert same_data["stock_rows_to_move"] == 1
        assert same_data["recipe_links_to_move"] == 2
        assert same_data["preferences_to_move"] == 2
        assert same_data["exclusions_to_move"] == 1
        assert same_data["purchase_history_rows_to_move"] == 1

        diff_res = client.get(
            f"/api/catalogue/products/{target_diff}/merge-preview?source_id={source_id}"
        )
        assert diff_res.status_code == 200
        diff_data = diff_res.json()
        assert diff_data["variant_mismatch"] is True
        assert diff_data["target_variant"] == "fresh"
        assert diff_data["source_variant"] == "tinned"

        # Preview is strictly read-only: source still exists
        assert client.get(f"/api/catalogue/overview/{source_id}").status_code == 200


@pytest.mark.asyncio
async def test_mcp_stock_and_merge_tools(
    overview_service: ProductOverviewService,
    memory_repo: MemoryProductOverviewRepository,
):
    """Test MCP tools set_catalogue_product_stock and merge_canonical_products."""
    dup_id = uuid4()
    memory_repo.add_product(CanonicalProduct(id=dup_id, canonical_name="gulrot dup", variant=""))
    memory_repo.add_alias(dup_id, "gulrot-dup", locale="nb", context="recipe", reviewed=False)

    class DummyFeatures:
        def __init__(self, svc):
            self.product_overview = svc

    mcp = FastMCP("test-catalogue-merge")
    install_feature_tools(mcp, DummyFeatures(overview_service), None)

    token = history_context.set(HistoryContext(actor_id="mcp-actor", channel="mcp"))
    try:
        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            assert "set_catalogue_product_stock" in tools
            assert "merge_canonical_products" in tools

            stock_res = await client.call_tool(
                "set_catalogue_product_stock",
                {
                    "product_id": str(dup_id),
                    "quantity": "2",
                    "unit": "stk",
                    "note": "Kjøleskap",
                },
            )
            stock_data = stock_res.structured_content.get("result", stock_res.structured_content)
            assert stock_data["stock"]["in_stock"] is True

            merge_res = await client.call_tool(
                "merge_canonical_products",
                {
                    "target_product_id": str(SYNTHETIC_CARROT_PRODUCT_ID),
                    "source_product_id": str(dup_id),
                },
            )
            merged = merge_res.structured_content.get("result", merge_res.structured_content)
            assert merged["id"] == str(SYNTHETIC_CARROT_PRODUCT_ID)
            assert any(a["alias"] == "gulrot-dup" for a in merged["aliases"])
            assert merged["stock"]["in_stock"] is True
    finally:
        history_context.reset(token)


@pytest.fixture
def overview_sqlite_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """CREATE TABLE kitchen_canonical_products (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    variant TEXT NOT NULL DEFAULT '',
                    UNIQUE(canonical_name, variant)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_product_aliases (
                    locale TEXT NOT NULL,
                    context TEXT NOT NULL DEFAULT '',
                    normalized_alias TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    reviewed BOOLEAN NOT NULL DEFAULT 0,
                    PRIMARY KEY (locale, context, normalized_alias, product_id)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_canonical_product_oda_preferences (
                    household_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    oda_product_id TEXT NOT NULL,
                    preference_order INTEGER NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (household_id, product_id, oda_product_id)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_canonical_product_oda_exclusions (
                    household_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    oda_product_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (household_id, product_id, oda_product_id)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_inventory (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    normalized_unit TEXT NOT NULL,
                    quantity NUMERIC NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Other',
                    note TEXT,
                    UNIQUE(normalized_name, normalized_unit)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE recipes (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    servings INTEGER NOT NULL,
                    ingredients TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual'
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_basket_selections (
                    id TEXT PRIMARY KEY,
                    week_start TEXT NOT NULL,
                    week_end TEXT NOT NULL,
                    need_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_food_catalogue (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    display_name TEXT NOT NULL
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_catalogue_sku_options (
                    id TEXT PRIMARY KEY,
                    household_id TEXT NOT NULL,
                    food_catalogue_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    current_snapshot_id TEXT
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_catalogue_sku_snapshots (
                    id TEXT PRIMARY KEY,
                    household_id TEXT NOT NULL,
                    option_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    snapshot_ref TEXT,
                    observed_at TEXT NOT NULL,
                    evidence_status TEXT NOT NULL
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_catalogue_sku_selections (
                    id TEXT PRIMARY KEY,
                    household_id TEXT NOT NULL,
                    option_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    selected_at TEXT NOT NULL
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_catalogue_sku_purchases (
                    id TEXT PRIMARY KEY,
                    household_id TEXT NOT NULL,
                    option_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    purchased_at TEXT NOT NULL,
                    quantity NUMERIC,
                    quantity_unit TEXT,
                    actor_id TEXT NOT NULL
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_history (
                    id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    structured TEXT NOT NULL,
                    actor_id TEXT
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE provider_order_sources (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT 'default',
                    external_id TEXT,
                    revision_id TEXT,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                )"""
            )
        )
    yield engine
    engine.dispose()


def test_postgres_repository_remembered_vs_purchased_skus_stock_edit_and_atomic_merge(
    overview_sqlite_engine,
):
    """DB-backed test for distinct remembered/purchased SKUs, stock upsert, and atomic merge."""
    hid = UUID("00000000-0000-0000-0000-000000000001")
    target_id = uuid4()
    source_id = uuid4()
    recipe_id = uuid4()
    now = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)

    with overview_sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO kitchen_canonical_products (id, canonical_name, variant) "
                "VALUES (:t, 'carrot', ''), (:s, 'gulrøtter', '')"
            ),
            {"t": target_id, "s": source_id},
        )
        conn.execute(
            text(
                "INSERT INTO kitchen_product_aliases (locale, context, normalized_alias, alias, product_id, reviewed) "
                "VALUES ('nb', 'recipe', 'gulrot', 'gulrot', :t, 0), "
                "('nb', 'recipe', 'gulrot', 'gulrot', :s, 1), "
                "('nb', 'recipe', 'gulrøtter', 'gulrøtter', :s, 1)"
            ),
            {"t": target_id, "s": source_id},
        )
        conn.execute(
            text(
                "INSERT INTO recipes (id, title, servings, ingredients, instructions) "
                "VALUES (:rid, 'Gulrotsuppe', 4, :ings, '[]')"
            ),
            {
                "rid": recipe_id,
                "ings": json.dumps(
                    [
                        {
                            "name": "gulrøtter",
                            "quantity": 3,
                            "unit": "stk",
                            "canonical_product_id": str(source_id),
                        }
                    ]
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO kitchen_basket_selections (id, week_start, week_end, need_id, provider, product_id, payload, updated_at) "
                "VALUES (:id, '2026-11-16', '2026-11-22', :nid, 'oda', 'oda-rem-99', :payload, :ts)"
            ),
            {
                "id": uuid4(),
                "nid": uuid4(),
                "payload": json.dumps(
                    {
                        "need_id": str(uuid4()),
                        "product": {
                            "provider": "oda",
                            "product_id": "oda-rem-99",
                            "name": "Bama Gulrot 1 kg",
                            "brand": "Bama",
                            "price": "22.90",
                            "pack_quantity": "1",
                            "pack_unit": "kg",
                            "observed_at": now.isoformat(),
                        },
                        "packages": 1,
                    }
                ),
                "ts": now.isoformat(),
            },
        )
        # Confirmed Rema 1000 receipt & delivered Oda order vs pending Oda order
        conn.execute(
            text(
                "INSERT INTO provider_order_sources (id, provider, account_id, external_id, revision_id, payload, observed_at) "
                "VALUES (:id1, 'rema1000', 'malo', 'rema-1', 'r1', :p1, :ts1), "
                "(:id2, 'oda', 'malo', 'oda-delivered', 'r1', :p2, :ts2), "
                "(:id3, 'oda', 'malo', 'oda-pending', 'r1', :p3, :ts3)"
            ),
            {
                "id1": uuid4(),
                "p1": json.dumps(
                    {
                        "receiptId": "rema-1",
                        "storeName": "REMA 1000 Vulkan",
                        "timestamp": (now - timedelta(days=5)).isoformat(),
                        "hasReceipt": True,
                        "lines": [
                            {
                                "name": "Gulrøtter 1 kg",
                                "ean": "7032069704555",
                                "quantity": 1,
                                "unit": "kg",
                                "canonical_product_id": str(source_id),
                            }
                        ],
                    }
                ),
                "ts1": (now - timedelta(days=5)).isoformat(),
                "id2": uuid4(),
                "p2": json.dumps(
                    {
                        "order": {
                            "orderNumber": "ODA-100",
                            "deliveryDate": "2026-11-10",
                            "products": [
                                {
                                    "product": {
                                        "id": 55501,
                                        "name": "Norsk Gulrot 750 g",
                                        "unitName": "kg",
                                    },
                                    "quantity": 1,
                                }
                            ],
                        },
                        "tracking": {"status": "delivered"},
                    }
                ),
                "ts2": (now - timedelta(days=8)).isoformat(),
                "id3": uuid4(),
                "p3": json.dumps(
                    {
                        "order": {
                            "orderNumber": "ODA-PENDING",
                            "deliveryDate": "2026-11-20",
                            "products": [
                                {
                                    "product": {
                                        "id": 55599,
                                        "name": "Ventende Gulrot",
                                        "unitName": "kg",
                                    },
                                    "quantity": 1,
                                }
                            ],
                        },
                        "tracking": {"status": "picking"},
                    }
                ),
                "ts3": now.isoformat(),
            },
        )
        conn.execute(
            text(
                "INSERT INTO kitchen_inventory (id, name, normalized_name, unit, normalized_unit, quantity, category, note) "
                "VALUES (:id1, 'gulrot', 'gulrot', 'stk', 'stk', 2, 'Produce', 'Fra mål'), "
                "(:id2, 'gulrot_extra', 'gulrot_extra', 'stk', 'stk', 3, 'Produce', 'Fra kilde')"
            ),
            {"id1": uuid4(), "id2": uuid4()},
        )
        # Add alias for gulrot_extra on source so source stock matches it
        conn.execute(
            text(
                "INSERT INTO kitchen_product_aliases (locale, context, normalized_alias, alias, product_id, reviewed) "
                "VALUES ('nb', 'recipe', 'gulrot_extra', 'gulrot_extra', :s, 0)"
            ),
            {"s": source_id},
        )
        conn.execute(
            text(
                "INSERT INTO kitchen_history (id, occurred_at, channel, kind, message, structured, actor_id) "
                "VALUES (:hid1, :ts, 'app', 'purchase', 'Bought gulrøtter', :st, 'malo')"
            ),
            {
                "hid1": uuid4(),
                "ts": (now - timedelta(days=1)).isoformat(),
                "st": json.dumps(
                    {
                        "need": {"name": "gulrøtter", "unit": "stk"},
                        "canonical_product_id": str(source_id),
                        "actual_quantity": 2,
                        "actual_unit": "stk",
                    }
                ),
            },
        )

    repo = PostgresProductOverviewRepository(overview_sqlite_engine)
    service = ProductOverviewService(repository=repo, household_id=hid)

    # Check remembered vs purchased before merge on target
    target_before = service.get_product_overview(target_id)
    assert [r.oda_product_id for r in target_before.remembered_oda_skus] == ["oda-rem-99"]
    purchased_ids = {p.sku_id for p in target_before.previously_purchased_skus}
    assert "55501" in purchased_ids  # delivered Oda order
    assert "55599" not in purchased_ids  # pending/picking order NOT treated as purchased!
    assert target_before.purchase_frequency.counts_by_provenance.uncertain == 1

    # Stock edit via service
    service.update_stock(
        target_id, ProductStockUpdate(quantity=Decimal("5"), unit="stk", note="Ny")
    )
    target_after_stock = service.get_product_overview(target_id)
    assert target_after_stock.stock.in_stock is True
    assert target_after_stock.stock.total_quantity == Decimal("5")

    # Preview merge before executing
    preview = service.preview_merge(target_id, source_id)
    assert preview.variant_mismatch is False
    assert preview.aliases_to_move == 3
    assert preview.stock_rows_to_move == 1
    assert preview.recipe_links_to_move == 1
    assert preview.preferences_to_move == 0
    assert preview.exclusions_to_move == 0
    assert preview.purchase_history_rows_to_move == 2

    # Merge source into target
    merged = service.merge_products(target_id, source_id, actor_id="malo")
    assert merged.id == target_id
    # Reviewed=True from source won conflict on 'gulrot', and 'gulrøtter' moved over
    alias_by_name = {a.alias: a for a in merged.aliases}
    assert alias_by_name["gulrot"].reviewed is True
    assert "gulrøtter" in alias_by_name
    # Combined stock from target (5) + source (3) = 8
    assert merged.stock.total_quantity == Decimal("8")
    # Rema 1000 receipt SKU (previously mapped to source_id) now shows on target!
    merged_purchased_ids = {p.sku_id for p in merged.previously_purchased_skus}
    assert {"55501", "7032069704555"} <= merged_purchased_ids

    # Verify recipe ingredient & provider_order_sources & kitchen_history were repointed, and source deleted
    with overview_sqlite_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM kitchen_canonical_products WHERE id = :id"),
                {"id": source_id},
            ).scalar_one()
            == 0
        )
        ings = json.loads(
            conn.execute(
                text("SELECT ingredients FROM recipes WHERE id = :id"), {"id": recipe_id}
            ).scalar_one()
        )
        assert ings[0]["canonical_product_id"] == str(target_id)
        hist_st = json.loads(
            conn.execute(
                text("SELECT structured FROM kitchen_history WHERE kind = 'purchase'")
            ).scalar_one()
        )
        assert hist_st["canonical_product_id"] == str(target_id)


def test_postgres_repository_merge_preview_detects_variant_mismatch_and_preferences(
    overview_sqlite_engine,
):
    """DB-backed merge preview counts prefs/exclusions and flags differing variants."""
    hid = UUID("00000000-0000-0000-0000-000000000001")
    target_id = uuid4()
    source_id = uuid4()
    with overview_sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO kitchen_canonical_products (id, canonical_name, variant) "
                "VALUES (:t, 'milk', 'whole'), (:s, 'melk', 'skimmed')"
            ),
            {"t": target_id, "s": source_id},
        )
    repo = PostgresProductOverviewRepository(overview_sqlite_engine)
    service = ProductOverviewService(repository=repo, household_id=hid)
    service.update_preferences(
        source_id,
        ProductPreferencesUpdate(
            preferred_oda_skus=["oda-skummet-1"],
            excluded_oda_skus=["oda-bad-1", "oda-bad-2"],
        ),
    )

    preview = service.preview_merge(target_id, source_id)
    assert preview.variant_mismatch is True
    assert preview.target_variant == "whole"
    assert preview.source_variant == "skimmed"
    assert preview.preferences_to_move == 1
    assert preview.exclusions_to_move == 2


def test_postgres_repository_excludes_cancelled_and_returned_provider_orders(
    overview_sqlite_engine,
):
    hid = UUID("00000000-0000-0000-0000-000000000001")
    prod_id = uuid4()
    now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    with overview_sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO kitchen_canonical_products (id, canonical_name, variant) "
                "VALUES (:pid, 'helmelk', '')"
            ),
            {"pid": prod_id},
        )
        conn.execute(
            text(
                "INSERT INTO provider_order_sources "
                "(id, provider, account_id, external_id, revision_id, payload, observed_at) "
                "VALUES "
                "(:id1, 'oda', 'malo', 'oda-canc', 'r1', :p1, :ts1), "
                "(:id2, 'oda', 'malo', 'oda-canc', 'r2', :p2, :ts2), "
                "(:id3, 'trumf', 'malo', 'trumf-refund', 'r1', :p3, :ts3), "
                "(:id4, 'rema1000', 'malo', 'rema-partial', 'r1', :p4, :ts4)"
            ),
            {
                "id1": uuid4(),
                "p1": json.dumps(
                    {
                        "order": {
                            "orderNumber": "oda-canc",
                            "deliveryDate": "2026-11-10",
                            "products": [
                                {
                                    "product": {"id": 9001, "name": "Helmelk 1L", "unitName": "l"},
                                    "quantity": 2,
                                }
                            ],
                        },
                        "tracking": {"orderNumber": "oda-canc", "status": "delivered"},
                    }
                ),
                "ts1": (now - timedelta(days=3)).isoformat(),
                "id2": uuid4(),
                "p2": json.dumps(
                    {
                        "order": {
                            "orderNumber": "oda-canc",
                            "deliveryDate": "2026-11-10",
                            "products": [
                                {
                                    "product": {"id": 9001, "name": "Helmelk 1L", "unitName": "l"},
                                    "quantity": 2,
                                }
                            ],
                        },
                        "tracking": {"orderNumber": "oda-canc", "status": "cancelled"},
                    }
                ),
                "ts2": (now - timedelta(days=2)).isoformat(),
                "id3": uuid4(),
                "p3": json.dumps(
                    {
                        "batchId": "trumf-refund",
                        "transaksjonsTidspunkt": (now - timedelta(days=2)).isoformat(),
                        "belop": -30.0,
                        "hasReceipt": True,
                        "varelinjer": [
                            {"produktBeskrivelse": "Helmelk 1L", "antall": 1, "belop": -30.0}
                        ],
                    }
                ),
                "ts3": (now - timedelta(days=2)).isoformat(),
                "id4": uuid4(),
                "p4": json.dumps(
                    {
                        "receiptId": "rema-partial",
                        "storeName": "REMA 1000",
                        "timestamp": (now - timedelta(days=1)).isoformat(),
                        "hasReceipt": True,
                        "totalAmount": 25.0,
                        "lines": [
                            {
                                "name": "Helmelk 1L",
                                "ean": "7038010000011",
                                "quantity": 2,
                                "amount": 50.0,
                            },
                            {
                                "name": "Helmelk 1L",
                                "ean": "7038010000011",
                                "quantity": -1,
                                "amount": -25.0,
                            },
                        ],
                    }
                ),
                "ts4": (now - timedelta(days=1)).isoformat(),
            },
        )

    repo = PostgresProductOverviewRepository(overview_sqlite_engine)
    service = ProductOverviewService(repository=repo, household_id=hid)
    overview = service.get_product_overview(prod_id)

    # Cancelled Oda order and Trumf refund are excluded; only net 1 from Rema remains
    assert len(overview.purchase_frequency.evidence_history) == 1
    ev = overview.purchase_frequency.evidence_history[0]
    assert ev.evidence_source == "rema1000"
    assert ev.quantity == Decimal("1")
    assert len(overview.previously_purchased_skus) == 1
    assert overview.previously_purchased_skus[0].provider == "rema1000"
    assert overview.previously_purchased_skus[0].purchase_count == 1
