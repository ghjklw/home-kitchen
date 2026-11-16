"""Unit tests for the read-only Trumf receipt and purchase history adapter.

All tests use synthetic Norwegian grocery fixtures and run completely offline
without calling live endpoints, loading credentials, or writing to kitchen domain tables.
"""

from decimal import Decimal
from typing import Any, cast

import pytest
from fixtures.synthetic_kitchen.purchase_history import (
    synthetic_trumf_receipt_revisions,
    synthetic_trumf_rsc_list_html,
    synthetic_trumf_rsc_receipt_html,
    synthetic_trumf_rsc_unauthenticated_html,
    trumf_transaction_pages,
)
from sqlalchemy import create_engine, func, select, text

from home_kitchen.purchase_sync import (
    IncompleteScanError,
    checkpoint,
    metadata,
    provider_order_sources,
    sync_account,
)
from home_kitchen.trumf_history import (
    TRUMF_AUTH_METHOD,
    TRUMF_HISTORY_WINDOW_MONTHS,
    TRUMF_LIMITATIONS,
    TRUMF_SCHEMA_SOURCE,
    TRUMF_SCHEMA_VERSION,
    TrumfAuthenticationError,
    TrumfFixtureSource,
    TrumfPurchaseHistoryAdapter,
    TrumfReceipt,
    TrumfTransaction,
    UnsupportedTrumfContract,
    _decode_cursor,
    _encode_cursor,
    _TrumfScanCursor,
    build_transaction_receipt_stub,
    chain_of,
    parse_receipt,
    parse_transactions,
    to_provider_order_record,
)


@pytest.fixture
def memory_engine():
    """Disposable SQLite database with triggers asserting no household domain writes."""
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as connection:
        for table in ("kitchen_history", "kitchen_inventory"):
            connection.execute(text(f"CREATE TABLE {table} (id TEXT)"))
            connection.execute(
                text(
                    f"CREATE TRIGGER reject_{table} BEFORE INSERT ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'source capture attempted household write'); END"
                )
            )
    yield engine
    engine.dispose()


def test_parse_transactions_from_rsc_flight_html():
    html = synthetic_trumf_rsc_list_html()
    transactions = parse_transactions(html)

    assert len(transactions) == 2

    first = transactions[0]
    assert first.batch_id == "synthetic-trumf-b-100"
    assert first.beskrivelse == "KIWI Fiktiv Testbutikk"
    assert first.belop == Decimal("268.41")
    assert first.bonus == Decimal("2.67")
    assert first.filter_category == "KIWI"
    assert first.partner_id == "KIWI"
    assert first.har_kvittering is True
    assert chain_of(first) == "KIWI"
    assert first.transaksjons_tidspunkt.year == 2026
    assert first.transaksjons_tidspunkt.month == 9
    assert first.transaksjons_tidspunkt.day == 5

    second = transactions[1]
    assert second.batch_id == "synthetic-trumf-b-101"
    assert second.beskrivelse == "MENY Fiktiv Testby"
    assert second.belop == Decimal("99.00")
    assert second.bonus == Decimal("1.00")
    assert second.filter_category == "MENY"
    assert second.har_kvittering is False
    assert chain_of(second) == "MENY"


def test_parse_transactions_unauthenticated_html_fails_closed():
    unauthenticated = synthetic_trumf_rsc_unauthenticated_html()
    with pytest.raises(TrumfAuthenticationError, match="no transaction data found"):
        parse_transactions(unauthenticated)

    with pytest.raises(TrumfAuthenticationError, match="no transaction data found"):
        parse_transactions("<html><body>Logg inn med BankID</body></html>")


def test_parse_receipt_detail_from_rsc_flight_html():
    html = synthetic_trumf_rsc_receipt_html()
    receipt = parse_receipt(html)

    assert receipt.batch_id == "synthetic-trumf-b-100"
    assert receipt.kvitterings_id == "syn-rec-224383"
    assert receipt.belop == Decimal("268.41")
    assert receipt.bonus == Decimal("2.67")
    assert receipt.has_receipt is True
    assert receipt.source_uri == TRUMF_SCHEMA_SOURCE
    assert receipt.source_schema_version == TRUMF_SCHEMA_VERSION
    assert receipt.source_updated_at is None
    assert receipt.revision_id.startswith("sha256:")

    assert len(receipt.varelinjer) == 2

    milk = receipt.varelinjer[0]
    assert milk.produkt_beskrivelse == "FIKTIV HELMELK 1L"
    assert milk.ean is None  # "$undefined" converted to None
    assert milk.antall == Decimal("2")
    assert milk.belop == Decimal("49.80")
    assert milk.enhets_type == "EA"
    assert milk.moms_prosent == Decimal("15")
    assert milk.besparelser == []

    carrots = receipt.varelinjer[1]
    assert carrots.produkt_beskrivelse == "FIKTIV GULROTBÅND / CARROT RIBBONS PR KG"
    assert carrots.ean is None
    assert carrots.antall == Decimal("0.215")
    assert carrots.belop == Decimal("27.74")
    assert carrots.enhets_type == "KG"
    assert carrots.moms_prosent == Decimal("15")
    assert len(carrots.besparelser) == 1
    assert carrots.besparelser[0].type == "OFFER"
    assert carrots.besparelser[0].beskrivelse == "UKE 36"
    assert carrots.besparelser[0].belop == Decimal("10.75")


