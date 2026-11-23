import hashlib
import json
from decimal import Decimal

import httpx
import pytest
from fixtures.synthetic_kitchen.purchase_history import (
    coop_purchase_list_pages,
    coop_receipt_detail_revisions,
)
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select

from home_kitchen.coop_history import (
    COOP_SCHEMA_SOURCE,
    COOP_SCHEMA_VERSION,
    DEFAULT_COOP_BASE_URL,
    DEFAULT_COOP_USER_AGENT,
    CoopAuthenticationError,
    CoopHttpSource,
    CoopPurchaseHistoryAdapter,
    CoopPurchaseStub,
    UnsupportedCoopContract,
    parse_coop_search_page,
    parse_purchase_list,
    parse_receipt_detail,
    to_provider_order_record,
)
from home_kitchen.purchase_sync import ProviderPage, metadata, provider_order_sources, sync_account

pytestmark = pytest.mark.mocked_provider


def test_purchase_list_maps_fixture_stub_fields_and_overlapping_page():
    pages = coop_purchase_list_pages()
    parsed = parse_purchase_list(
        pages[1].response, continuation_observed=pages[1].continuation_observed
    )

    assert [purchase.summary_id for purchase in parsed] == [
        "synthetic-coop-summary-001",
        "synthetic-coop-summary-002",
    ]
    assert parsed[0].amount == Decimal("86.75")
    assert parsed[0].purchase_date is not None
    assert parsed[0].store_id == "synthetic-coop-store-01"
    assert parsed[0].chain_id == "synthetic-coop-chain"
    assert parsed[0].total_discount == Decimal("3.25")


def test_purchase_list_accepts_comma_and_dot_decimal_amounts():
    parsed = parse_purchase_list(
        {
            "purchases": [
                {"summaryId": "comma", "amount": " 7,00 ", "totalDiscount": "-0,50"},
                {"summaryId": "dot", "amount": "12.34", "totalDiscount": None},
                {"summaryId": "numeric", "amount": 42.5, "totalDiscount": 0},
            ]
        }
    )

    assert parsed[0].amount == Decimal("7.00")
    assert parsed[0].total_discount == Decimal("-0.50")
    assert parsed[1].amount == Decimal("12.34")
    assert parsed[1].total_discount is None
    assert parsed[2].amount == Decimal("42.5")
    assert parsed[2].total_discount == Decimal("0")


def test_receipt_detail_preserves_fixture_ids_amounts_lines_and_attribution():
    fixture = coop_receipt_detail_revisions()[0]
    parsed = parse_receipt_detail(fixture.payload)

    assert parsed.summary_id == fixture.external_id
    assert parsed.receipt_id == "synthetic-coop-receipt-001"
    assert parsed.amount == Decimal("86.75")
    assert parsed.total_discount == Decimal("3.25")
    assert parsed.lines is not None
    assert parsed.lines[0].product_name == "Fictional gulrot / carrot ribbons"
    assert parsed.lines[0].ean13 is None
    assert parsed.lines[0].gtin13 is None
    assert parsed.lines[0].barcode is None
    assert parsed.lines[0].quantity is not None
    assert parsed.lines[0].quantity.value == Decimal("2")
    assert parsed.lines[0].discount == Decimal("1.50")
    assert parsed.lines[0].unit_price == Decimal("9.50")
    assert parsed.lines[1].product_name == "Fictional grain parcel"
    assert parsed.lines[1].quantity is None
    assert parsed.lines[1].discount is None
    assert parsed.source_uri == COOP_SCHEMA_SOURCE
    assert parsed.source_schema_version == COOP_SCHEMA_VERSION
    assert parsed.source_updated_at is None
    assert parsed.revision_id.startswith("sha256:")


