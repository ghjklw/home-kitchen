from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, select, text

from home_kitchen.purchase_sync import (
    CheckpointConflict,
    ProviderOrderRecord,
    ProviderPage,
    PurchaseHistorySyncService,
    RevisionConflict,
    checkpoint,
    metadata,
    persist_page,
    provider_order_sources,
    resolve_effective_orders,
    sync_account,
)


@pytest.fixture
def engine():
    value = create_engine("sqlite://")
    metadata.create_all(value)
    yield value
    value.dispose()


def record(external_id: str, revision: str, payload: dict | None = None):
    return ProviderOrderRecord(
        external_id=external_id,
        revision_id=revision,
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload or {"state": "provider-reported"},
    )


class PagedAdapter:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch_page(self, account_id, cursor):
        self.calls.append((account_id, cursor))
        return self.pages[cursor]


def page(records, cursor, has_more):
    return ProviderPage(records=records, next_cursor=cursor, has_more=has_more)


def count_sources(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(func.count()).select_from(provider_order_sources)
        ).scalar_one()


def test_backfill_overlap_dedupe_and_next_poll_resumes_terminal_cursor(engine):
    backfill = PagedAdapter(
        {
            None: page([record("o1", "r1")], "page-2", True),
            "page-2": page([record("o1", "r1"), record("o2", "r1")], "watermark-2", False),
        }
    )

    assert sync_account(engine, "provider-a", "account-a", backfill) == 3
    assert backfill.calls == [("account-a", None), ("account-a", "page-2")]
    assert checkpoint(engine, "provider-a", "account-a") == "watermark-2"
    assert count_sources(engine) == 2
    with engine.connect() as connection:
        row = connection.execute(select(provider_order_sources)).mappings().first()
        assert row["observed_at"] is not None
        assert row["source_updated_at"] != row["observed_at"]

    incremental = PagedAdapter({"watermark-2": page([record("o3", "r1")], "watermark-3", False)})
    assert sync_account(engine, "provider-a", "account-a", incremental) == 1
    assert incremental.calls == [("account-a", "watermark-2")]
    assert checkpoint(engine, "provider-a", "account-a") == "watermark-3"
    assert count_sources(engine) == 3

    # Same revision replay is immutable and idempotent.
    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([record("o3", "r1")], "watermark-3", False),
        expected_cursor="watermark-3",
    )
    assert count_sources(engine) == 3


def test_missing_provider_timestamp_is_nullable_and_replay_is_idempotent(engine):
    record_without_provider_time = ProviderOrderRecord(
        external_id="coop-summary-1",
        revision_id="sha256:fixture-revision",
        source_updated_at=None,
        payload={"summaryId": "coop-summary-1"},
    )
    first = page([record_without_provider_time], "page-2", True)
    persist_page(engine, "coop", "account-a", first, expected_cursor=None)
    persist_page(
        engine,
        "coop",
        "account-a",
        page([record_without_provider_time], "terminal", False),
        expected_cursor="page-2",
    )

    assert count_sources(engine) == 1
    with engine.connect() as connection:
        row = connection.execute(select(provider_order_sources)).mappings().one()
    assert row["source_updated_at"] is None
    assert checkpoint(engine, "coop", "account-a") == "terminal"


def test_migration_allows_unknown_provider_updated_timestamp(test_database_url):
    engine = create_engine(test_database_url)
    try:
        source_updated_at = next(
            column
            for column in inspect(engine).get_columns("provider_order_sources")
            if column["name"] == "source_updated_at"
        )
        assert source_updated_at["nullable"] is True
    finally:
        engine.dispose()


