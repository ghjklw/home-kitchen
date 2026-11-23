"""Comprehensive offline unit tests for the unified purchase history sync orchestrator.

Tests coordinate registered provider adapters (Oda, Coop, Trumf, Rema 1000), verify
durable per-provider checkpoints, idempotent retries, overlap deduplication, separate
delay tracking (provider posting delay vs kitchen ingestion delay), coverage windows,
strict separation from kitchen inventory/history tables, and REST/MCP interfaces.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
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

from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.connector_store import ConnectorStore
from home_kitchen.connector_store import metadata as connector_metadata
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

pytestmark = pytest.mark.mocked_provider


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


@pytest.fixture(autouse=True)
def _no_kassal_enrichment(monkeypatch):
    """These fixtures predate the Kassal requirement; barcode enrichment is tested separately."""
    monkeypatch.setattr("home_kitchen.purchase_sync.extract_line_ean", lambda line: None)


@pytest.mark.asyncio
async def test_sync_forever_executes_bounded_ticks_isolates_errors_and_cancels_cleanly(
    memory_engine, caplog
):
    service = PurchaseHistorySyncService(engine=memory_engine)
    seen_calls: list[tuple[str, int]] = []
    second_tick_done = asyncio.Event()

    class _RecordingAdapter:
        def fetch_page(self, account_id, cursor):
            return ProviderPage(
                records=[
                    ProviderOrderRecord(
                        external_id="bg-order-1",
                        revision_id="rev-1",
                        source_updated_at=datetime(2026, 4, 2, 10, 0, 0, tzinfo=UTC),
                        payload={"purchaseDate": "2026-04-02T10:00:00Z"},
                    )
                ],
                next_cursor="bg-cur-1",
                has_more=False,
            )

    service.register_adapter("rema1000", _RecordingAdapter(), account_id="household-a")
    real_sync = service.sync

    def flaky_sync(account_id: str = "default", provider: str | None = None, max_pages: int = 50):
        seen_calls.append((account_id, max_pages))
        if len(seen_calls) == 1:
            raise RuntimeError("transient network failure with secret-dsn")
        result = real_sync(account_id=account_id, provider=provider, max_pages=max_pages)
        second_tick_done.set()
        return result

    service.sync = flaky_sync  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="positive"):
        await service.sync_forever(interval_seconds=0)

    task = asyncio.create_task(
        service.sync_forever(interval_seconds=0.01, account_id="household-a", max_pages=12)
    )
    try:
        await asyncio.wait_for(second_tick_done.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)

    assert len(seen_calls) >= 2
    assert seen_calls[0] == ("household-a", 12)
    assert checkpoint(memory_engine, "rema1000", "household-a") == "bg-cur-1"
    assert "secret-dsn" not in caplog.text
    assert "will retry" in caplog.text


@pytest.mark.asyncio
async def test_sync_forever_cancellation_waits_for_inflight_thread(memory_engine):
    service = PurchaseHistorySyncService(engine=memory_engine)
    started, release = Event(), Event()

    def slow_sync(*, account_id: str = "default", max_pages: int = 50):
        started.set()
        assert release.wait(2.0)
        raise RuntimeError("cancelled-during-shutdown")

    service.sync = slow_sync  # type: ignore[method-assign]
    task = asyncio.create_task(service.sync_forever(interval_seconds=300.0))
    try:
        assert await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        release.set()
        if not task.done():
            task.cancel()


def test_app_lifespan_starts_and_cancels_purchase_sync_when_not_test_env(monkeypatch):
    from home_kitchen.app import create_app
    from home_kitchen.service import KitchenService
    from home_kitchen.settings import Settings

    class MemoryTestStore:
        def __init__(self):
            self.settings: dict[str, str] = {}
            self.photo_household_id = None
            self.engine = create_engine(
                "sqlite://",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
            connector_metadata.create_all(self.engine)
            metadata.create_all(self.engine)

        def get_setting(self, key: str) -> str | None:
            return self.settings.get(key)

        def set_setting(self, key: str, value: str) -> None:
            self.settings[key] = value

        def ensure_setting(self, key: str, value: str) -> str:
            return self.settings.setdefault(key, value)

        def list(self, kind: str, model: type) -> list:
            return []

    store = MemoryTestStore()
    kitchen = KitchenService(store=store)
    kitchen.identities = Mock(spec=CanonicalIdentityService)
    settings = Settings(
        local_dev=True,
        entra_allowed_group=uuid4(),
        database_url="postgresql+psycopg://user:pass@localhost/db",
        purchase_sync_interval_seconds=120.0,
    )

    started = Event()
    cancelled = Event()
    observed_intervals: list[float] = []

    async def fake_sync_forever(
        self, interval_seconds: float = 300.0, *, account_id: str = "default", max_pages: int = 50
    ) -> None:
        observed_intervals.append(interval_seconds)
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fake_refresh_forever(self, interval: float = 300.0) -> None:
        await asyncio.sleep(60)

    async def fake_photo_cleanup_forever(self, interval: float = 3600.0) -> None:
        await asyncio.sleep(60)

    async def fake_cookidoo_sync_forever(
        self, interval_seconds: float = 300.0, *, actor_id: str = "system"
    ) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr("home_kitchen.app._is_test_env", lambda: False)
    monkeypatch.setattr(
        "home_kitchen.recipes.photo_storage.PhotoRecipeService.cleanup_forever",
        fake_photo_cleanup_forever,
    )
    monkeypatch.setattr(PurchaseHistorySyncService, "sync_forever", fake_sync_forever)
    monkeypatch.setattr(
        "home_kitchen.cookidoo_plan_sync.CookidooPlanSyncService.sync_forever",
        fake_cookidoo_sync_forever,
    )
    monkeypatch.setattr(
        "home_kitchen.app.ConnectorAuthService.refresh_forever", fake_refresh_forever
    )

    app = create_app(settings, service=kitchen)
    with TestClient(app, client=("127.0.0.1", 5000)):
        assert started.wait(2.0)

    assert cancelled.wait(2.0)
    assert observed_intervals == [120.0]


def test_app_lifespan_skips_purchase_sync_in_test_env(monkeypatch):
    from home_kitchen.app import create_app
    from home_kitchen.service import KitchenService
    from home_kitchen.settings import Settings

    class MemoryTestStore:
        def __init__(self):
            self.settings: dict[str, str] = {}
            self.photo_household_id = None
            self.engine = create_engine(
                "sqlite://",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
            connector_metadata.create_all(self.engine)
            metadata.create_all(self.engine)

        def get_setting(self, key: str) -> str | None:
            return self.settings.get(key)

        def set_setting(self, key: str, value: str) -> None:
            self.settings[key] = value

        def ensure_setting(self, key: str, value: str) -> str:
            return self.settings.setdefault(key, value)

        def list(self, kind: str, model: type) -> list:
            return []

    store = MemoryTestStore()
    kitchen = KitchenService(store=store)
    kitchen.identities = Mock(spec=CanonicalIdentityService)
    settings = Settings(
        local_dev=True,
        entra_allowed_group=uuid4(),
        database_url="postgresql+psycopg://user:pass@localhost/db",
    )

    sync_forever_spy = AsyncMock()
    monkeypatch.setattr(PurchaseHistorySyncService, "sync_forever", sync_forever_spy)

    app = create_app(settings, service=kitchen)
    with TestClient(app, client=("127.0.0.1", 5000)):
        pass

    sync_forever_spy.assert_not_called()


def test_provider_status_reports_connector_store_auth_mode_accurately(memory_engine):
    connector_metadata.create_all(memory_engine)
    store = ConnectorStore(memory_engine, Fernet(Fernet.generate_key()))
    household_id = uuid4()

    def build_adapter(provider: str, account_id: str, creds: dict):
        if provider == "rema1000":
            return Rema1000PurchaseHistoryAdapter(Rema1000FixtureSource([]), account_id=account_id)
        if provider == "coop":
            return CoopPurchaseHistoryAdapter(
                CoopFixtureSource(purchases=[], receipts={}), account_id=account_id
            )
        if provider == "trumf":
            return TrumfPurchaseHistoryAdapter(TrumfFixtureSource([]), account_id=account_id)
        return None

    service = PurchaseHistorySyncService(
        engine=memory_engine,
        connector_store=store,
        household_id=household_id,
        adapter_factory=build_adapter,
    )

    # Unconfigured defaults retain legacy manual/provider fallback descriptions
    assert service._default_provider_metadata("trumf")[0] == "manual_session_cookie"
    assert service._default_provider_metadata("rema1000")[0] == "manual_session_token"
    assert service._default_provider_metadata("coop")[0] == "oidc_pkce"

    # Connected via oauth_pkce for Rema 1000 and Coop, and session_cookie for Trumf
    store.save_connection(
        household_id=household_id,
        provider_id="trumf",
        auth_mode="session_cookie",
        credentials={"session_cookie": "cookie=abc"},
        actor_id="household-a",
        account_id="household-a",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="oauth_pkce",
        credentials={"access_token": "rema-pkce-token", "refresh_token": "rema-ref"},
        actor_id="household-a",
        account_id="household-a",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="coop",
        auth_mode="oauth_pkce",
        credentials={"access_token": "coop-pkce-token", "refresh_token": "coop-ref"},
        actor_id="household-a",
        account_id="household-a",
    )

    assert (
        service._default_provider_metadata("trumf", account_id="household-a")[0] == "session_cookie"
    )
    assert (
        service._default_provider_metadata("rema1000", account_id="household-a")[0] == "oauth_pkce"
    )
    assert service._default_provider_metadata("coop", account_id="household-a")[0] == "oauth_pkce"

    status_pkce = service.get_status(account_id="household-a")
    assert status_pkce.providers["trumf"].auth_method == "session_cookie"
    assert status_pkce.providers["trumf"].status == "connected"
    assert "manual" not in (status_pkce.providers["trumf"].limitations or "").lower()
    assert status_pkce.providers["rema1000"].auth_method == "oauth_pkce"
    assert status_pkce.providers["rema1000"].status == "connected"
    assert "OAuth PKCE" in (status_pkce.providers["rema1000"].limitations or "")
    assert status_pkce.providers["coop"].auth_method == "oauth_pkce"
    assert status_pkce.providers["coop"].status == "connected"

    # Switching Rema 1000 to session_token updates metadata accurately
    store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": "rema-session-tok"},
        actor_id="household-a",
        account_id="household-a",
    )

    assert (
        service._default_provider_metadata("rema1000", account_id="household-a")[0]
        == "session_token"
    )

    status_session = service.get_status(account_id="household-a")
    assert status_session.providers["rema1000"].auth_method == "session_token"
    assert "manual" not in (status_session.providers["rema1000"].limitations or "").lower()


def test_sync_proactively_refreshes_expiring_coop_and_rema_tokens_before_fetch(memory_engine):
    from home_kitchen.connector_auth import ConnectorAuthService
    from home_kitchen.integrations import (
        CoopIntegration,
        IntegrationRegistry,
        Rema1000Integration,
        TrumfIntegration,
    )

    connector_metadata.create_all(memory_engine)
    store = ConnectorStore(memory_engine, Fernet(Fernet.generate_key()))
    household_id = uuid4()
    seen_tokens: list[tuple[str, str]] = []

    store.save_connection(
        household_id=household_id,
        provider_id="coop",
        auth_mode="oauth_pkce",
        credentials={"access_token": "old-coop", "refresh_token": "coop-ref-1"},
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        actor_id="household-a",
        account_id="household-a",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="oauth_pkce",
        credentials={"access_token": "old-rema", "refresh_token": "rema-ref-1"},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        actor_id="household-a",
        account_id="household-a",
    )

    def auth_handler(request: httpx.Request) -> httpx.Response:
        if "coop.no" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-coop",
                    "refresh_token": "coop-ref-2",
                    "expires_in": 3600,
                },
            )
        if "rema.no" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "access_token": "fresh-rema",
                    "refresh_token": "rema-ref-2",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    registry = IntegrationRegistry(
        [
            Rema1000Integration(store, household_id),
            CoopIntegration(store, household_id),
            TrumfIntegration(store, household_id),
        ]
    )
    auth = ConnectorAuthService(
        store=store,
        integrations=registry,
        household_id=household_id,
        transport=httpx.MockTransport(auth_handler),
    )

    def build_adapter(provider: str, account_id: str, creds: dict):
        token = creds.get("access_token", "")
        seen_tokens.append((provider, token))
        if provider == "coop":
            return CoopPurchaseHistoryAdapter(
                CoopFixtureSource(purchases=[], receipts={}), account_id=account_id
            )
        if provider == "rema1000":
            return Rema1000PurchaseHistoryAdapter(Rema1000FixtureSource([]), account_id=account_id)
        return None

    service = PurchaseHistorySyncService(
        engine=memory_engine,
        connector_store=store,
        household_id=household_id,
        adapter_factory=build_adapter,
        connector_auth=auth,
    )

    res = service.sync(account_id="household-a")
    assert res.results["coop"].status == "success"
    assert res.results["rema1000"].status == "success"
    assert ("coop", "fresh-coop") in seen_tokens
    assert ("rema1000", "fresh-rema") in seen_tokens

    status = service.get_status(account_id="household-a")
    assert status.providers["coop"].auth_state == "connected"
    assert status.providers["coop"].status == "connected"
    assert status.providers["rema1000"].auth_state == "connected"
    assert status.providers["rema1000"].status == "connected"


def test_sync_retries_once_after_auth_error_for_coop_rema_and_trumf(memory_engine):
    from home_kitchen.connector_auth import ConnectorAuthService
    from home_kitchen.coop_history import CoopAuthenticationError
    from home_kitchen.integrations import (
        CoopIntegration,
        IntegrationRegistry,
        Rema1000Integration,
        TrumfIntegration,
    )
    from home_kitchen.rema1000_history import Rema1000AuthenticationError
    from home_kitchen.trumf_history import TrumfAuthenticationError

    connector_metadata.create_all(memory_engine)
    store = ConnectorStore(memory_engine, Fernet(Fernet.generate_key()))
    household_id = uuid4()

    future = datetime.now(UTC) + timedelta(hours=2)
    store.save_connection(
        household_id=household_id,
        provider_id="coop",
        auth_mode="oauth_pkce",
        credentials={"access_token": "stale-coop", "refresh_token": "coop-ref"},
        expires_at=future,
        actor_id="household-a",
        account_id="household-a",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="oauth_pkce",
        credentials={"access_token": "stale-rema", "refresh_token": "rema-ref"},
        expires_at=future,
        actor_id="household-a",
        account_id="household-a",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="trumf",
        auth_mode="session_cookie",
        credentials={"session_cookie": "__Secure-authjs.session-token=stale"},
        expires_at=future,
        actor_id="household-a",
        account_id="household-a",
    )

    def auth_handler(request: httpx.Request) -> httpx.Response:
        if "coop.no" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "access_token": "renewed-coop",
                    "refresh_token": "coop-ref-2",
                    "expires_in": 3600,
                },
            )
        if "rema.no" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "access_token": "renewed-rema",
                    "refresh_token": "rema-ref-2",
                    "expires_in": 3600,
                },
            )
        if "trumf.no" in str(request.url) and request.url.path == "/api/auth/session":
            return httpx.Response(
                200,
                json={"user": {"name": "Malo"}, "expires": "2026-12-31T00:00:00Z"},
                headers={"Set-Cookie": "__Secure-authjs.session-token=renewed-cookie; Path=/"},
            )
        return httpx.Response(404)

    registry = IntegrationRegistry(
        [
            Rema1000Integration(store, household_id),
            CoopIntegration(store, household_id),
            TrumfIntegration(store, household_id),
        ]
    )
    auth = ConnectorAuthService(
        store=store,
        integrations=registry,
        household_id=household_id,
        transport=httpx.MockTransport(auth_handler),
    )

    def build_adapter(provider: str, account_id: str, creds: dict):
        if provider == "coop":
            if creds.get("access_token") == "stale-coop":
                return CoopPurchaseHistoryAdapter(
                    CoopFixtureSource([], failure=CoopAuthenticationError("expired")),
                    account_id=account_id,
                )
            return CoopPurchaseHistoryAdapter(CoopFixtureSource([]), account_id=account_id)
        if provider == "rema1000":
            if creds.get("access_token") == "stale-rema":
                return Rema1000PurchaseHistoryAdapter(
                    Rema1000FixtureSource([], failure=Rema1000AuthenticationError("expired")),
                    account_id=account_id,
                )
            return Rema1000PurchaseHistoryAdapter(Rema1000FixtureSource([]), account_id=account_id)
        if provider == "trumf":
            if "stale" in creds.get("session_cookie", ""):
                return TrumfPurchaseHistoryAdapter(
                    TrumfFixtureSource([], failure=TrumfAuthenticationError("expired")),
                    account_id=account_id,
                )
            return TrumfPurchaseHistoryAdapter(TrumfFixtureSource([]), account_id=account_id)
        return None

    service = PurchaseHistorySyncService(
        engine=memory_engine,
        connector_store=store,
        household_id=household_id,
        adapter_factory=build_adapter,
        connector_auth=auth,
    )
    res = service.sync(account_id="household-a")
    assert res.results["coop"].status == "success"
    assert res.results["rema1000"].status == "success"
    assert res.results["trumf"].status == "success"


def test_sync_records_expired_auth_state_and_sanitizes_errors_across_all_providers(memory_engine):
    from home_kitchen.connector_auth import ConnectorAuthService
    from home_kitchen.integrations import (
        CoopIntegration,
        IntegrationRegistry,
        Rema1000Integration,
        TrumfIntegration,
    )
    from home_kitchen.oda.service import OdaError

    connector_metadata.create_all(memory_engine)
    store = ConnectorStore(memory_engine, Fernet(Fernet.generate_key()))
    household_id = uuid4()

    expired = datetime.now(UTC) - timedelta(hours=1)
    store.save_connection(
        household_id=household_id,
        provider_id="coop",
        auth_mode="oauth_pkce",
        credentials={"access_token": "secret-coop-acc", "refresh_token": "secret-coop-ref"},
        expires_at=expired,
        actor_id="household-a",
        account_id="acc-coop-bad",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="oauth_pkce",
        credentials={"access_token": "secret-rema-acc", "refresh_token": "secret-rema-ref"},
        expires_at=expired,
        actor_id="household-a",
        account_id="acc-rema-bad",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="rema1000",
        auth_mode="session_token",
        credentials={"session_token": "good-rema-session"},
        expires_at=datetime.now(UTC) + timedelta(hours=5),
        actor_id="household-a",
        account_id="acc-rema-good",
    )
    store.save_connection(
        household_id=household_id,
        provider_id="trumf",
        auth_mode="session_cookie",
        credentials={"session_cookie": "secret-trumf-cookie"},
        expires_at=expired,
        actor_id="household-a",
        account_id="acc-trumf-bad",
    )

    def failing_auth(request: httpx.Request) -> httpx.Response:
        if "trumf.no" in str(request.url):
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(400, json={"error": "invalid_grant"})

    registry = IntegrationRegistry(
        [
            Rema1000Integration(store, household_id),
            CoopIntegration(store, household_id),
            TrumfIntegration(store, household_id),
        ]
    )
    auth = ConnectorAuthService(
        store=store,
        integrations=registry,
        household_id=household_id,
        transport=httpx.MockTransport(failing_auth),
    )

    def build_adapter(provider: str, account_id: str, creds: dict):
        if provider == "coop":
            return CoopPurchaseHistoryAdapter(CoopFixtureSource([]), account_id=account_id)
        if provider == "rema1000":
            return Rema1000PurchaseHistoryAdapter(Rema1000FixtureSource([]), account_id=account_id)
        if provider == "trumf":
            return TrumfPurchaseHistoryAdapter(TrumfFixtureSource([]), account_id=account_id)
        return None

    fake_oda = SimpleNamespace(
        configured=True,
        repository=SimpleNamespace(
            has_connection=lambda acc: acc == "household-a",
            list_connected_actors=lambda: ["household-a"],
        ),
        list_orders=AsyncMock(
            side_effect=OdaError("Your Oda connection expired; connect again", 409)
        ),
        track_order=AsyncMock(),
    )

    service = PurchaseHistorySyncService(
        engine=memory_engine,
        oda_service=fake_oda,
        connector_store=store,
        household_id=household_id,
        adapter_factory=build_adapter,
        connector_auth=auth,
    )

    res = service.sync(account_id="household-a")
    assert res.results["coop"].status == "failed"
    assert res.results["trumf"].status == "failed"
    assert res.results["oda"].status == "failed"
    # Multi-account partial failure: acc-rema-good succeeded while acc-rema-bad failed refresh
    assert res.results["rema1000"].status == "incomplete"
    assert res.results["rema1000"].accounts_synced == 1

    for secret in (
        "secret-coop-acc",
        "secret-coop-ref",
        "secret-rema-acc",
        "secret-rema-ref",
        "secret-trumf-cookie",
    ):
        assert secret not in str(res.model_dump())

    status = service.get_status(account_id="household-a")
    assert status.providers["coop"].auth_state == "expired"
    assert status.providers["coop"].status == "unavailable"
    assert status.providers["trumf"].auth_state == "expired"
    assert status.providers["trumf"].status == "unavailable"
    assert status.providers["oda"].auth_state == "expired"
    assert status.providers["oda"].status == "unavailable"
    assert status.providers["rema1000"].auth_state == "expired"
    assert status.providers["rema1000"].status == "degraded"

    coop_meta = store.get_connection_metadata(household_id, "coop", account_id="acc-coop-bad")
    assert coop_meta is not None
    assert coop_meta.metadata.get("last_error")