def test_receipt_detail_accepts_comma_decimal_amounts_and_line_quantities():
    receipt = parse_receipt_detail(
        {
            "summary": {
                "summaryId": "comma-detail",
                "amount": "210,75",
                "totalDiscount": "10,25",
                "memberBonus": "0,50",
                "lines": [
                    {
                        "productName": "Sample product",
                        "quantity": "1,5 kg",
                        "amount": "3,75",
                        "discount": "0,25",
                        "discountAmountPerQuantity": "0,10",
                        "unitPrice": "2,50",
                    }
                ],
            }
        }
    )

    assert receipt.amount == Decimal("210.75")
    assert receipt.total_discount == Decimal("10.25")
    assert receipt.member_bonus == Decimal("0.50")
    assert receipt.lines is not None
    assert receipt.lines[0].quantity is not None
    assert receipt.lines[0].quantity.value == Decimal("1.5")
    assert receipt.lines[0].quantity.unit == "kg"
    assert receipt.lines[0].amount == Decimal("3.75")
    assert receipt.lines[0].discount == Decimal("0.25")
    assert receipt.lines[0].discount_amount_per_quantity == Decimal("0.10")
    assert receipt.lines[0].unit_price == Decimal("2.50")


def test_receipt_detail_maps_live_wire_fields_and_preserves_weighted_line_total():
    raw_summary = {
        "summaryId": "synthetic-live-wire-shape",
        "purchaseDate": "2026-11-10T18:59:00+01:00",
        "totalSum": "12,00",
        "lines": [
            {
                "name": "Synthetic weighed item",
                "quantity": "2 kg",
                "price": "5,00",
            },
            {"name": "Synthetic second item", "quantity": "1", "price": "7,00"},
        ],
    }
    response = {"summary": raw_summary}

    receipt = parse_receipt_detail(response)
    normalized = receipt.standardize()
    record = to_provider_order_record(receipt)

    assert normalized.total_amount == Decimal("12.00")
    assert normalized.lines[0].name == "Synthetic weighed item"
    assert normalized.lines[0].amount == Decimal("5.00")
    assert normalized.lines[0].quantity == Decimal("2")
    assert normalized.lines[0].unit == "kg"
    assert normalized.lines[0].ean is None
    assert normalized.total_amount == sum(line.amount for line in normalized.lines)
    assert normalized.lines[0].amount != normalized.lines[0].quantity * Decimal("5.00")
    assert record.payload["amount"] == "12.00"
    assert record.payload["lines"][0]["productName"] == "Synthetic weighed item"
    assert record.payload["lines"][0]["amount"] == "5.00"

    legacy_revision = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "normalizer_version": "2",
                    "history_year": None,
                    "history_month": None,
                    "source": raw_summary,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert receipt.revision_id != legacy_revision
    assert parse_receipt_detail(response).revision_id == receipt.revision_id


@pytest.mark.parametrize("grouping", [" ", "\u00a0", "\u202f"])
def test_purchase_list_accepts_grouped_total_discounts(grouping):
    parsed = parse_purchase_list(
        {"purchases": [{"summaryId": "grouped", "totalDiscount": f"1{grouping}088.02"}]}
    )

    assert parsed[0].total_discount == Decimal("1088.02")


def test_receipt_detail_accepts_grouped_amounts_and_blank_optional_decimals():
    receipt = parse_receipt_detail(
        {
            "summary": {
                "summaryId": "localized-detail",
                "totalDiscount": "1\u00a0088.02",
                "amount": "1\u202f234,50",
                "lines": [
                    {
                        "productName": "Sample product",
                        "quantity": "",
                        "amount": "1 088.02",
                        "discount": " \u00a0 ",
                    }
                ],
            }
        }
    )

    assert receipt.total_discount == Decimal("1088.02")
    assert receipt.amount == Decimal("1234.50")
    assert receipt.lines is not None
    assert receipt.lines[0].quantity is None
    assert receipt.lines[0].amount == Decimal("1088.02")
    assert receipt.lines[0].discount is None
    record = to_provider_order_record(receipt)
    assert record.payload["lines"][0]["quantity"] is None


