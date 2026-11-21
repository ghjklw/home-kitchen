"""Unit tests for Oda search adapter."""

from unittest.mock import AsyncMock

import pytest

from home_kitchen.basket.adapters.oda_search import OdaSearchAdapter


@pytest.mark.asyncio
async def test_oda_search_adapter_search_products():
    mock_service = AsyncMock()
    mock_service.search_products.return_value = {
        "result": [
            {
                "query": "hvetemel",
                "hasMore": False,
                "products": [
                    {
                        "id": 123,
                        "name": "Hvetemel siktet",
                        "description": "1 kg",
                        "brand": "Møllergutten",
                        "url": "https://oda.com/products/123",
                        "price": "25.00",
                        "unitPrice": "25.00 /kg",
                        "unitName": "kg",
                        "availability": {
                            "isAvailable": True,
                            "description": "Tilgjengelig",
                            "code": "available",
                        },
                    }
                ],
            }
        ]
    }

    adapter = OdaSearchAdapter(mock_service)
    groups = await adapter.search_products("actor-1", ["all-purpose flour"], 1, 5)
    assert len(groups) == 1
    assert groups[0].query == "all-purpose flour"
    assert len(groups[0].products) == 1
    assert groups[0].products[0].product_id == "123"
    assert groups[0].products[0].identity_status == "compatible"
