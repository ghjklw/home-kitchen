"""Offline MockTransport tests for live receipt HTTP sources and adapter_factory wiring."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from home_kitchen.coop_history import (
    CoopAuthenticationError,
    CoopHttpSource,
    CoopPurchaseHistoryAdapter,
)
from home_kitchen.features import _build_purchase_adapter
from home_kitchen.rema1000_history import (
    Rema1000AuthenticationError,
    Rema1000HttpSource,
    Rema1000PurchaseHistoryAdapter,
)
from home_kitchen.trumf_history import (
    TrumfAuthenticationError,
    TrumfHttpSource,
    TrumfPurchaseHistoryAdapter,
    UnsupportedTrumfContract,
)

pytestmark = pytest.mark.mocked_provider


def test_coop_http_source_fetches_purchases_and_receipt_detail():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        assert request.headers.get("x-token") == "coop-token"
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-gltlocale") == "nb_NO"
        assert request.headers.get("x-deviceid")
        if request.url.path == "/user/pay/history/search":
            assert request.url.params.get("text") == ""
            return httpx.Response(
                200,
                json={
                    "hitsPerMonth": [
                        {
                            "monthOfYear": {"year": 2023, "month": 4},
                            "periodPurchases": [
                                {
                                    "summaryId": "sum-1",
                                    "storeName": "Coop Prix",
                                    "dateTime": "07.04. 16:40",
                                    "amount": "149.50",
                                }
                            ],
                        }
                    ],
                    "continuationToken": None,
                },
            )
        if request.url.path == "/user/pay/history/details":
            return httpx.Response(
                200,
                json={
                    "summary": {
                        "summaryId": "sum-1",
                        "receiptId": "rcp-1",
                        "storeName": "Coop Prix",
                        "dateTime": "07.04. 16:40",
                        "amount": "149.50",
                        "lines": [
                            {
                                "productName": "Tine Helmelk",
                                "ean13": "7038010000188",
                                "quantity": "1",
                                "amount": "24.90",
                            }
                        ],
                    }
                },
            )
        return httpx.Response(404)

    source = CoopHttpSource(access_token="coop-token", transport=httpx.MockTransport(handler))
    purchases = source.get_purchases("user-1")
    assert len(purchases) == 1
    assert purchases[0].summary_id == "sum-1"
    assert purchases[0].history_year == 2023
    assert purchases[0].history_month == 4
    assert purchases[0].purchase_date is not None
    assert purchases[0].purchase_date.year == 2023

    receipt = source.get_receipt(
        "user-1",
        "sum-1",
        history_year=purchases[0].history_year,
        history_month=purchases[0].history_month,
    )
    assert receipt is not None
    assert len(receipt.lines) == 1
    assert receipt.lines[0].ean13 == "7038010000188"
    assert calls == [
        "GET /user/pay/history/search?text=",
        "GET /user/pay/history/details?summaryId=sum-1",
    ]
    assert receipt.purchase_date is not None
    assert receipt.purchase_date.year == 2023


def test_coop_http_source_raises_on_401():
    source = CoopHttpSource(
        access_token="expired",
        transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "invalid"})),
    )
    with pytest.raises(CoopAuthenticationError):
        source.get_purchases("user-1")


def test_rema1000_http_source_fetches_transactions_and_receipt():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer rema-jwt"
        assert request.headers.get("x-app") == "Bella"
        if request.url.path == "/v1/bella/transaction/v2/heads":
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "id": "r-101",
                            "purchaseDate": "2026-11-18T14:00:00Z",
                            "storeName": "REMA 1000 Majorstuen",
                            "amount": "89.00",
                            "hasReceipt": True,
                        }
                    ]
                },
            )
        if request.url.path == "/v1/bella/transaction/v2/rows/r-101":
            return httpx.Response(
                200,
                json=[
                    {
                        "prodtxt1": "Gulrot",
                        "prodtxt3": "7038010000188",
                        "quantity": "1",
                        "amount": "29.00",
                    }
                ],
            )
        return httpx.Response(404)

    source = Rema1000HttpSource("rema-jwt", transport=httpx.MockTransport(handler))
    txs = source.get_transactions("u1")
    assert len(txs) == 1
    receipt = source.get_receipt("u1", "r-101")
    assert receipt is not None
    assert receipt.lines[0].name == "Gulrot"


def test_rema1000_http_source_raises_on_401():
    source = Rema1000HttpSource(
        "bad",
        transport=httpx.MockTransport(lambda req: httpx.Response(401)),
    )
    with pytest.raises(Rema1000AuthenticationError):
        source.get_transactions("u1")


def test_trumf_http_source_fetches_transactions_and_raises_on_redirect():
    tx_payload = json.dumps(
        {
            "transactionsData": [
                {
                    "batchId": "b-1",
                    "transaksjonsTidspunkt": "2026-11-18T15:00:00Z",
                    "beskrivelse": "KIWI",
                    "belop": "55.00",
                    "harKvittering": True,
                }
            ]
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie") == "session=abc"
        return httpx.Response(200, text=tx_payload)

    source = TrumfHttpSource("session=abc", transport=httpx.MockTransport(handler))
    txs = source.get_transactions("u1")
    assert len(txs) == 1
    assert txs[0].batch_id == "b-1"

    null_batch_payload = json.dumps(
        {
            "transactionsData": [
                {
                    "batchId": None,
                    "transaksjonsTidspunkt": "2026-11-07T15:48:41.000Z",
                    "beskrivelse": "KIWI Kjørbo",
                    "belop": "2062.93",
                    "harKvittering": True,
                }
            ]
        }
    )
    null_source = TrumfHttpSource(
        "session=abc",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=null_batch_payload)),
    )
    adapter = TrumfPurchaseHistoryAdapter(null_source, account_id="u1")
    page = adapter.fetch_page("u1", None)
    assert len(page.records) == 1
    assert page.records[0].external_id.startswith("tx:")

    expired_source = TrumfHttpSource(
        "session=expired",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"location": "/login"})
        ),
    )
    with pytest.raises(TrumfAuthenticationError):
        expired_source.get_transactions("u1")


def test_trumf_http_source_cookie_normalization_batch_id_validation_redirects_and_null_receipt():
    with pytest.raises(TrumfAuthenticationError):
        TrumfHttpSource("Path=/; HttpOnly; Secure")

    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        assert request.headers.get("cookie") == "a=1; b=2"
        if request.url.path == "/trumf-profil/kvitteringer":
            return httpx.Response(
                200,
                text=(
                    '<script>self.__next_f.push([1,"{\\"transactionsData\\":['
                    '{\\"batchId\\":\\"b 1\\",\\"transaksjonsTidspunkt\\":\\"2026-11-18T15:00:00Z\\",'
                    '\\"beskrivelse\\":\\"KIWI\\",\\"belop\\":\\"55.00\\",\\"harKvittering\\":true}'
                    ']}"])</script>'
                ),
            )
        if request.url.path == "/trumf-profil/kvitteringer/b%201":
            return httpx.Response(
                200,
                text='<script>self.__next_f.push([1,"{\\"purchaseDetails\\":null}"])</script>',
            )
        return httpx.Response(404)

    source = TrumfHttpSource(
        "Cookie: a=1; Path=/;\nb=2; HttpOnly",
        transport=httpx.MockTransport(handler),
    )
    adapter = TrumfPurchaseHistoryAdapter(source, account_id="u1")
    page = adapter.fetch_page("u1", None)
    assert len(page.records) == 1
    assert page.records[0].external_id == "b 1"
    assert page.records[0].payload["hasReceipt"] is False
    assert any(url.endswith("/trumf-profil/kvitteringer/b%201") for url in seen_urls)

    for bad_batch_id in ("", "  ", "../secret", "a/b", "$undefined"):
        with pytest.raises(UnsupportedTrumfContract):
            source.get_receipt("u1", bad_batch_id)

    for redirect_code in (301, 308):
        redir_source = TrumfHttpSource(
            "session=abc",
            transport=httpx.MockTransport(lambda req, code=redirect_code: httpx.Response(code)),
        )
        with pytest.raises(TrumfAuthenticationError):
            redir_source.get_transactions("u1")
        with pytest.raises(TrumfAuthenticationError):
            redir_source.get_receipt("u1", "b-1")


def test_build_purchase_adapter_wires_all_three_providers():
    acc = str(uuid4())
    trumf = _build_purchase_adapter("trumf", acc, {"session_cookie": "s=1"})
    coop = _build_purchase_adapter("coop", acc, {"access_token": "t-1"})
    rema = _build_purchase_adapter("rema1000", acc, {"session_token": "r-1"})
    assert isinstance(trumf, TrumfPurchaseHistoryAdapter)
    assert isinstance(coop, CoopPurchaseHistoryAdapter)
    assert isinstance(rema, Rema1000PurchaseHistoryAdapter)
    assert _build_purchase_adapter("unknown", acc, {}) is None