def test_every_coop_decimal_field_uses_localized_format_and_empty_as_missing():
    raw = "1\u00a0088,02"
    list_items = parse_purchase_list(
        {
            "purchases": [
                {"summaryId": "list-1", "amount": raw, "totalDiscount": raw},
                {"summaryId": "list-2", "amount": "", "totalDiscount": ""},
            ]
        }
    )
    assert list_items[0].amount == list_items[0].total_discount == Decimal("1088.02")
    assert list_items[1].amount is None
    assert list_items[1].total_discount is None

    receipt = parse_receipt_detail(
        {
            "summary": {
                "summaryId": "detail-all-fields",
                "amount": raw,
                "totalDiscount": raw,
                "memberBonus": raw,
                "lines": [
                    {
                        "productName": "Synthetic item",
                        "quantity": raw,
                        "amount": raw,
                        "discount": raw,
                        "discountAmountPerQuantity": raw,
                        "unitPrice": raw,
                    },
                    {
                        "productName": "Synthetic item without numbers",
                        "quantity": "",
                        "amount": "",
                        "discount": "",
                        "discountAmountPerQuantity": "",
                        "unitPrice": "",
                    },
                ],
            }
        }
    )
    for field in ("amount", "total_discount", "member_bonus"):
        assert getattr(receipt, field) == Decimal("1088.02")
    assert receipt.lines is not None
    numeric_line_fields = (
        "amount",
        "discount",
        "discount_amount_per_quantity",
        "unit_price",
    )
    assert receipt.lines[0].quantity is not None
    assert receipt.lines[0].quantity.value == Decimal("1088.02")
    assert receipt.lines[0].quantity.unit is None
    assert receipt.lines[1].quantity is None
    for field in numeric_line_fields:
        assert getattr(receipt.lines[0], field) == Decimal("1088.02")
        assert getattr(receipt.lines[1], field) is None


def test_receipt_decimal_rejects_malformed_grouping():
    with pytest.raises(ValidationError):
        parse_purchase_list({"purchases": [{"summaryId": "invalid", "totalDiscount": "12 34.56"}]})


def test_revision_hash_is_stable_and_distinguishes_fixture_correction():
    revisions = coop_receipt_detail_revisions()
    base = parse_receipt_detail(revisions[0].payload)
    replay = parse_receipt_detail(revisions[0].payload)
    corrected = parse_receipt_detail(revisions[1].payload)

    assert replay.revision_id == base.revision_id
    assert corrected.revision_id != base.revision_id
    assert corrected.summary_id == base.summary_id
    assert corrected.amount == Decimal("85.25")


def test_empty_history_is_valid_but_continuation_and_unknown_shapes_fail_closed():
    assert parse_purchase_list({"purchases": []}) == []
    first_page = coop_purchase_list_pages()[0]
    with pytest.raises(
        UnsupportedCoopContract, match="continuation response schema is undocumented"
    ):
        parse_purchase_list(
            first_page.response, continuation_observed=first_page.continuation_observed
        )
    with pytest.raises(
        UnsupportedCoopContract, match="unsupported Coop purchase-list response shape"
    ):
        parse_purchase_list({"results": []})
    with pytest.raises(
        UnsupportedCoopContract, match="unsupported Coop receipt-detail response shape"
    ):
        parse_receipt_detail({"summary": {}, "nextPage": "opaque"})