def test_parse_receipt_unauthenticated_html_fails_closed():
    unauthenticated = synthetic_trumf_rsc_unauthenticated_html()
    with pytest.raises(TrumfAuthenticationError, match="no receipt details found"):
        parse_receipt(unauthenticated)


def test_parse_synthetic_dict_format_compatibility():
    pages = trumf_transaction_pages()
    tx_page = cast(list[dict[str, Any]], pages[0].response)
    transactions = parse_transactions(tx_page)

    assert len(transactions) == 1
    assert transactions[0].batch_id == "synthetic-trumf-batch-001"
    assert transactions[0].belop == Decimal("92.80")
    assert transactions[0].bonus == Decimal("1.35")
    assert chain_of(transactions[0]) == "synthetic-chain-01"

    detail_page = cast(dict[str, Any], pages[2].response)
    receipt = parse_receipt(detail_page, transaction=transactions[0])

    assert receipt.batch_id == "synthetic-trumf-batch-001"
    assert receipt.store_name == "Synthetic pantry collection"
    assert receipt.chain == "synthetic-chain-01"
    assert len(receipt.varelinjer) == 2
    assert receipt.varelinjer[0].produkt_beskrivelse == "Fictional gulrotbånd / carrot ribbons"
    assert receipt.varelinjer[0].ean == "SYNTHETIC-TRUMF-001"
    assert receipt.varelinjer[0].antall == Decimal("2")
    assert receipt.varelinjer[1].antall is None  # loose unquantified item preserved as None


def test_revision_id_is_stable_and_detects_corrections():
    revisions = synthetic_trumf_receipt_revisions()
    base_raw = revisions[0].payload
    corrected_raw = revisions[1].payload

    base_1 = parse_receipt(base_raw)
    base_2 = parse_receipt(base_raw)
    corrected = parse_receipt(corrected_raw)

    assert base_1.revision_id == base_2.revision_id
    assert corrected.revision_id != base_1.revision_id
    assert corrected.batch_id == base_1.batch_id
    assert corrected.belop == Decimal("250.00")


def test_build_transaction_receipt_stub_for_non_receipt_transaction():
    tx = TrumfTransaction.model_validate(
        {
            "batchId": "synthetic-trumf-b-101",
            "beskrivelse": "MENY Fiktiv Testby",
            "belop": "99.00",
            "bonus": "1.00",
            "transaksjonsTidspunkt": "2026-08-01T10:00:00.000Z",
            "filterCategory": "MENY",
            "harKvittering": False,
        }
    )
    stub = build_transaction_receipt_stub(tx)

    assert stub.batch_id == "synthetic-trumf-b-101"
    assert stub.store_name == "MENY Fiktiv Testby"
    assert stub.chain == "MENY"
    assert stub.belop == Decimal("99.00")
    assert stub.bonus == Decimal("1.00")
    assert stub.has_receipt is False
    assert stub.varelinjer == []
    assert stub.revision_id.startswith("sha256:")

    record = to_provider_order_record(stub)
    assert record.external_id == "synthetic-trumf-b-101"
    assert record.payload["hasReceipt"] is False
    assert record.payload["varelinjer"] == []


def test_adapter_status_documents_limits():
    source = TrumfFixtureSource([])
    adapter = TrumfPurchaseHistoryAdapter(source, "alice")
    status = adapter.status()

    assert status.provider == "trumf"
    assert status.history_window_months == TRUMF_HISTORY_WINDOW_MONTHS == 12
    assert status.auth_method == TRUMF_AUTH_METHOD == "manual_session_cookie"
    assert status.read_only is True
    assert status.live_calls_enabled is False
    assert status.limitations == TRUMF_LIMITATIONS
    assert "12 months" in status.limitations
    assert "session cookie" in status.limitations


