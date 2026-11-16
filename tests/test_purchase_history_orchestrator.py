"""Comprehensive offline unit tests for the unified purchase history sync orchestrator.

Tests coordinate registered provider adapters (Oda, Coop, Trumf, Rema 1000), verify
durable per-provider checkpoints, idempotent retries, overlap deduplication, separate
delay tracking (provider posting delay vs kitchen ingestion delay), coverage windows,
strict separation from kitchen inventory/history tables, and REST/MCP interfaces.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from fixtures.synthetic_kitchen.purchase_history import (
    coop_purchase_list_pages,
    coop_receipt_detail_revisions,
    synthetic_rema1000_receipt_revisions,
    synthetic_trumf_receipt_revisions,
)
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool

from home_kitchen.context import HistoryContext, history_context
from home_kitchen.coop_history import (
    CoopFixtureSource,
    CoopPurchaseHistoryAdapter,
    parse_purchase_list,
    parse_receipt_detail,
)
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.oda.purchase_history import OdaPurchaseHistoryAdapter
from home_kitchen.purchase_sync import (
    SUPPORTED_PROVIDERS,
    ProviderOrderRecord,
    ProviderPage,
    PurchaseHistorySyncService,
    checkpoint,
    metadata,
    provider_order_sources,
)
from home_kitchen.rema1000_history import (
    Rema1000FixtureSource,
    Rema1000PurchaseHistoryAdapter,
    Rema1000Transaction,
)
from home_kitchen.rema1000_history import (
    parse_receipt as parse_rema_receipt,
)
from home_kitchen.trumf_history import (
    TrumfFixtureSource,
    TrumfPurchaseHistoryAdapter,
    TrumfTransaction,
    parse_receipt,
)


@pytest.fixture
def memory_engine():
    """Disposable SQLite database with triggers asserting no household domain writes."""
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        for table in ("kitchen_history", "kitchen_inventory", "inventory_items"):
            connection.execute(text(f"CREATE TABLE {table} (id TEXT)"))
            connection.execute(
                text(
                    f"CREATE TRIGGER reject_{table} BEFORE INSERT ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'sync attempted household write'); END"
                )
            )
    yield engine
    engine.dispose()


@pytest.fixture
def rema_adapter():
    scenarios = synthetic_rema1000_receipt_revisions()
    txs = [
        Rema1000Transaction(
            receipt_id=s.payload["receiptId"],
            store_name=s.payload.get("storeName") or "REMA 1000",
            total_amount=Decimal(str(s.payload.get("totalAmount", "100.00"))),
            timestamp=datetime(2026, 9, 8, 15, 30, 0, tzinfo=UTC),
            has_receipt=s.payload.get("hasReceipt", True),
        )
        for s in scenarios
    ]
    receipts_map = {
        s.payload["receiptId"]: parse_rema_receipt(s.payload)
        for s in scenarios
        if s.payload.get("hasReceipt", True)
    }
    source = Rema1000FixtureSource(transactions=txs, receipts=receipts_map)
    return Rema1000PurchaseHistoryAdapter(source, account_id="household-a")


@pytest.fixture
def trumf_adapter():
    scenarios = synthetic_trumf_receipt_revisions()
    txs = [
        TrumfTransaction(
            batch_id=s.payload["batchId"],
            beskrivelse=s.payload.get("storeName") or "Fiktiv Butikk",
            belop=Decimal(str(s.payload.get("belop", "100.00"))),
            bonus=Decimal("1.00"),
            transaksjons_tidspunkt=datetime(2026, 9, 5, 17, 58, 14, tzinfo=UTC),
            har_kvittering=s.payload.get("hasReceipt", True),
        )
        for s in scenarios
    ]
    receipts_map = {
        s.payload["batchId"]: parse_receipt(s.payload)
        for s in scenarios
        if s.payload.get("hasReceipt", True)
    }
    source = TrumfFixtureSource(transactions=txs, receipts=receipts_map)
    return TrumfPurchaseHistoryAdapter(source, account_id="household-a")


@pytest.fixture
def coop_adapter():
    pages = coop_purchase_list_pages()
    purchases = parse_purchase_list(
        pages[1].response, continuation_observed=pages[1].continuation_observed
    )
    revisions = coop_receipt_detail_revisions()
    receipts_map = {r.external_id: parse_receipt_detail(r.payload) for r in revisions}
    source = CoopFixtureSource(purchases=purchases, receipts=receipts_map)
    return CoopPurchaseHistoryAdapter(source, account_id="household-a")


@pytest.fixture
def oda_adapter():
    fake_service = SimpleNamespace(
        list_orders=AsyncMock(
            return_value={
                "hasMore": False,
                "orders": [
                    {
                        "orderNumber": "synthetic-oda-001",
                        "deliveryDate": "2026-04-10",
                        "currency": "NOK",
                        "grossAmount": 53.5,
                        "products": [
                            {
                                "product": {
                                    "id": 101,
                                    "name": "Fictional carrot ribbons",
                                    "description": "250g",
                                    "price": "18.50",
                                    "unitPrice": "74.00",
                                    "unitName": "kg",
                                },
                                "quantity": 2,
                                "totalGrossAmount": "37.00",
                            }
                        ],
                    }
                ],
            }
        ),
        track_order=AsyncMock(
            return_value={"orderNumber": "synthetic-oda-001", "status": "delivered"}
        ),
    )
    return OdaPurchaseHistoryAdapter(fake_service, account_id="household-a")


def test_supported_providers_constant():
    assert "oda" in SUPPORTED_PROVIDERS
    assert "coop" in SUPPORTED_PROVIDERS
    assert "trumf" in SUPPORTED_PROVIDERS
    assert "rema1000" in SUPPORTED_PROVIDERS


def test_unconfigured_providers_report_honest_status(memory_engine):
    service = PurchaseHistorySyncService(engine=memory_engine)
    status = service.get_status(account_id="household-a")

    assert status.overall_status == "not_configured"
    for p in ("oda", "coop", "trumf", "rema1000"):
        info = status.providers[p]
        assert info.provider == p
        assert info.status == "not_configured"
        assert info.auth_state == "not_configured"
        assert info.last_successful_sync is None
        assert info.last_sync_error is None
        assert info.coverage_window.total_records == 0
        assert info.provider_posting_delay.tracked is False
        assert info.kitchen_ingestion_delay.tracked is False
        assert "not configured" in (info.limitations or "").lower()


def test_sync_unconfigured_provider_skips_cleanly(memory_engine):
    service = PurchaseHistorySyncService(engine=memory_engine)
    result = service.sync_provider("rema1000", account_id="household-a")

    assert result.provider == "rema1000"
    assert result.status == "skipped"
    assert result.records_synced == 0
    assert "not configured" in (result.error or "")


def test_unified_sync_orchestrates_all_four_providers(
    memory_engine, oda_adapter, coop_adapter, trumf_adapter, rema_adapter
):
    service = PurchaseHistorySyncService(
        engine=memory_engine,
        adapters={
            "oda": oda_adapter,
            "coop": coop_adapter,
            "trumf": trumf_adapter,
            "rema1000": rema_adapter,
        },
    )

    # Sync all providers
    response = service.sync(account_id="household-a")
    assert response.total_records_synced > 0
    assert set(response.results.keys()) == {"oda", "coop", "trumf", "rema1000"}

    for res in response.results.values():
        assert res.status == "success"
        assert res.records_synced > 0
        assert res.checkpoint is not None
        assert res.duration_seconds >= 0

    # Verify durable checkpoints in database
    for p in ("oda", "coop", "trumf", "rema1000"):
        cur = checkpoint(memory_engine, p, "household-a")
        assert cur is not None

    # Status reflects connected state, coverage, and last sync
    status = service.get_status(account_id="household-a")
    assert status.overall_status == "connected"

    for p in ("oda", "coop", "trumf", "rema1000"):
        info = status.providers[p]
        assert info.status == "connected"
        assert info.auth_state == "connected"
        assert info.last_successful_sync is not None
        assert info.coverage_window.total_records > 0
        assert info.coverage_window.start is not None
        assert info.kitchen_ingestion_delay.tracked is True

    # Trumf retains 12 months, Rema 1000 retains 24 months
    assert status.providers["trumf"].coverage_window.retained_months == 12
    assert status.providers["rema1000"].coverage_window.retained_months == 24


def test_idempotent_retry_and_overlap_dedupe(memory_engine, rema_adapter, coop_adapter):
    service = PurchaseHistorySyncService(
        engine=memory_engine,
        adapters={"rema1000": rema_adapter, "coop": coop_adapter},
    )

    # First sync
    res1 = service.sync(account_id="household-a", provider="rema1000")
    assert res1.results["rema1000"].status == "success"
    initial_records = res1.results["rema1000"].records_synced
    assert initial_records > 0

    with memory_engine.connect() as connection:
        count_first = connection.execute(
            select(func.count()).where(provider_order_sources.c.provider == "rema1000")
        ).scalar_one()
    assert count_first > 0

    # Re-syncing the exact same source should be idempotent
    res2 = service.sync(account_id="household-a", provider="rema1000")
    assert res2.results["rema1000"].status == "success"

    with memory_engine.connect() as connection:
        count_second = connection.execute(
            select(func.count()).where(provider_order_sources.c.provider == "rema1000")
        ).scalar_one()
    assert count_second == count_first


def test_posting_delay_vs_ingestion_delay_reporting(memory_engine):
    """When source_updated_at is present, provider posting delay is tracked separately."""
    service = PurchaseHistorySyncService(engine=memory_engine)

    tx_time = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    provider_posted = datetime(2026, 4, 1, 12, 5, 0, tzinfo=UTC)  # 5 min posting delay
    kitchen_ingested = datetime(2026, 4, 1, 12, 12, 0, tzinfo=UTC)  # 7 min ingestion delay

    # Ingest record with explicit source_updated_at
    record_with_source_time = ProviderOrderRecord(
        external_id="ext-order-001",
        revision_id="rev-001",
        source_updated_at=provider_posted,
        payload={"purchaseDate": tx_time.isoformat(), "total": 100},
    )

    from home_kitchen.purchase_sync import persist_page

    persist_page(
        memory_engine,
        "rema1000",
        "household-a",
        ProviderPage(records=[record_with_source_time], next_cursor="cur-1", has_more=False),
        expected_cursor=None,
    )

    # Override observed_at to simulate specific delay
    with memory_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE provider_order_sources SET observed_at = :obs "
                "WHERE provider = 'rema1000' AND external_id = 'ext-order-001'"
            ),
            {"obs": kitchen_ingested},
        )

    info = service.get_provider_status("rema1000", account_id="household-a")
    assert info.coverage_window.total_records == 1
    assert info.provider_posting_delay.tracked is True
    # 5 minutes = 300 seconds
    assert info.provider_posting_delay.latest_delay_seconds == 300.0
    assert "5.0 minutes" in info.provider_posting_delay.description

    # Kitchen ingestion delay = 7 minutes = 420 seconds
    assert info.kitchen_ingestion_delay.tracked is True
    assert info.kitchen_ingestion_delay.latest_delay_seconds == 420.0
    assert "7.0 minutes" in info.kitchen_ingestion_delay.description


def test_untracked_posting_delay_when_source_timestamp_is_absent(memory_engine, coop_adapter):
    """Coop does not provide source_updated_at; posting delay is untracked."""
    service = PurchaseHistorySyncService(engine=memory_engine, adapters={"coop": coop_adapter})
    service.sync(account_id="household-a", provider="coop")

    info = service.get_provider_status("coop", account_id="household-a")
    assert info.coverage_window.total_records > 0
    assert info.provider_posting_delay.tracked is False
    assert info.provider_posting_delay.latest_delay_seconds is None
    assert (
        "does not report a separate source posting timestamp"
        in info.provider_posting_delay.description
    )
    assert info.kitchen_ingestion_delay.tracked is True


def test_incomplete_scan_handles_truncation_gracefully(memory_engine):
    """Truncated adapter scan updates checkpoint and reports incomplete status."""

    class TruncatedAdapter:
        def fetch_page(self, account_id, cursor):
            rec = ProviderOrderRecord(
                external_id="part-1",
                revision_id="r1",
                source_updated_at=None,
                payload={"purchaseDate": "2026-04-01T10:00:00Z"},
            )
            return ProviderPage(
                records=[rec],
                next_cursor="cursor-page-2",
                has_more=False,
                incomplete_reason="Truncated at page 1 due to boundary limit",
            )

    service = PurchaseHistorySyncService(
        engine=memory_engine, adapters={"trumf": TruncatedAdapter()}
    )
    result = service.sync_provider("trumf", account_id="household-a")

    assert result.status == "incomplete"
    assert result.records_synced == 1
    assert result.checkpoint == "cursor-page-2"
    assert "Truncated at page 1" in (result.error or "")

    # Status reflects degraded state with error
    status = service.get_status(account_id="household-a")
    assert status.providers["trumf"].status == "degraded"
    assert "Truncated at page 1" in (status.providers["trumf"].last_sync_error or "")


def test_rest_api_status_and_sync_endpoints(memory_engine, rema_adapter, trumf_adapter):
    service = PurchaseHistorySyncService(
        engine=memory_engine,
        adapters={"rema1000": rema_adapter, "trumf": trumf_adapter},
    )

    app = FastAPI()
    features = SimpleNamespace(
        purchase_sync=service,
        catalogue_identities=Mock(),
        household_id="hh-1",
    )
    app.include_router(feature_router(features, basket=Mock()))

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.actor_id = "household-a"
        return await call_next(request)

    client = TestClient(app)

    # GET /api/purchase-history/status
    res = client.get("/api/purchase-history/status")
    assert res.status_code == 200
    data = res.json()
    assert "providers" in data
    assert "rema1000" in data["providers"]
    assert "trumf" in data["providers"]
    assert "oda" in data["providers"]
    assert "coop" in data["providers"]

    # POST /api/purchase-history/sync (specific provider)
    sync_res = client.post("/api/purchase-history/sync", json={"provider": "rema1000"})
    assert sync_res.status_code == 200
    sync_data = sync_res.json()
    assert sync_data["total_records_synced"] > 0
    assert sync_data["results"]["rema1000"]["status"] == "success"

    # POST /api/purchase-history/sync (all providers)
    sync_all_res = client.post("/api/purchase-history/sync", json={})
    assert sync_all_res.status_code == 200
    sync_all_data = sync_all_res.json()
    assert "rema1000" in sync_all_data["results"]
    assert "trumf" in sync_all_data["results"]
    assert "coop" in sync_all_data["results"]
    assert "oda" in sync_all_data["results"]


@pytest.mark.asyncio
async def test_mcp_tools_status_and_sync(memory_engine, rema_adapter):
    service = PurchaseHistorySyncService(
        engine=memory_engine,
        adapters={"rema1000": rema_adapter},
    )

    mcp = FastMCP("Purchase history orchestrator")
    features = SimpleNamespace(
        purchase_sync=service,
        catalogue_identities=Mock(),
        household_id="hh-1",
    )
    install_feature_tools(mcp, features, basket=Mock())

    token = history_context.set(HistoryContext(actor_id="household-a", channel="mcp"))
    try:
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert "get_purchase_history_sync_status" in tools
            assert "sync_purchase_history" in tools
            assert tools["get_purchase_history_sync_status"].annotations.readOnlyHint is True
            assert tools["sync_purchase_history"].annotations.readOnlyHint is False

            # Call get_purchase_history_sync_status
            status_res = await client.call_tool("get_purchase_history_sync_status", {})
            assert status_res is not None
            # FastMCP returns contents
            raw_text = status_res.content[0].text if status_res.content else ""
            assert "rema1000" in raw_text

            # Call sync_purchase_history
            sync_res = await client.call_tool("sync_purchase_history", {"provider": "rema1000"})
            assert sync_res is not None
            sync_text = sync_res.content[0].text if sync_res.content else ""
            assert "success" in sync_text
    finally:
        history_context.reset(token)
