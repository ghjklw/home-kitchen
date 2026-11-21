"""Unit tests for the read-only Rema 1000 receipt and purchase history adapter.

All tests use synthetic Norwegian grocery fixtures and run completely offline
without calling live endpoints, loading credentials, or writing to kitchen domain tables.
"""

from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from fixtures.synthetic_kitchen.purchase_history import (
    rema1000_transaction_pages,
    synthetic_rema1000_receipt_json,
    synthetic_rema1000_receipt_revisions,
    synthetic_rema1000_transactions_json,
    synthetic_rema1000_unauthenticated_html,
    synthetic_rema1000_unauthenticated_json,
)
from sqlalchemy import create_engine, func, select, text

from home_kitchen.purchase_sync import (
    IncompleteScanError,
    checkpoint,
    metadata,
    provider_order_sources,
    sync_account,
)
from home_kitchen.rema1000_history import (
    DEFAULT_REMA1000_BASE_URL,
    DEFAULT_REMA1000_SUBSCRIPTION_KEY,
    REMA1000_AUTH_METHOD,
    REMA1000_HISTORY_WINDOW_MONTHS,
    REMA1000_LIMITATIONS,
    REMA1000_SCHEMA_SOURCE,
    REMA1000_SCHEMA_VERSION,
    Rema1000AuthenticationError,
    Rema1000FixtureSource,
    Rema1000HttpSource,
    Rema1000PurchaseHistoryAdapter,
    Rema1000Receipt,
    Rema1000ReceiptLine,
    Rema1000Transaction,
    UnsupportedRema1000Contract,
    _decode_cursor,
    _encode_cursor,
    _Rema1000ScanCursor,
    build_transaction_receipt_stub,
    parse_receipt,
    parse_transactions,
    to_provider_order_record,
)


@pytest.fixture
def memory_engine():
    """Disposable SQLite database with triggers asserting zero household domain writes."""
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


def test_parse_transactions_from_json_and_list():
    json_str = synthetic_rema1000_transactions_json()
    transactions = parse_transactions(json_str)

    assert len(transactions) == 2

    first = transactions[0]
    assert first.receipt_id == "synthetic-rema-rec-100"
    assert first.store_name == "REMA 1000 Vulkan"
    assert first.store_id == "1042"
    assert first.chain == "REMA 1000"
    assert first.total_amount == Decimal("184.50")
    assert first.total_discount == Decimal("14.50")
    assert first.has_receipt is True
    assert first.timestamp.year == 2026
    assert first.timestamp.month == 9
    assert first.timestamp.day == 8

    second = transactions[1]
    assert second.receipt_id == "synthetic-rema-stub-101"
    assert second.store_name == "REMA 1000 Torggata"
    assert second.store_id == "1088"
    assert second.total_amount == Decimal("42.00")
    assert second.total_discount == Decimal("0.0")
    assert second.has_receipt is False


def test_parse_transactions_unauthenticated_fails_closed():
    unauthenticated_html = synthetic_rema1000_unauthenticated_html()
    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_transactions(unauthenticated_html)

    unauthenticated_json = synthetic_rema1000_unauthenticated_json()
    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_transactions(unauthenticated_json)

    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_transactions({"authenticated": False, "receipts": []})

    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_transactions({"statusCode": 401, "message": "Unauthorized"})

    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_transactions("<!DOCTYPE html><html><body><h1>Logg inn</h1></body></html>")


def test_parse_transactions_unsupported_contract():
    with pytest.raises(UnsupportedRema1000Contract):
        parse_transactions(12345)  # type: ignore

    with pytest.raises(UnsupportedRema1000Contract):
        parse_transactions({"unexpectedEnvelope": []})

    with pytest.raises(UnsupportedRema1000Contract):
        parse_transactions("{not-valid-json")

    with pytest.raises(UnsupportedRema1000Contract):
        parse_transactions({"receipts": "not-a-list"})