def test_cursor_encoding_decoding_and_validation():
    cursor = _TrumfScanCursor(
        version=1,
        mode="full_rescan",
        account_id="alice",
        scan_id="00000000-0000-0000-0000-000000000001",
        next_page=2,
        state="scanning",
    )
    encoded = _encode_cursor(cursor)
    assert encoded.startswith("trumf-scan-v1.")

    decoded = _decode_cursor(encoded)
    assert decoded == cursor

    with pytest.raises(ValueError, match="Invalid Trumf history scan cursor"):
        _decode_cursor("")
    with pytest.raises(ValueError, match="Invalid Trumf history scan cursor"):
        _decode_cursor("invalid-prefix.abc")
    with pytest.raises(ValueError, match="Invalid Trumf history scan cursor"):
        _decode_cursor("trumf-scan-v1.not-base64!!!")


def test_adapter_bounded_pagination_and_truncation():
    txs = [
        TrumfTransaction.model_validate(
            {
                "batchId": f"batch-{i:03d}",
                "beskrivelse": "KIWI Test",
                "belop": "10.00",
                "transaksjonsTidspunkt": "2026-09-01T10:00:00.000Z",
                "harKvittering": False,
            }
        )
        for i in range(5)
    ]
    source = TrumfFixtureSource(txs)
    # Page size 2, max pages 2 (can fetch at most 4 items before truncation)
    adapter = TrumfPurchaseHistoryAdapter(source, "alice", page_size=2, max_pages=2)

    # Page 1
    p1 = adapter.fetch_page("alice", None)
    assert len(p1.records) == 2
    assert p1.has_more is True
    assert p1.incomplete_reason is None

    # Page 2 (reaches max_pages=2 with 1 item remaining)
    p2 = adapter.fetch_page("alice", p1.next_cursor)
    assert len(p2.records) == 2
    assert p2.has_more is False
    assert p2.incomplete_reason is not None
    assert "truncated at page 2" in p2.incomplete_reason

    # Terminal cursor resets to page 1 for next poll
    decoded = _decode_cursor(p2.next_cursor)
    assert decoded.next_page == 1
    assert decoded.state == "incomplete"


def test_adapter_account_mismatch_fails_closed():
    source = TrumfFixtureSource([])
    adapter = TrumfPurchaseHistoryAdapter(source, "alice")

    with pytest.raises(ValueError, match="account mismatch"):
        adapter.fetch_page("bob", None)

    foreign_cursor = _encode_cursor(
        _TrumfScanCursor(
            version=1,
            mode="full_rescan",
            account_id="bob",
            scan_id="00000000-0000-0000-0000-000000000001",
            next_page=1,
            state="scanning",
        )
    )
    with pytest.raises(ValueError, match="account mismatch"):
        adapter.fetch_page("alice", foreign_cursor)


