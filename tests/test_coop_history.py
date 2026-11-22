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
    UnsupportedCoopContract,
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
    assert parsed.lines[0].quantity == Decimal("2")
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
                        "quantity": "1,5",
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
    assert receipt.lines[0].quantity == Decimal("1.5")
    assert receipt.lines[0].amount == Decimal("3.75")
    assert receipt.lines[0].discount == Decimal("0.25")
    assert receipt.lines[0].discount_amount_per_quantity == Decimal("0.10")
    assert receipt.lines[0].unit_price == Decimal("2.50")


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
        "quantity",
        "amount",
        "discount",
        "discount_amount_per_quantity",
        "unit_price",
    )
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


def test_coop_http_source_defaults_and_falls_back_from_list_404_to_search():
    calls: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.path, request.url.query.decode()))
        assert request.headers.get("accept") == "application/json;charset=UTF-8"
        assert request.headers.get("content-type") == "application/json; charset=utf-8"
        assert request.headers.get("user-agent") == DEFAULT_COOP_USER_AGENT
        assert request.headers.get("x-gltlocale") == "nb_NO"
        assert request.headers.get("x-token") == "coop-token"
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-deviceid") == source.device_id
        if request.url.path == "/user/pay/history/list":
            assert request.url.params.get("n") == "50"
            return httpx.Response(404)
        if request.url.path == "/user/pay/history/search":
            assert request.url.params.get("text") == ""
            return httpx.Response(
                200,
                json={
                    "purchases": [
                        {
                            "summaryId": "coop-search-1",
                            "storeName": "Coop Mega Bislett",
                            "purchaseDate": "2026-11-18T12:00:00Z",
                            "amount": "210.00",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    source = CoopHttpSource(access_token="coop-token", transport=httpx.MockTransport(handler))
    assert source.base_url == DEFAULT_COOP_BASE_URL == "https://coopay.coop.no"
    assert len(source.device_id) == 16
    purchases = source.get_purchases("user-1")
    assert [p.summary_id for p in purchases] == ["coop-search-1"]
    assert purchases[0].amount == Decimal("210.00")
    assert calls == [
        ("coopay.coop.no", "/user/pay/history/list", "n=50"),
        ("coopay.coop.no", "/user/pay/history/search", "text="),
    ]


def test_coop_http_source_fetches_list_with_n_param_and_custom_device_id():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.url.query.decode()))
        assert request.headers.get("x-deviceid") == "custom-device-id"
        assert request.headers.get("x-token") == "coop-token"
        if request.url.path == "/user/pay/history/list":
            assert request.url.params.get("n") == "50"
            return httpx.Response(
                200,
                json={
                    "purchases": [
                        {
                            "summaryId": "coop-list-50",
                            "storeName": "Coop Extra",
                            "purchaseDate": "2026-11-19T12:00:00Z",
                            "amount": "120.00",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    source = CoopHttpSource(
        access_token="coop-token",
        device_id="custom-device-id",
        transport=httpx.MockTransport(handler),
    )
    purchases = source.get_purchases("user-1")
    assert [p.summary_id for p in purchases] == ["coop-list-50"]
    assert calls == [("/user/pay/history/list", "n=50")]


def test_coop_http_source_falls_back_to_month_and_dashboard_on_404():
    month_calls: list[tuple[str, dict[str, str]]] = []

    def month_handler(request: httpx.Request) -> httpx.Response:
        month_calls.append((request.url.path, dict(request.url.params)))
        if request.url.path in ("/user/pay/history/list", "/user/pay/history/search"):
            return httpx.Response(404)
        if request.url.path == "/user/pay/history/month":
            return httpx.Response(
                200,
                json={
                    "purchases": [
                        {
                            "summaryId": "coop-month-1",
                            "storeName": "Extra Sagene",
                            "purchaseDate": "2026-11-15T10:30:00Z",
                            "amount": "95.50",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    month_source = CoopHttpSource(
        access_token="coop-token", transport=httpx.MockTransport(month_handler)
    )
    purchases = month_source.get_purchases("user-1")
    assert [p.summary_id for p in purchases] == ["coop-month-1"]
    assert [path for path, _ in month_calls] == [
        "/user/pay/history/list",
        "/user/pay/history/search",
        "/user/pay/history/month",
    ]
    assert set(month_calls[2][1]) == {"year", "month"}

    dash_calls: list[str] = []

    def dashboard_handler(request: httpx.Request) -> httpx.Response:
        dash_calls.append(request.url.path)
        if request.url.path == "/user/pay/history/dashboard":
            return httpx.Response(
                200,
                json={
                    "monthlyBreakdown": [
                        {
                            "month": "2026-11",
                            "purchases": [
                                {
                                    "summaryId": "coop-dash-1",
                                    "storeName": "Coop Prix",
                                    "purchaseDate": "2026-11-10T09:00:00Z",
                                    "amount": "64.00",
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    dash_source = CoopHttpSource(
        access_token="coop-token", transport=httpx.MockTransport(dashboard_handler)
    )
    dash_purchases = dash_source.get_purchases("user-1")
    assert [p.summary_id for p in dash_purchases] == ["coop-dash-1"]
    assert dash_calls == [
        "/user/pay/history/list",
        "/user/pay/history/search",
        "/user/pay/history/month",
        "/user/pay/history/dashboard",
    ]


def test_coop_http_source_raises_auth_or_contract_error_when_fallbacks_fail():
    all_404 = CoopHttpSource(
        access_token="coop-token",
        transport=httpx.MockTransport(lambda req: httpx.Response(404)),
    )
    with pytest.raises(UnsupportedCoopContract, match="HTTP 404"):
        all_404.get_purchases("user-1")

    def auth_fail_on_fallback(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/pay/history/list":
            return httpx.Response(404)
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