def test_parse_receipt_detail_from_json():
    json_str = synthetic_rema1000_receipt_json()
    receipt = parse_receipt(json_str)

    assert receipt.receipt_id == "synthetic-rema-rec-100"
    assert receipt.store_name == "REMA 1000 Vulkan"
    assert receipt.store_id == "1042"
    assert receipt.chain == "REMA 1000"
    assert receipt.total_amount == Decimal("184.50")
    assert receipt.total_discount == Decimal("14.50")
    assert receipt.has_receipt is True
    assert receipt.source_uri == REMA1000_SCHEMA_SOURCE
    assert receipt.source_schema_version == REMA1000_SCHEMA_VERSION
    assert receipt.source_updated_at is None
    assert receipt.revision_id.startswith("sha256:")

    assert len(receipt.lines) == 2

    milk = receipt.lines[0]
    assert milk.name == "FIKTIV REMA 1000 HELMELK 1L"
    assert milk.ean == "7032069704018"
    assert milk.quantity == Decimal("2")
    assert milk.unit == "stk"
    assert milk.unit_price == Decimal("24.50")
    assert milk.amount == Decimal("49.00")
    assert milk.discount == Decimal("0.0")
    assert milk.tax_percent == Decimal("15")
    assert milk.discounts == []

    carrots = receipt.lines[1]
    assert carrots.name == "FIKTIV GULROTBÅND / CARROT RIBBONS"
    assert carrots.ean is None  # nullable EAN preserved without inferring canonical identity
    assert carrots.quantity == Decimal("0.5")
    assert carrots.unit == "kg"
    assert carrots.unit_price == Decimal("45.00")
    assert carrots.amount == Decimal("18.00")
    assert carrots.discount == Decimal("4.50")
    assert carrots.tax_percent == Decimal("15")
    assert len(carrots.discounts) == 1
    assert carrots.discounts[0].type == "personal_cut"
    assert carrots.discounts[0].description == "10 på 10"
    assert carrots.discounts[0].amount == Decimal("4.50")
    assert carrots.discounts[0].percentage == Decimal("20")


def test_parse_receipt_unauthenticated_fails_closed():
    unauthenticated_html = synthetic_rema1000_unauthenticated_html()
    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_receipt(unauthenticated_html)

    unauthenticated_json = synthetic_rema1000_unauthenticated_json()
    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_receipt(unauthenticated_json)

    with pytest.raises(Rema1000AuthenticationError, match="unauthenticated response"):
        parse_receipt({"statusCode": 401, "error": "Unauthorized"})


def test_parse_synthetic_dict_format_compatibility():
    pages = rema1000_transaction_pages()
    tx_page = cast(list[dict[str, Any]], pages[0].response)
    transactions = parse_transactions(tx_page)

    assert len(transactions) == 1
    assert transactions[0].receipt_id == "synthetic-rema-rec-100"
    assert transactions[0].total_amount == Decimal("184.50")
    assert transactions[0].total_discount == Decimal("14.50")
    assert transactions[0].chain == "REMA 1000"

    detail_page = cast(dict[str, Any], pages[2].response)
    receipt = parse_receipt(detail_page, transaction=transactions[0])

    assert receipt.receipt_id == "synthetic-rema-rec-100"
    assert receipt.store_name == "REMA 1000 Vulkan"
    assert receipt.chain == "REMA 1000"
    assert len(receipt.lines) == 3
    assert receipt.lines[0].name == "FIKTIV REMA 1000 HELMELK 1L"
    assert receipt.lines[1].ean is None
    assert receipt.lines[2].name == "FIKTIV REMA 1000 HAVREGRYN LETTKOKTE 1KG"


def test_parse_receipt_enrichment_from_transaction():
    tx = Rema1000Transaction.model_validate(
        {
            "receiptId": "rec-enrich-001",
            "storeName": "REMA 1000 Nydalen",
            "storeId": "1099",
            "chain": "REMA 1000",
            "totalAmount": "99.00",
            "totalDiscount": "5.00",
            "timestamp": "2026-09-08T12:00:00+02:00",
            "hasReceipt": True,
        }
    )
    raw_detail: dict[str, Any] = {
        "lines": [
            {
                "name": "FIKTIV BROKKOLI",
                "amount": "99.00",
            }
        ]
    }
    receipt = parse_receipt(raw_detail, transaction=tx)

    assert receipt.receipt_id == "rec-enrich-001"
    assert receipt.store_name == "REMA 1000 Nydalen"
    assert receipt.store_id == "1099"
    assert receipt.total_amount == Decimal("99.00")
    assert receipt.total_discount == Decimal("5.00")
    assert receipt.has_receipt is True


