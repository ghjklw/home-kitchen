from decimal import Decimal

import pytest

from home_kitchen.coop_history import (
    COOP_SCHEMA_SOURCE,
    COOP_SCHEMA_VERSION,
    UnsupportedCoopContract,
    parse_purchase_list,
    parse_receipt_detail,
)


def test_purchase_list_maps_documented_stub_fields_and_missing_fields():
    parsed = parse_purchase_list(
        {
            "purchases": [
                {
                    "summaryId": "synthetic-summary-1",
                    "purchaseDate": "2026-11-15T16:04:05Z",
                    "storeName": "Synthetic Extra",
                    "storeId": "synthetic-store-1",
                    "chainId": "Extra",
                    "amount": 48.5,
                    "totalDiscount": 2.5,
                },
                {"summaryId": "synthetic-summary-2"},
            ]
        }
    )

    assert parsed[0].summary_id == "synthetic-summary-1"
    assert parsed[0].amount == Decimal("48.5")
    assert parsed[0].purchase_date is not None
    assert parsed[1].summary_id == "synthetic-summary-2"
    assert parsed[1].purchase_date is None
    assert parsed[1].amount is None


def test_receipt_detail_preserves_provider_ids_amounts_lines_and_attribution():
    parsed = parse_receipt_detail(
        {
            "summary": {
                "summaryId": "synthetic-summary-1",
                "receiptId": "synthetic-receipt-1",
                "purchaseDate": "2026-11-15T16:04:05+01:00",
                "storeName": "Synthetic Extra",
                "storeId": "synthetic-store-1",
                "chainId": "Extra",
                "amount": 48.5,
                "totalDiscount": 2.5,
                "lines": [
                    {
                        "productName": "Synthetic apples",
                        "ean13": "0000000000000",
                        "gtin13": "1111111111111",
                        "barcode": "2222222222222",
                        "quantity": 2,
                        "amount": 48.5,
                        "discount": 2.5,
                        "discountAmountPerQuantity": 1.25,
                        "unitPrice": 25.5,
                    }
                ],
            }
        }
    )

    assert parsed.summary_id == "synthetic-summary-1"
    assert parsed.receipt_id == "synthetic-receipt-1"
    assert parsed.amount == Decimal("48.5")
    assert parsed.lines is not None
    assert parsed.lines[0].product_name == "Synthetic apples"
    assert parsed.lines[0].ean13 == "0000000000000"
    assert parsed.lines[0].gtin13 == "1111111111111"
    assert parsed.lines[0].barcode == "2222222222222"
    assert parsed.lines[0].discount_amount_per_quantity == Decimal("1.25")
    assert parsed.lines[0].unit_price == Decimal("25.5")
    assert parsed.source_uri == COOP_SCHEMA_SOURCE
    assert parsed.source_schema_version == COOP_SCHEMA_VERSION
    assert parsed.source_updated_at is None
    assert parsed.revision_id.startswith("sha256:")


def test_receipt_revision_is_stable_and_changes_with_source_contents():
    payload = {"summary": {"summaryId": "synthetic-1", "amount": 10}}

    assert parse_receipt_detail(payload).revision_id == parse_receipt_detail(payload).revision_id
    assert (
        parse_receipt_detail(payload).revision_id
        != parse_receipt_detail({"summary": {"summaryId": "synthetic-1", "amount": 11}}).revision_id
    )


def test_empty_history_is_valid_but_unknown_or_continuation_shapes_fail_closed():
    assert parse_purchase_list({"purchases": []}) == []
    with pytest.raises(
        UnsupportedCoopContract, match="continuation response schema is undocumented"
    ):
        parse_purchase_list({"purchases": []}, continuation_observed=True)
    with pytest.raises(
        UnsupportedCoopContract, match="unsupported Coop purchase-list response shape"
    ):
        parse_purchase_list({"results": []})
    with pytest.raises(
        UnsupportedCoopContract, match="unsupported Coop receipt-detail response shape"
    ):
        parse_receipt_detail({"summary": {}, "nextPage": "opaque"})