def test_page_failure_rolls_back_source_and_checkpoint(engine):
    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([], "page-2", True),
        expected_cursor=None,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER reject_record BEFORE INSERT ON provider_order_sources "
                "BEGIN SELECT RAISE(ABORT, 'injected persistence failure'); END"
            )
        )

    with pytest.raises(Exception, match="injected persistence failure"):
        persist_page(
            engine,
            "provider-a",
            "account-a",
            page([record("o1", "r1")], "page-3", True),
            expected_cursor="page-2",
        )

    assert checkpoint(engine, "provider-a", "account-a") == "page-2"
    assert count_sources(engine) == 0


def test_revisions_are_immutable_and_corrections_require_new_revision(engine):
    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([record("o1", "r1")], "c1", True),
        expected_cursor=None,
    )
    with pytest.raises(RevisionConflict, match="new revision_id"):
        persist_page(
            engine,
            "provider-a",
            "account-a",
            page([record("o1", "r1", {"status": "returned"})], "c2", True),
            expected_cursor="c1",
        )
    assert checkpoint(engine, "provider-a", "account-a") == "c1"

    persist_page(
        engine,
        "provider-a",
        "account-a",
        page([record("o1", "r2", {"status": "returned"})], "watermark", False),
        expected_cursor="c1",
    )
    assert count_sources(engine) == 2


def test_provider_account_isolation(engine):
    persist_page(engine, "provider-a", "account-a", page([], "a1", False), expected_cursor=None)
    persist_page(engine, "provider-a", "account-b", page([], "b1", False), expected_cursor=None)
    persist_page(engine, "provider-b", "account-a", page([], "p1", False), expected_cursor=None)

    assert checkpoint(engine, "provider-a", "account-a") == "a1"
    assert checkpoint(engine, "provider-a", "account-b") == "b1"
    assert checkpoint(engine, "provider-b", "account-a") == "p1"


def test_stale_page_cannot_regress_newer_checkpoint(engine):
    persist_page(engine, "provider-a", "account-a", page([], "c1", True), expected_cursor=None)
    stale_page = page([record("o1", "r1")], "stale", True)
    persist_page(engine, "provider-a", "account-a", page([], "c2", True), expected_cursor="c1")

    with pytest.raises(CheckpointConflict):
        persist_page(engine, "provider-a", "account-a", stale_page, expected_cursor="c1")
    assert checkpoint(engine, "provider-a", "account-a") == "c2"
    assert count_sources(engine) == 0


@pytest.mark.parametrize("external_id", ["", "   "])
def test_rejects_malformed_external_identity(external_id):
    with pytest.raises(ValidationError):
        record(external_id, "r1")


def test_rejects_naive_source_time():
    with pytest.raises(ValidationError):
        ProviderOrderRecord(
            external_id="o1",
            revision_id="r1",
            source_updated_at=datetime(2026, 1, 1),
            payload={},
        )


@pytest.mark.parametrize(("provider", "account_id"), [(" ", "acct"), ("provider", "")])
def test_rejects_malformed_provider_account_identity(engine, provider, account_id):
    with pytest.raises(ValueError, match="provider and account_id"):
        persist_page(
            engine,
            provider,
            account_id,
            page([], "cursor", False),
            expected_cursor=None,
        )


def test_same_page_corrections_preserve_order_and_resolve_latest(engine):
    r1 = ProviderOrderRecord(
        external_id="order-1",
        revision_id="rev-1",
        source_updated_at=None,
        payload={
            "order": {
                "orderNumber": "order-1",
                "deliveryDate": "2026-04-10",
                "products": [
                    {"product": {"id": 1, "name": "Melk", "unitName": "l"}, "quantity": 1}
                ],
            },
            "tracking": {"orderNumber": "order-1", "status": "delivered"},
        },
    )
    r2 = ProviderOrderRecord(
        external_id="order-1",
        revision_id="rev-2",
        source_updated_at=None,
        payload={
            "order": {
                "orderNumber": "order-1",
                "deliveryDate": "2026-04-10",
                "products": [
                    {"product": {"id": 1, "name": "Melk", "unitName": "l"}, "quantity": 1}
                ],
            },
            "tracking": {"orderNumber": "order-1", "status": "cancelled"},
        },
    )
    persist_page(engine, "oda", "acct", page([r1, r2], "cur-1", False), expected_cursor=None)

    service = PurchaseHistorySyncService(engine=engine)
    effective = service.get_effective_orders("oda")
    assert len(effective) == 1
    eff = effective[0]
    assert eff.revision_id == "rev-2"
    assert eff.is_correction is True
    assert eff.is_cancellation is True
    assert eff.is_active_purchase is False
    assert eff.lifecycle == "cancellation"
    assert eff.superseded_revision_ids == ["rev-1"]

    status = service.get_provider_status("oda", account_id="acct")
    assert status.coverage_window.total_records == 1
    assert status.coverage_window.correction_count == 1
    assert status.coverage_window.cancellation_count == 1


