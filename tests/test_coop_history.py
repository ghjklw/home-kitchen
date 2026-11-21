from decimal import Decimal

import httpx
import pytest
from fixtures.synthetic_kitchen.purchase_history import (
    coop_purchase_list_pages,
    coop_receipt_detail_revisions,
)
from sqlalchemy import create_engine, func, select

from home_kitchen.coop_history import (
    COOP_SCHEMA_SOURCE,
    COOP_SCHEMA_VERSION,
    CoopAuthenticationError,
    CoopHttpSource,
    UnsupportedCoopContract,
    parse_purchase_list,
    parse_receipt_detail,
    to_provider_order_record,
)
from home_kitchen.purchase_sync import ProviderPage, metadata, provider_order_sources, sync_account


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


def test_coop_http_source_falls_back_from_list_404_to_search():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.url.query.decode()))
        if request.url.path == "/user/pay/history/list":
            return httpx.Response(404)
        if request.url.path == "/user/pay/history/search":
            assert request.url.params.get("searchQuery") == ""
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
    purchases = source.get_purchases("user-1")
    assert [p.summary_id for p in purchases] == ["coop-search-1"]
    assert purchases[0].amount == Decimal("210.00")
    assert calls == [
        ("/user/pay/history/list", ""),
        ("/user/pay/history/search", "searchQuery="),
    ]


def test_coop_http_source_falls_back_to_month_and_dashboard_on_404():
    month_calls: list[str] = []

    def month_handler(request: httpx.Request) -> httpx.Response:
        month_calls.append(request.url.path)
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
    assert month_calls == [
        "/user/pay/history/list",
        "/user/pay/history/search",
        "/user/pay/history/month",
    ]

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
