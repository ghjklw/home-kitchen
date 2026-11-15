from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from home_kitchen.basket.matching import (
    BasketMatchingService,
    OdaSearchAdapter,
    ProviderProduct,
    ProviderSearchGroup,
    _explicit_pack,
)
from home_kitchen.oda.media import ProductMedia


class FakeInventory:
    def __init__(self, lines):
        self.lines = lines

    def plan(self, start, end):
        assert start == date(2026, 11, 16)
        assert end == date(2026, 11, 22)
        return type("Plan", (), {"purchase_lines": self.lines})()


class FakeProvider:
    def __init__(self, *, fail_query=None):
        self.calls = []
        self.fail_query = fail_query

    async def search_products(self, actor_id, queries, page, size):
        assert actor_id == "household-1"
        self.calls.append((queries, page, size))
        if self.fail_query in queries:
            raise TimeoutError("simulated provider timeout")
        return [
            ProviderSearchGroup(
                query=query,
                has_more=page == 1,
                products=[
                    ProviderProduct(
                        provider="oda",
                        product_id=str(index + 10),
                        name=f"{query} product",
                        description="Provider description kept as-is",
                        price="29.90",
                        unit_price="29.90",
                        unit_name="per kg",
                        product_url=f"https://oda.com/no/products/{index + 10}-product",
                    )
                ],
            )
            for index, query in enumerate(queries)
        ]


def line(name="carrot", quantity="1", unit="kg", index=1):
    return type(
        "PurchaseLine",
        (),
        {
            "id": UUID(int=index),
            "name": name,
            "to_buy_quantity": Decimal(quantity),
            "unit": unit,
        },
    )()


@pytest.mark.asyncio
async def test_search_batches_queries_and_exposes_next_page_and_reviewable_candidates():
    provider = FakeProvider()
    service = BasketMatchingService(
        FakeInventory([line("carrot", index=1), line("milk", quantity="2", unit="l", index=2)]),
        provider,
        media_fetcher=lambda _: _media("500 g"),
    )

    result = await service.find_products(
        "household-1", date(2026, 11, 16), date(2026, 11, 22), page=1, size=5
    )

    assert len(provider.calls) == 1
    assert provider.calls[0][0] == ["carrot", "milk"]
    assert [item.status for item in result.needs] == ["matched", "matched"]
    assert all(item.has_more for item in result.needs)
    first = result.needs[0].candidates[0]
    assert first.id == "oda:10"
    assert first.description == "Provider description kept as-is"
    assert first.pack_quantity == Decimal("500")
    assert first.pack_unit == "g"
    assert first.recommended_packages == 2
    assert first.recommended_fulfillment_quantity == Decimal("1000")
    assert result.needs[1].candidates[0].recommended_packages is None


@pytest.mark.asyncio
async def test_failed_batch_is_reported_without_dropping_successful_need():
    provider = FakeProvider(fail_query="milk")
    service = BasketMatchingService(
        FakeInventory(
            [line(f"item {index}", index=index) for index in range(1, 11)]
            + [line("milk", index=11)]
        ),
        provider,
        media_fetcher=lambda _: _media(None),
    )

    result = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))

    assert [item.status for item in result.needs[:10]] == ["matched"] * 10
    assert result.needs[10].status == "search_failed"
    assert all(item.candidates for item in result.needs[:10])
    assert result.needs[10].candidates == []


def test_pack_metadata_requires_full_unambiguous_supported_measurement():
    assert _explicit_pack(" 8 x 125 g ") == (Decimal("1000"), "g")
    assert _explicit_pack("1,5 l") == (Decimal("1500"), "ml")
    assert _explicit_pack("8 stk") == (Decimal("8"), "item")
    assert _explicit_pack("Milk 1 l, price 25 kr") is None
    assert _explicit_pack("per kg") is None
    assert _explicit_pack("1 l / 500 ml") is None


@pytest.mark.asyncio
async def test_oda_adapter_normalizes_official_fields_to_provider_contract():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            return {
                "result": [
                    {
                        "query": "carrot",
                        "hasMore": False,
                        "products": [
                            {
                                "id": 77,
                                "name": "Carrots",
                                "description": "1 kg carrots",
                                "price": "25.00",
                                "unitPrice": "25.00",
                                "unitName": "per kg",
                                "url": "https://oda.com/no/products/77-carrots",
                            }
                        ],
                    }
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products("household-1", ["carrot"], 1, 5)

    assert groups[0].query == "carrot"
    assert groups[0].products[0].provider == "oda"
    assert groups[0].products[0].product_id == "77"
    assert groups[0].products[0].description == "1 kg carrots"


async def _media(pack_description):
    return ProductMedia(
        url="https://oda.com/no/products/10-product",
        image_url="https://images.oda.com/10.jpg",
        pack_description=pack_description,
        detail="Public Oda product page metadata.",
    )