def test_revision_id_is_stable_and_detects_corrections():
    revisions = synthetic_rema1000_receipt_revisions()
    base_raw = revisions[0].payload
    corrected_raw = revisions[1].payload

    base_1 = parse_receipt(base_raw)
    base_2 = parse_receipt(base_raw)
    corrected = parse_receipt(corrected_raw)

    assert base_1.revision_id == base_2.revision_id
    assert corrected.revision_id != base_1.revision_id
    assert corrected.receipt_id == base_1.receipt_id
    assert corrected.total_amount == Decimal("175.50")
    assert corrected.total_discount == Decimal("23.50")


def test_build_transaction_receipt_stub_for_non_receipt_transaction():
    tx = Rema1000Transaction.model_validate(
        {
            "receiptId": "synthetic-rema-stub-101",
            "storeName": "REMA 1000 Torggata",
            "storeId": "1088",
            "chain": "REMA 1000",
            "totalAmount": "42.00",
            "totalDiscount": "0.0",
            "timestamp": "2026-09-09T11:15:00+02:00",
            "hasReceipt": False,
        }
    )
    stub = build_transaction_receipt_stub(tx)

    assert stub.receipt_id == "synthetic-rema-stub-101"
    assert stub.store_name == "REMA 1000 Torggata"
    assert stub.store_id == "1088"
    assert stub.chain == "REMA 1000"
    assert stub.total_amount == Decimal("42.00")
    assert stub.total_discount == Decimal("0.0")
    assert stub.has_receipt is False
    assert stub.lines == []
    assert stub.revision_id.startswith("sha256:")

    record = to_provider_order_record(stub)
    assert record.external_id == "synthetic-rema-stub-101"
    assert record.payload["hasReceipt"] is False
    assert record.payload["lines"] == []


def test_adapter_status_documents_limits():
    source = Rema1000FixtureSource([])
    adapter = Rema1000PurchaseHistoryAdapter(source, "alice")
    status = adapter.status()

    assert status.provider == "rema1000"
    assert status.history_window_months == REMA1000_HISTORY_WINDOW_MONTHS == 24
    assert status.auth_method == REMA1000_AUTH_METHOD == "manual_session_token"
    assert status.read_only is True
    assert status.live_calls_enabled is False
    assert status.limitations == REMA1000_LIMITATIONS
    assert "24 months" in status.limitations
    assert "session token" in status.limitations


def test_cursor_encoding_decoding_and_validation():
    cursor = _Rema1000ScanCursor(
        version=1,
        mode="full_rescan",
        account_id="alice",
        scan_id="00000000-0000-0000-0000-000000000001",
        next_page=2,
        state="scanning",
    )
    encoded = _encode_cursor(cursor)
    assert encoded.startswith("rema1000-scan-v1.")

    decoded = _decode_cursor(encoded)
    assert decoded == cursor

    with pytest.raises(ValueError, match="Invalid Rema 1000 history scan cursor"):
        _decode_cursor("")
    with pytest.raises(ValueError, match="Invalid Rema 1000 history scan cursor"):
        _decode_cursor("invalid-prefix.abc")
    with pytest.raises(ValueError, match="Invalid Rema 1000 history scan cursor"):
        _decode_cursor("rema1000-scan-v1.not-base64!!!")


def test_adapter_input_validation():
    source = Rema1000FixtureSource([])

    with pytest.raises(ValueError, match="account_id must be non-empty"):
        Rema1000PurchaseHistoryAdapter(source, "")

    with pytest.raises(ValueError, match="account_id must be non-empty and trimmed"):
        Rema1000PurchaseHistoryAdapter(source, "  alice  ")

    with pytest.raises(ValueError, match="page_size must be at least 1"):
        Rema1000PurchaseHistoryAdapter(source, "alice", page_size=0)

    with pytest.raises(ValueError, match="max_pages must be between 1 and 50"):
        Rema1000PurchaseHistoryAdapter(source, "alice", max_pages=0)

    with pytest.raises(ValueError, match="max_pages must be between 1 and 50"):
        Rema1000PurchaseHistoryAdapter(source, "alice", max_pages=51)