def test_fixture_imports_dedupe_overlap_and_preserve_correction_revisions():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    revisions = coop_receipt_detail_revisions()
    records = [
        to_provider_order_record(parse_receipt_detail(revision.payload)) for revision in revisions
    ]

    class FixtureAdapter:
        def __init__(self):
            self.calls = []

        def fetch_page(self, account_id, cursor):
            self.calls.append((account_id, cursor))
            if cursor is None:
                return ProviderPage(
                    records=[records[0]], next_cursor="fixture-page-2", has_more=True
                )
            if cursor == "fixture-page-2":
                return ProviderPage(
                    records=[records[0], records[1], records[2]],
                    next_cursor="fixture-terminal",
                    has_more=False,
                )
            return ProviderPage(
                records=records,
                next_cursor="fixture-terminal",
                has_more=False,
            )

    adapter = FixtureAdapter()
    try:
        assert sync_account(engine, "coop", "synthetic-account", adapter) == 4
        assert sync_account(engine, "coop", "synthetic-account", adapter) == 3
        assert adapter.calls == [
            ("synthetic-account", None),
            ("synthetic-account", "fixture-page-2"),
            ("synthetic-account", "fixture-terminal"),
        ]
        with engine.connect() as connection:
            rows = connection.execute(select(provider_order_sources)).mappings().all()
            count = connection.execute(
                select(func.count()).select_from(provider_order_sources)
            ).scalar_one()
        assert count == 3
        assert sum(row["external_id"] == "synthetic-coop-summary-001" for row in rows) == 2
        assert all(row["source_updated_at"] is None for row in rows)
    finally:
        engine.dispose()


class _MutableCoopSource:
    def __init__(self, summary_ids: list[str]) -> None:
        self.purchase_calls = 0
        self.receipt_calls: list[str] = []
        self.summary_ids = summary_ids

    def get_purchases(self, account_id: str) -> list[CoopPurchaseStub]:
        self.purchase_calls += 1
        return [
            CoopPurchaseStub.model_validate(
                {"summaryId": summary_id, "historyYear": 2026, "historyMonth": 11}
            )
            for summary_id in self.summary_ids
        ]

    def get_receipt(
        self,
        account_id: str,
        summary_id: str,
        *,
        history_year: int | None = None,
        history_month: int | None = None,
    ):
        self.receipt_calls.append(summary_id)
        return parse_receipt_detail(
            {"summary": {"summaryId": summary_id, "amount": "1.00"}},
            history_year=history_year,
            history_month=history_month,
        )


def test_coop_adapter_caches_one_stable_purchase_snapshot_per_scan():
    source = _MutableCoopSource(["a", "b", "c", "d"])
    adapter = CoopPurchaseHistoryAdapter(source, "user-1", page_size=2)

    first_page = adapter.fetch_page("user-1", None)
    assert [record.external_id for record in first_page.records] == ["a", "b"]
    assert first_page.has_more is True
    assert source.purchase_calls == 1

    source.summary_ids = ["new", "a", "b", "c", "d"]
    second_page = adapter.fetch_page("user-1", first_page.next_cursor)
    assert [record.external_id for record in second_page.records] == ["c", "d"]
    assert second_page.has_more is False
    assert source.purchase_calls == 1

    next_scan = adapter.fetch_page("user-1", second_page.next_cursor)
    assert [record.external_id for record in next_scan.records] == ["new", "a"]
    assert source.purchase_calls == 2


def test_coop_adapter_restarts_at_page_one_when_scanning_cursor_cache_is_stale():
    source = _MutableCoopSource(["a", "b", "c"])
    original_adapter = CoopPurchaseHistoryAdapter(source, "user-1", page_size=1)
    original_first_page = original_adapter.fetch_page("user-1", None)
    stale_cursor = original_first_page.next_cursor
    assert [record.external_id for record in original_first_page.records] == ["a"]

    source.summary_ids = ["new", "x", "y", "z"]
    restarted_adapter = CoopPurchaseHistoryAdapter(source, "user-1", page_size=1)
    restarted_page = restarted_adapter.fetch_page("user-1", stale_cursor)
    assert [record.external_id for record in restarted_page.records] == ["new"]
    assert restarted_page.next_cursor != stale_cursor
    assert source.purchase_calls == 2


