"""Offline unit tests for Product Overview, translations, stock, frequency, and Oda preferences."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

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

from home_kitchen.basket.matching import BasketCandidate
from home_kitchen.catalogue.identity import CanonicalProduct
from home_kitchen.catalogue.overview_models import (
    ExcludedOdaSkuInput,
    PreferredOdaSkuInput,
    ProductPreferencesUpdate,
    PurchaseEvidenceRecord,
)
from home_kitchen.catalogue.overview_repository import MemoryProductOverviewRepository
from home_kitchen.catalogue.overview_service import (
    ProductOverviewService,
    compute_purchase_frequency,
)
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools


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