def test_sync_account_idempotent_deduplication_and_no_household_writes(memory_engine):
    list_html = synthetic_trumf_rsc_list_html()
    receipt_html = synthetic_trumf_rsc_receipt_html()

    source = TrumfFixtureSource(
        list_html,
        receipts={"synthetic-trumf-b-100": receipt_html},
    )
    adapter = TrumfPurchaseHistoryAdapter(source, "alice")

    # Initial sync
    imported_count = sync_account(memory_engine, "trumf", "alice", adapter)
    assert imported_count == 2

    with memory_engine.connect() as connection:
        # Check provider_order_sources has 2 records
        rows = (
            connection.execute(
                select(provider_order_sources).where(provider_order_sources.c.provider == "trumf")
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2

        by_id = {row["external_id"]: row for row in rows}
        assert "synthetic-trumf-b-100" in by_id
        assert "synthetic-trumf-b-101" in by_id

        # Verify receipt with lines
        rec_row = by_id["synthetic-trumf-b-100"]
        assert rec_row["payload"]["hasReceipt"] is True
        assert len(rec_row["payload"]["varelinjer"]) == 2
        assert rec_row["payload"]["storeName"] == "KIWI Fiktiv Testbutikk"
        assert rec_row["payload"]["chain"] == "KIWI"
        assert rec_row["source_updated_at"] is None

        # Verify non-receipt transaction stub
        stub_row = by_id["synthetic-trumf-b-101"]
        assert stub_row["payload"]["hasReceipt"] is False
        assert stub_row["payload"]["varelinjer"] == []
        assert stub_row["payload"]["storeName"] == "MENY Fiktiv Testby"
        assert stub_row["payload"]["chain"] == "MENY"

        # Explicit assertion: triggers verified that kitchen_history and kitchen_inventory were NEVER written
        for table in ("kitchen_history", "kitchen_inventory"):
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count == 0

    # Second sync: idempotent replay
    initial_checkpoint = checkpoint(memory_engine, "trumf", "alice")
    replayed_count = sync_account(memory_engine, "trumf", "alice", adapter)
    assert replayed_count == 2

    # Checkpoint advances to fresh generation, but no duplicate rows were added
    new_checkpoint = checkpoint(memory_engine, "trumf", "alice")
    assert new_checkpoint != initial_checkpoint

    with memory_engine.connect() as connection:
        total_rows = connection.execute(
            select(func.count()).select_from(provider_order_sources)
        ).scalar()
        assert total_rows == 2


def test_sync_account_persists_correction_revisions(memory_engine):
    revisions = synthetic_trumf_receipt_revisions()
    base_rev = revisions[0]
    corrected_rev = revisions[1]

    tx = TrumfTransaction.model_validate(
        {
            "batchId": "synthetic-trumf-b-100",
            "beskrivelse": "KIWI Fiktiv Testbutikk",
            "belop": "268.41",
            "transaksjonsTidspunkt": "2026-09-05T17:58:14.000Z",
            "harKvittering": True,
        }
    )

    # First sync with base receipt
    source_1 = TrumfFixtureSource([tx], receipts={"synthetic-trumf-b-100": base_rev.payload})
    adapter_1 = TrumfPurchaseHistoryAdapter(source_1, "alice")
    assert sync_account(memory_engine, "trumf", "alice", adapter_1) == 1

    with memory_engine.connect() as connection:
        rows = connection.execute(select(provider_order_sources)).mappings().all()
        assert len(rows) == 1
        assert rows[0]["payload"]["belop"] == "268.41"

    # Second sync with corrected receipt
    tx_corrected = TrumfTransaction.model_validate(
        {
            "batchId": "synthetic-trumf-b-100",
            "beskrivelse": "KIWI Fiktiv Testbutikk",
            "belop": "250.00",
            "transaksjonsTidspunkt": "2026-09-05T17:58:14.000Z",
            "harKvittering": True,
        }
    )
    source_2 = TrumfFixtureSource(
        [tx_corrected], receipts={"synthetic-trumf-b-100": corrected_rev.payload}
    )
    adapter_2 = TrumfPurchaseHistoryAdapter(source_2, "alice")
    assert sync_account(memory_engine, "trumf", "alice", adapter_2) == 1

    # Both revisions are retained under the same external_id
    with memory_engine.connect() as connection:
        rows = connection.execute(select(provider_order_sources)).mappings().all()
        assert len(rows) == 2
        assert rows[0]["external_id"] == rows[1]["external_id"] == "synthetic-trumf-b-100"
        assert rows[0]["revision_id"] != rows[1]["revision_id"]
        amounts = {Decimal(str(r["payload"]["belop"])) for r in rows}
        assert amounts == {Decimal("268.41"), Decimal("250.00")}


def test_sync_account_handles_incomplete_scan_error(memory_engine):
    txs = [
        TrumfTransaction.model_validate(
            {
                "batchId": f"batch-{i:03d}",
                "beskrivelse": "KIWI Test",
                "belop": "10.00",
                "transaksjonsTidspunkt": "2026-09-01T10:00:00.000Z",
                "harKvittering": False,
            }
        )
        for i in range(5)
    ]
    source = TrumfFixtureSource(txs)
    # page_size=2, max_pages=1 => truncates on page 1
    adapter = TrumfPurchaseHistoryAdapter(source, "alice", page_size=2, max_pages=1)

    with pytest.raises(IncompleteScanError, match="scan truncated"):
        sync_account(memory_engine, "trumf", "alice", adapter)

    # Even with incomplete scan, available records are safely persisted and checkpoint saved
    with memory_engine.connect() as connection:
        count = connection.execute(
            select(func.count()).select_from(provider_order_sources)
        ).scalar()
        assert count == 2


def test_unsupported_contract_shapes_fail_closed():
    with pytest.raises(UnsupportedTrumfContract):
        parse_transactions(12345)  # type: ignore

    with pytest.raises(UnsupportedTrumfContract):
        parse_transactions({"unexpected_envelope": []})

    with pytest.raises(UnsupportedTrumfContract):
        parse_receipt(12345)  # type: ignore

    with pytest.raises(UnsupportedTrumfContract, match="no batchId"):
        to_provider_order_record(
            TrumfReceipt(
                batch_id="",
                revision_id="sha256:123",
            )
        )
