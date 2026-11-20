"""Tests for preferred and excluded Oda SKU enforcement in BasketMatchingService (Refs #68)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from home_kitchen.basket.matching import (
    BasketMatchingService,
    ProviderProduct,
    ProviderSearchGroup,
)
from home_kitchen.inventory import PurchasePlan, PurchasePlanLine


class _FakeProvider:
    def __init__(self, products: list[ProviderProduct]) -> None:
        self._products = products

    async def search_products(
        self, actor_id: str, queries: list[str], page: int = 1, size: int = 5
    ) -> list[ProviderSearchGroup]:
        return [
            ProviderSearchGroup(query=q, products=list(self._products), has_more=False)
            for q in queries
        ]


@pytest.mark.asyncio
async def test_preferred_and_excluded_oda_skus_shape_candidates_in_rank_order():
    carrot_id = uuid4()
    plan = PurchasePlan(
        start=date(2026, 11, 20),
        end=date(2026, 11, 26),
        lines=[
            PurchasePlanLine(
                id=uuid4(),
                name="gulrot",
                canonical_product_id=carrot_id,
                unit="kg",
                required_quantity=Decimal("1"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("1"),
                origins=["Test recipe"],
            )
        ],
        suggestions=[],
    )
    inventory = SimpleNamespace(plan=lambda _s, _e: plan)

    products = [
        ProviderProduct(
            provider="oda",
            product_id="sku-excluded",
            name="Gulrot Bad Brand 1 kg",
            description="1 kg",
            identity_status="compatible",
        ),
        ProviderProduct(
            provider="oda",
            product_id="sku-neutral",
            name="Gulrot Vanlig 1 kg",
            description="1 kg",
            identity_status="compatible",
        ),
        ProviderProduct(
            provider="oda",
            product_id="sku-pref-2",
            name="Gulrot Nr 2 1 kg",
            description="1 kg",
            identity_status="compatible",
        ),
        ProviderProduct(
            provider="oda",
            product_id="sku-pref-1",
            name="Gulrot Favoritt 1 kg",
            description="1 kg",
            identity_status="compatible",
        ),
    ]
    provider = _FakeProvider(products)

    service = BasketMatchingService(
        inventory=inventory,
        provider=provider,
        preferences_provider=lambda pid: (
            (["sku-pref-1", "sku-pref-2"], ["sku-excluded"]) if pid == carrot_id else ([], [])
        ),
    )

    result = await service.find_products("malo", plan.start, plan.end, plan=plan, size=5)
    assert len(result.needs) == 1
    candidates = result.needs[0].candidates
    ids = [c.product_id for c in candidates]
    assert "sku-excluded" not in ids
    assert ids == ["sku-pref-1", "sku-pref-2", "sku-neutral"]
    assert "Foretrukket Oda-vare (rang 1)" in candidates[0].match_reason
    assert "Foretrukket Oda-vare (rang 2)" in candidates[1].match_reason
    assert "Foretrukket Oda-vare" not in candidates[2].match_reason