def _history_group(year: int, month: int, purchases: list[dict]) -> dict:
    return {
        "monthOfYear": {"year": year, "month": month},
        "periodPurchases": purchases,
    }


def _search_response(*groups: dict, token: str | None = None) -> dict:
    return {"hitsPerMonth": list(groups), "continuationToken": token}


def test_coop_search_accepts_search_text_echo_and_rejects_unexpected_shapes():
    payload = {
        "hitsPerMonth": [],
        "continuationToken": None,
        "searchText": "",
        "resultCode": "OK",
        "errorCode": None,
        "errorMessage": None,
    }
    assert parse_coop_search_page(payload) == ([], None)

    with pytest.raises(UnsupportedCoopContract, match="searchText:int"):
        parse_coop_search_page({**payload, "searchText": 42})
    with pytest.raises(UnsupportedCoopContract, match="unsupportedField:dict"):
        parse_coop_search_page({**payload, "unsupportedField": {}})


def test_coop_search_maps_observed_total_sum_stub_field():
    purchases, token = parse_coop_search_page(
        _search_response(
            _history_group(
                2026,
                11,
                [
                    {
                        "summaryId": "synthetic-total-sum-stub",
                        "dateTime": "10. november 18:59",
                        "totalSum": "321,50",
                    }
                ],
            )
        )
    )

    assert token is None
    assert len(purchases) == 1
    assert purchases[0].amount == Decimal("321.50")
    assert purchases[0].purchase_date is not None
    assert purchases[0].purchase_date.year == 2026
    assert purchases[0].purchase_date.month == 11


def test_coop_http_source_searches_all_pages_and_preserves_authoritative_years():
    calls: list[tuple[str, dict[str, str]]] = []
    first = [
        {"summaryId": f"coop-new-{index:02}", "dateTime": "18.11. 12:00"} for index in range(51)
    ]
    second = [
        {"summaryId": "coop-new-50", "dateTime": "18.11. 12:00"},
        *[{"summaryId": f"coop-old-{index:02}", "dateTime": "07.04. 16:40"} for index in range(9)],
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, dict(request.url.params)))
        assert request.headers.get("accept") == "application/json;charset=UTF-8"
        assert request.headers.get("content-type") == "application/json; charset=utf-8"
        assert request.headers.get("user-agent") == DEFAULT_COOP_USER_AGENT
        assert request.headers.get("x-gltlocale") == "nb_NO"
        assert request.headers.get("x-token") == "coop-token"
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-deviceid") == source.device_id
        if request.url.path == "/user/pay/history/search":
            assert request.url.params.get("text") == ""
            return httpx.Response(
                200,
                json=_search_response(_history_group(2026, 11, first), token="page-2"),
            )
        if request.url.path == "/user/pay/history/search/continue":
            assert request.url.params.get("token") == "page-2"
            return httpx.Response(
                200,
                json=_search_response(
                    _history_group(2026, 11, second[:1]),
                    _history_group(2023, 4, second[1:]),
                    token=None,
                ),
            )
        return httpx.Response(404)

    source = CoopHttpSource(access_token="coop-token", transport=httpx.MockTransport(handler))
    assert source.base_url == DEFAULT_COOP_BASE_URL == "https://coopay.coop.no"
    assert len(source.device_id) == 16
    purchases = source.get_purchases("user-1")
    assert len(purchases) == 60
    assert purchases[0].summary_id == "coop-new-00"
    assert purchases[-1].summary_id == "coop-old-08"
    assert purchases[0].purchase_date is not None
    assert purchases[0].purchase_date.year == 2026
    assert purchases[0].purchase_date.month == 11
    assert purchases[-1].purchase_date is not None
    assert purchases[-1].purchase_date.year == 2023
    assert purchases[-1].purchase_date.month == 4
    assert purchases[-1].history_year == 2023
    assert purchases[-1].history_month == 4
    assert calls == [
        ("/user/pay/history/search", {"text": ""}),
        ("/user/pay/history/search/continue", {"token": "page-2"}),
    ]