def test_adapter_bounded_pagination_and_truncation():
    txs = [
        Rema1000Transaction.model_validate(
            {
                "receiptId": f"rec-{i:03d}",
                "storeName": "REMA 1000 Test",
                "totalAmount": "10.00",
                "timestamp": "2026-09-01T10:00:00+02:00",
                "hasReceipt": False,
            }
        )
        for i in range(5)
    ]
    source = Rema1000FixtureSource(txs)
    # Page size 2, max pages 2 (can fetch at most 4 items before truncation)
    adapter = Rema1000PurchaseHistoryAdapter(source, "alice", page_size=2, max_pages=2)

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
    source = Rema1000FixtureSource([])
    adapter = Rema1000PurchaseHistoryAdapter(source, "alice")

    with pytest.raises(ValueError, match="account mismatch"):
        adapter.fetch_page("bob", None)

    foreign_cursor = _encode_cursor(
        _Rema1000ScanCursor(
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
    txs_json = synthetic_rema1000_transactions_json()
    rec_json = synthetic_rema1000_receipt_json()

    source = Rema1000FixtureSource(
        txs_json,
        receipts={"synthetic-rema-rec-100": rec_json},
    )
    adapter = Rema1000PurchaseHistoryAdapter(source, "alice")

    # Initial sync
    imported_count = sync_account(memory_engine, "rema1000", "alice", adapter)
    assert imported_count == 2

    with memory_engine.connect() as connection:
        rows = (
            connection.execute(
                select(provider_order_sources).where(
                    provider_order_sources.c.provider == "rema1000"
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 2

        by_id = {row["external_id"]: row for row in rows}
        assert "synthetic-rema-rec-100" in by_id
        assert "synthetic-rema-stub-101" in by_id

        # Verify receipt with lines
        rec_row = by_id["synthetic-rema-rec-100"]
        assert rec_row["payload"]["hasReceipt"] is True
        assert len(rec_row["payload"]["lines"]) == 2
        assert rec_row["payload"]["storeName"] == "REMA 1000 Vulkan"
        assert rec_row["payload"]["chain"] == "REMA 1000"
        assert rec_row["source_updated_at"] is None

        # Verify non-receipt transaction stub
        stub_row = by_id["synthetic-rema-stub-101"]
        assert stub_row["payload"]["hasReceipt"] is False
        assert stub_row["payload"]["lines"] == []
        assert stub_row["payload"]["storeName"] == "REMA 1000 Torggata"

        # Explicit assertion: triggers verified that kitchen_history and kitchen_inventory were NEVER written
        for table in ("kitchen_history", "kitchen_inventory"):
            count = connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert count == 0

    # Second sync: idempotent replay
    initial_checkpoint = checkpoint(memory_engine, "rema1000", "alice")
    replayed_count = sync_account(memory_engine, "rema1000", "alice", adapter)
    assert replayed_count == 2

    # Checkpoint advances to fresh generation, but no duplicate rows were added
    new_checkpoint = checkpoint(memory_engine, "rema1000", "alice")
    assert new_checkpoint != initial_checkpoint

    with memory_engine.connect() as connection:
        total_rows = connection.execute(
            select(func.count()).select_from(provider_order_sources)
        ).scalar()
        assert total_rows == 2


def test_sync_account_persists_correction_revisions(memory_engine):
    revisions = synthetic_rema1000_receipt_revisions()
    base_rev = revisions[0]
    corrected_rev = revisions[1]

    tx = Rema1000Transaction.model_validate(
        {
            "receiptId": "synthetic-rema-rec-100",
            "storeName": "REMA 1000 Vulkan",
            "totalAmount": "184.50",
            "totalDiscount": "14.50",
            "timestamp": "2026-09-08T15:30:00+02:00",
            "hasReceipt": True,
        }
    )

    # First sync with base receipt
    source_1 = Rema1000FixtureSource([tx], receipts={"synthetic-rema-rec-100": base_rev.payload})
    adapter_1 = Rema1000PurchaseHistoryAdapter(source_1, "alice")
    assert sync_account(memory_engine, "rema1000", "alice", adapter_1) == 1

    with memory_engine.connect() as connection:
        rows = connection.execute(select(provider_order_sources)).mappings().all()
        assert len(rows) == 1
        assert Decimal(str(rows[0]["payload"]["totalAmount"])) == Decimal("184.50")

    # Second sync with corrected receipt
    tx_corrected = Rema1000Transaction.model_validate(
        {
            "receiptId": "synthetic-rema-rec-100",
            "storeName": "REMA 1000 Vulkan",
            "totalAmount": "175.50",
            "totalDiscount": "23.50",
            "timestamp": "2026-09-08T15:30:00+02:00",
            "hasReceipt": True,
        }
    )
    source_2 = Rema1000FixtureSource(
        [tx_corrected], receipts={"synthetic-rema-rec-100": corrected_rev.payload}
    )
    adapter_2 = Rema1000PurchaseHistoryAdapter(source_2, "alice")
    assert sync_account(memory_engine, "rema1000", "alice", adapter_2) == 1

    # Both revisions are retained under the same external_id
    with memory_engine.connect() as connection:
        rows = connection.execute(select(provider_order_sources)).mappings().all()
        assert len(rows) == 2
        assert rows[0]["external_id"] == rows[1]["external_id"] == "synthetic-rema-rec-100"
        assert rows[0]["revision_id"] != rows[1]["revision_id"]
        amounts = {Decimal(str(r["payload"]["totalAmount"])) for r in rows}
        assert amounts == {Decimal("184.50"), Decimal("175.50")}


def test_sync_account_handles_incomplete_scan_error(memory_engine):
    txs = [
        Rema1000Transaction.model_validate(
            {
                "receiptId": f"rec-{i:03d}",
                "storeName": "REMA 1000 Test",
                "totalAmount": "10.00",
                "timestamp": "2026-09-01T10:00:00+02:00",
                "hasReceipt": False,
            }
        )
        for i in range(5)
    ]
    source = Rema1000FixtureSource(txs)
    # page_size=2, max_pages=1 => truncates on page 1
    adapter = Rema1000PurchaseHistoryAdapter(source, "alice", page_size=2, max_pages=1)

    with pytest.raises(IncompleteScanError, match="scan truncated"):
        sync_account(memory_engine, "rema1000", "alice", adapter)

    # Even with incomplete scan, available records are safely persisted and checkpoint saved
    with memory_engine.connect() as connection:
        count = connection.execute(
            select(func.count()).select_from(provider_order_sources)
        ).scalar()
        assert count == 2


def test_unsupported_contract_shapes_fail_closed():
    with pytest.raises(UnsupportedRema1000Contract):
        parse_receipt(12345)  # type: ignore

    with pytest.raises(UnsupportedRema1000Contract):
        parse_receipt([])  # type: ignore

    with pytest.raises(UnsupportedRema1000Contract, match="no receipt_id"):
        to_provider_order_record(
            Rema1000Receipt(
                receipt_id="",
                store_name="REMA 1000 Vulkan",
                timestamp="2026-09-08T15:30:00+02:00",
                total_amount=Decimal("100"),
                revision_id="sha256:123",
            )
        )


def test_line_item_ean_normalization_and_nullable_barcode():
    line = Rema1000ReceiptLine.model_validate(
        {
            "name": "FIKTIV VARE",
            "ean": " $undefined ",
            "amount": "10.00",
        }
    )
    assert line.ean is None

    line_empty = Rema1000ReceiptLine.model_validate(
        {
            "name": "FIKTIV VARE 2",
            "ean": "",
            "amount": "10.00",
        }
    )
    assert line_empty.ean is None

    line_valid = Rema1000ReceiptLine.model_validate(
        {
            "name": "FIKTIV VARE 3",
            "ean": " 7032069704018 ",
            "amount": "10.00",
        }
    )
    assert line_valid.ean == "7032069704018"


@pytest.fixture(autouse=True)
def _no_kassal_enrichment(monkeypatch):
    """These fixtures predate the Kassal requirement; barcode enrichment is tested separately."""
    monkeypatch.setattr("home_kitchen.purchase_sync.extract_line_ean", lambda line: None)


def test_rema1000_http_source_uses_bella_endpoints_and_headers():
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.url.path == "/v1/bella/transaction/v2/heads":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "id": "bella-tx-1",
                            "purchaseDate": "2026-11-18T14:00:00Z",
                            "storeId": "1042",
                            "storeName": "REMA 1000 Vulkan",
                            "amount": "129.90",
                            "discount": "20.00",
                            "hasReceipt": True,
                        }
                    ]
                },
            )
        if request.url.path == "/v1/bella/transaction/v2/rows/bella-tx-1":
            return httpx.Response(
                200,
                json=[
                    {
                        "prodtxt1": "NORVEGIA 1KG",
                        "prodtxt3": "7038010009457",
                        "unitPrice": "149.90",
                        "amount": "129.90",
                        "discount": "20.00",
                    }
                ],
            )
        return httpx.Response(404)

    source = Rema1000HttpSource("rema-jwt", transport=httpx.MockTransport(handler))
    assert source.base_url == DEFAULT_REMA1000_BASE_URL == "https://api.rema.no"
    assert source.subscription_key == DEFAULT_REMA1000_SUBSCRIPTION_KEY

    txs = source.get_transactions("alice")
    assert len(txs) == 1
    assert txs[0].receipt_id == "bella-tx-1"
    assert txs[0].store_name == "REMA 1000 Vulkan"
    assert txs[0].total_amount == Decimal("129.90")
    assert txs[0].total_discount == Decimal("20.00")

    receipt = source.get_receipt("alice", "bella-tx-1")
    assert receipt is not None
    assert receipt.receipt_id == "bella-tx-1"
    assert receipt.store_name == "REMA 1000 Vulkan"
    assert len(receipt.lines) == 1
    assert receipt.lines[0].name == "NORVEGIA 1KG"
    assert receipt.lines[0].ean == "7038010009457"
    assert receipt.lines[0].amount == Decimal("129.90")
    assert receipt.lines[0].discount == Decimal("20.00")

    assert source.get_receipt("alice", "missing-tx") is None

    assert [req.url.path for req in seen_requests] == [
        "/v1/bella/transaction/v2/heads",
        "/v1/bella/transaction/v2/rows/bella-tx-1",
        "/v1/bella/transaction/v2/rows/missing-tx",
    ]
    for req in seen_requests:
        assert req.headers.get("authorization") == "Bearer rema-jwt"
        assert req.headers.get("ocp-apim-subscription-key") == DEFAULT_REMA1000_SUBSCRIPTION_KEY
        assert req.headers.get("x-app") == "Bella"
        assert req.headers.get("x-platform") == "CLI"
        assert req.headers.get("x-app-version") == "3.13.0.1181"
        assert req.headers.get("accept-language") == "no"


def test_rema1000_http_source_custom_subscription_key_and_404_fallback():
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        assert request.headers.get("ocp-apim-subscription-key") == "custom-sub-key"
        assert request.headers.get("x-app") == "Bella"
        if request.url.path == "/v1/receipts":
            return httpx.Response(
                200,
                json={
                    "receipts": [
                        {
                            "receiptId": "legacy-1",
                            "storeName": "REMA 1000 Majorstuen",
                            "totalAmount": "50.00",
                            "timestamp": "2026-11-18T14:00:00Z",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    source = Rema1000HttpSource(
        "rema-jwt",
        subscription_key="custom-sub-key",
        transport=httpx.MockTransport(handler),
    )
    txs = source.get_transactions("alice")
    assert [tx.receipt_id for tx in txs] == ["legacy-1"]
    assert seen_paths == ["/v1/bella/transaction/v2/heads", "/v1/receipts"]

    failing_source = Rema1000HttpSource(
        "rema-jwt",
        transport=httpx.MockTransport(lambda req: httpx.Response(404)),
    )
    with pytest.raises(UnsupportedRema1000Contract, match="HTTP 404"):
        failing_source.get_transactions("alice")
