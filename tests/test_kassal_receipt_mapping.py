import httpx
import pytest

from home_kitchen.kassal import KassalAuthError, KassalClient
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
