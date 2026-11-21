import httpx
import pytest

from home_kitchen.kassal import KassalAuthError, KassalClient, KassalError, KassalRateLimitError
from home_kitchen.purchase_sync import (
    ProviderOrderRecord,
    enrich_and_map_receipt_record,
    extract_line_ean,
)

pytestmark = pytest.mark.mocked_provider


def _transport(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.headers.get("authorization") != "Bearer good":
            return httpx.Response(401, json={"message": "Unauthenticated"})
        return httpx.Response(
            200,
            json={
                "data": {
                    "products": [
                        {
                            "name": "Økologisk Gulrot 1kg",
                            "brand": "Tine",
                            "weight": 1,
                            "weight_unit": "kg",
                        }
                    ]
                }
            },
        )

    return httpx.MockTransport(handler)


def test_kassal_client_caches_and_rejects_bad_key():
    calls: list[str] = []
    client = KassalClient("good", transport=_transport(calls))
    assert client.lookup_ean("123 45678").name.startswith("Økologisk")
    client.lookup_ean("12345678")
    assert len(calls) == 1
    with pytest.raises(KassalAuthError):
        KassalClient("bad", transport=_transport([])).validate_key()


def test_receipt_enrichment_collects_unmapped():
    record = ProviderOrderRecord(
        external_id="1", revision_id="r", payload={"lines": [{"ean": "12345678", "name": "GULROT"}]}
    )
    assert extract_line_ean(record.payload["lines"][0]) == "12345678"
    client = KassalClient("good", transport=_transport([]))
    unmapped = enrich_and_map_receipt_record(record, client, None)
    assert unmapped and unmapped[0]["name"].startswith("Økologisk")
    assert record.payload["lines"][0]["kassal"]["brand"] == "Tine"


def test_kassal_client_raises_rate_limit_error_without_caching_429():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "45"},
                json={"message": "Too Many Attempts."},
            )
        return httpx.Response(
            200,
            json={"data": {"products": [{"name": "Kvikk Lunsj", "weight_unit": "stk"}]}},
        )

    client = KassalClient("good", transport=httpx.MockTransport(handler))
    with pytest.raises(KassalRateLimitError) as exc_info:
        client.lookup_ean("7032069101520")
    assert isinstance(exc_info.value, KassalError)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 45.0

    # A 429 must not poison the client's positive/404 cache; subsequent calls succeed and cache.
    prod = client.lookup_ean("7032069101520")
    assert prod is not None
    assert prod.name == "Kvikk Lunsj"
    assert client.lookup_ean("7032069101520") is prod
    assert attempts == 2


def test_enrich_and_map_receipt_record_caches_eans_and_stops_on_429():
    looked_up: list[str] = []

    class _NonCachingRateLimitedKassal:
        def lookup_ean(self, ean: str):
            looked_up.append(ean)
            if ean == "7038010000188":
                return type(
                    "_Prod",
                    (),
                    {
                        "ean": ean,
                        "name": "Tine Helmelk 1L",
                        "brand": "Tine",
                        "vendor": "Tine SA",
                        "weight": 1.0,
                        "weight_unit": "l",
                        "category": "Meieri",
                        "image": None,
                    },
                )()
            if ean == "7032069101520":
                raise KassalRateLimitError(
                    'Kassal.app svarte med HTTP 429 for EAN 7032069101520: {"message":"Too Many Attempts."}'
                )
            raise AssertionError(f"Unexpected lookup after 429: {ean}")

    record = ProviderOrderRecord(
        external_id="rec-1",
        revision_id="rev-1",
        payload={
            "lines": [
                {"ean": "7038010000188", "name": "HELMELK 1L", "amount": "24.90"},
                {"ean": "7038010000188", "name": "HELMELK 1L", "amount": "24.90"},
                {"ean": "7032069101520", "name": "KVIKK LUNSJ", "amount": "39.90"},
                {"ean": "7032069999999", "name": "EPLE", "amount": "19.90"},
            ]
        },
    )

    unmapped = enrich_and_map_receipt_record(record, _NonCachingRateLimitedKassal(), None)
    assert looked_up == ["7038010000188", "7032069101520"]
    assert len(unmapped) == 4
    assert unmapped[0]["name"] == "Tine Helmelk 1L"
    assert unmapped[1]["name"] == "Tine Helmelk 1L"
    assert unmapped[2]["name"] == "KVIKK LUNSJ"
    assert unmapped[3]["name"] == "EPLE"
    assert record.payload["lines"][2]["name"] == "KVIKK LUNSJ"
    assert record.payload["lines"][2]["amount"] == "39.90"
    assert "kassal" not in record.payload["lines"][2]
    assert record.payload["lines"][3]["name"] == "EPLE"
    assert record.payload["lines"][3]["amount"] == "19.90"
    assert "kassal" not in record.payload["lines"][3]
