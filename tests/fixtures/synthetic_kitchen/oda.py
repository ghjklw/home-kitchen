"""Fictional response data shaped for the current Oda-facing app adapters.

Every identifier, label and URL is synthetic. The ``.invalid`` host prevents
these fixtures from being mistaken for fetchable product or recipe pages.
The collection shape below is the app adapter contract, not a claim about an
undocumented provider endpoint.
"""

from typing import Any


def synthetic_oda_product_search_response() -> dict[str, Any]:
    return {
        "result": [
            {
                "query": "havredrikk",
                "hasMore": True,
                "products": [
                    {
                        "id": 9900001,
                        "name": "TEST ONLY Havrevik oat drink, 1 L",
                        "description": "Fictional fixture product; not a sale listing.",
                        "price": "24.00",
                        "unitPrice": "24.00",
                        "unitName": "per L",
                        "brand": "SYNTHETIC FIXTURE",
                        "brandId": 9900,
                        "url": "https://fixtures.invalid/oda/products/9900001",
                        "availability": {
                            "isAvailable": True,
                            "description": "Synthetic fixture state",
                            "code": "FIXTURE_ONLY",
                        },
                    }
                ],
            },
            {
                "query": "gulrot",
                "hasMore": False,
                "products": [
                    {
                        "id": 9900002,
                        "name": "TEST ONLY Gulrotbånd, 2 stk",
                        "description": "Fictional fixture product; not a sale listing.",
                        "price": "18.50",
                        "unitPrice": "9.25",
                        "unitName": "per stk",
                        "brand": "SYNTHETIC FIXTURE",
                        "brandId": 9900,
                        "url": "https://fixtures.invalid/oda/products/9900002",
                    }
                ],
            },
        ]
    }


def synthetic_oda_recipe_collection_response() -> dict[str, Any]:
    """Return the current Oda collection adapter shape with fictional entries."""
    return {
        "hasMore": True,
        "recipes": [
            {
                "id": "synthetic-oda-recipe-0001",
                "title": "TEST ONLY Barley cloud bake",
                "url": "https://fixtures.invalid/oda/recipes/synthetic-oda-recipe-0001",
                "imageUrl": None,
                "providerName": "SYNTHETIC FIXTURE",
                "deliveriesCount": 2,
            }
        ],
    }