def test_returns_and_linked_refunds_adjust_active_purchases(engine):
    orig = ProviderOrderRecord(
        external_id="coop-orig-1",
        revision_id="r1",
        payload={
            "summary": {
                "summaryId": "coop-orig-1",
                "receiptId": "coop-rcpt-1",
                "purchaseDate": "2026-04-07T10:00:00Z",
                "amount": 100.0,
                "lines": [
                    {"productName": "Gulrot", "quantity": 2, "amount": 40.0},
                    {"productName": "Brød", "quantity": 1, "amount": 60.0},
                ],
            }
        },
    )
    partial_refund = ProviderOrderRecord(
        external_id="coop-ret-1",
        revision_id="r1",
        payload={
            "summary": {
                "summaryId": "coop-ret-1",
                "receiptId": "coop-rcpt-ret-1",
                "originalReceiptId": "coop-orig-1",
                "purchaseDate": "2026-04-08T10:00:00Z",
                "amount": -40.0,
                "lines": [
                    {"productName": "Gulrot", "quantity": 2, "amount": -40.0},
                ],
            }
        },
    )
    inline_return = ProviderOrderRecord(
        external_id="trumf-inline-ret",
        revision_id="r1",
        payload={
            "batchId": "trumf-inline-ret",
            "transaksjonsTidspunkt": "2026-04-09T12:00:00Z",
            "hasReceipt": True,
            "belop": 25.0,
            "varelinjer": [
                {"produktBeskrivelse": "Yoghurt", "antall": 3, "belop": 75.0},
                {"produktBeskrivelse": "Yoghurt", "antall": -2, "belop": -50.0},
            ],
        },
    )
    persist_page(
        engine,
        "coop",
        "acct",
        page([orig, partial_refund], "c-done", False),
        expected_cursor=None,
    )
    persist_page(
        engine,
        "trumf",
        "acct",
        page([inline_return], "t-done", False),
        expected_cursor=None,
    )

    with engine.connect() as conn:
        rows = conn.execute(select(provider_order_sources)).mappings().all()

    effective = {o.external_id: o for o in resolve_effective_orders(rows)}
    assert effective["coop-ret-1"].is_return is True
    assert effective["coop-ret-1"].is_active_purchase is False

    # Gulrot was fully returned by coop-ret-1; Brød remains active
    assert effective["coop-orig-1"].is_active_purchase is True
    assert [line.name for line in effective["coop-orig-1"].lines] == ["Brød"]
    assert [line.name for line in effective["coop-orig-1"].returned_lines] == ["Gulrot"]

    # Inline return nets 3 - 2 = 1 Yoghurt
    assert effective["trumf-inline-ret"].is_active_purchase is True
    assert len(effective["trumf-inline-ret"].lines) == 1
    assert effective["trumf-inline-ret"].lines[0].quantity == 1

    service = PurchaseHistorySyncService(engine=engine)
    coop_status = service.get_provider_status("coop", account_id="acct")
    assert coop_status.coverage_window.total_records == 2
    assert coop_status.coverage_window.return_count == 1