def test_coop_search_overlap_deduplicates_summary_ids_across_groups_and_pages():
    pages = iter(
        [
            _search_response(
                _history_group(
                    2026, 11, [{"summaryId": "same", "purchaseDate": "2026-11-01T10:00:00Z"}]
                ),
                token="more",
            ),
            _search_response(
                _history_group(
                    2026, 11, [{"summaryId": "same", "purchaseDate": "2026-11-01T10:00:00Z"}]
                ),
                _history_group(
                    2025, 12, [{"summaryId": "other", "purchaseDate": "2025-12-01T10:00:00Z"}]
                ),
            ),
        ]
    )
    source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(pages))),
    )
    assert [purchase.summary_id for purchase in source.get_purchases("user-1")] == [
        "same",
        "other",
    ]


def test_coop_search_rejects_duplicate_summary_with_conflicting_month_context():
    pages = iter(
        [
            _search_response(
                _history_group(
                    2026,
                    11,
                    [{"summaryId": "same", "dateTime": "07.11. 16:40"}],
                ),
                token="more",
            ),
            _search_response(
                _history_group(
                    2023,
                    11,
                    [{"summaryId": "same", "dateTime": "07.11. 16:40"}],
                )
            ),
        ]
    )
    source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(pages))),
    )

    with pytest.raises(UnsupportedCoopContract, match="conflicting history month context"):
        source.get_purchases("user-1")


def test_coop_month_group_corrects_mismatched_year_but_rejects_month_conflict():
    corrected_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_search_response(
                    _history_group(
                        2023,
                        4,
                        [{"summaryId": "corrected", "purchaseDate": "2026-04-07T16:40:00Z"}],
                    )
                ),
            )
        ),
    )
    corrected = corrected_source.get_purchases("user-1")[0]
    assert corrected.purchase_date is not None
    assert corrected.purchase_date.year == 2023
    assert corrected.purchase_date.month == 4
    assert corrected.purchase_date.day == 7

    conflicting_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_search_response(
                    _history_group(
                        2023,
                        4,
                        [{"summaryId": "conflict", "purchaseDate": "2026-03-07T16:40:00Z"}],
                    )
                ),
            )
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="month conflicts with its history grouping"):
        conflicting_source.get_purchases("user-1")


def test_coop_search_rejects_unaddressable_purchase_summary():
    source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_search_response(_history_group(2023, 4, [{"amount": "10.00"}])),
            )
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="missing summaryId; scan is incomplete"):
        source.get_purchases("user-1")


def test_coop_missing_or_yearless_date_never_uses_current_year():
    source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_search_response(
                    _history_group(
                        2023,
                        4,
                        [
                            {"summaryId": "has-no-date"},
                            {"summaryId": "yearless-no-context", "dateTime": "07.04. 16:40"},
                        ],
                    )
                ),
            )
        ),
    )
    purchases = source.get_purchases("user-1")
    assert purchases[0].purchase_date is None
    assert purchases[1].purchase_date is not None
    assert purchases[1].purchase_date.year == 2023
    no_context = parse_receipt_detail(
        {"summary": {"summaryId": "partial", "dateTime": "07.04. 16:40"}}
    )
    assert no_context.purchase_date is None


def test_coop_detail_yearless_date_uses_search_month_context_and_revision_changes():
    older = parse_receipt_detail(
        {"summary": {"summaryId": "same", "dateTime": "07.04. 16:40"}},
        history_year=2023,
        history_month=4,
    )
    newer = parse_receipt_detail(
        {"summary": {"summaryId": "same", "dateTime": "07.04. 16:40"}},
        history_year=2026,
        history_month=4,
    )
    assert older.purchase_date is not None and older.purchase_date.year == 2023
    assert newer.purchase_date is not None and newer.purchase_date.year == 2026
    assert older.revision_id != newer.revision_id


def test_coop_norwegian_month_name_uses_authoritative_history_context():
    purchase = CoopPurchaseStub.model_validate(
        {
            "summaryId": "norwegian-month",
            "dateTime": "10. november 18:59",
            "historyYear": 2026,
            "historyMonth": 11,
        }
    )
    assert purchase.purchase_date is not None
    assert purchase.purchase_date.isoformat() == "2026-11-10T18:59:00+01:00"

    no_context = CoopPurchaseStub.model_validate(
        {"summaryId": "no-context", "dateTime": "10. november 18:59"}
    )
    assert no_context.purchase_date is None

    with pytest.raises(ValidationError, match="month conflicts with its history grouping"):
        CoopPurchaseStub.model_validate(
            {
                "summaryId": "conflicting-month",
                "dateTime": "10. november 18:59",
                "historyYear": 2026,
                "historyMonth": 4,
            }
        )


def test_coop_search_fails_closed_for_malformed_or_truncated_traversal():
    malformed_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"hitsPerMonth": []})
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="missing continuationToken"):
        malformed_source.get_purchases("user-1")

    repeated_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_search_response(token="loop"),
            )
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="repeated a continuation token"):
        repeated_source.get_purchases("user-1")

    truncated_source = CoopHttpSource(
        access_token="coop-token",
        max_search_pages=1,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_search_response(token="more"))
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="exceeded its page limit"):
        truncated_source.get_purchases("user-1")


def test_coop_http_source_preserves_auth_and_upgrade_errors():
    all_404 = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(lambda req: httpx.Response(404)),
    )
    with pytest.raises(UnsupportedCoopContract, match="HTTP 404"):
        all_404.get_purchases("user-1")

    def auth_fail_on_fallback(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    unauth_source = CoopHttpSource(
        access_token="expired",
        transport=httpx.MockTransport(auth_fail_on_fallback),
    )
    with pytest.raises(CoopAuthenticationError):
        unauth_source.get_purchases("user-1")

    upgrade_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(403, headers={"x-client-upgrade": "force"})
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="app-oppgradering"):
        upgrade_source.get_purchases("user-1")


def test_coop_http_source_handles_base_response_errors_and_extra_442_fields():
    perm_error_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "resultCode": "PERMANENT_ERROR",
                    "errorCode": "UNEXPECTED_SERVER_ERROR",
                    "errorMessage": "A temporary error has occurred.",
                },
            )
        ),
    )
    with pytest.raises(UnsupportedCoopContract, match="PERMANENT_ERROR: UNEXPECTED_SERVER_ERROR"):
        perm_error_source.get_purchases("user-1")

    token_error_source = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"resultCode": "PERMANENT_ERROR", "errorCode": "INVALID_TOKEN"},
            )
        ),
    )
    with pytest.raises(CoopAuthenticationError):
        token_error_source.get_purchases("user-1")

    def detail_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/pay/history/details":
            return httpx.Response(
                200,
                json={
                    "resultCode": "OK",
                    "summary": {
                        "summaryId": "sum-442",
                        "receiptId": "rec-442",
                        "purchaseDate": "2026-11-20T12:00:00Z",
                        "storeName": "Coop Mega",
                        "amount": "199.00",
                        "couponDiscount": "10.00",
                        "subTotalDiscount": "15.00",
                        "lines": [
                            {
                                "productName": "Gulost",
                                "ean13": "7038010009457",
                                "quantity": "1",
                                "amount": "99.00",
                                "lowestPriceDiscount": "5.00",
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    detail_source = CoopHttpSource(
        access_token="coop-token", transport=httpx.MockTransport(detail_handler)
    )
    receipt = detail_source.get_receipt("user-1", "sum-442")
    assert receipt is not None
    assert receipt.summary_id == "sum-442"
    assert receipt.lines is not None
    assert receipt.lines[0].product_name == "Gulost"
