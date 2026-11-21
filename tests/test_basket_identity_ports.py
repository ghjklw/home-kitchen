"""Unit tests for explicit basket matching and receipt mapping ports/adapters."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from home_kitchen.basket.adapters import (
    CanonicalIdentityCorrectionAdapter,
    CanonicalIdentityLookupAdapter,
    ProductOverviewSkuPreferenceAdapter,
)
from home_kitchen.basket.matching import (
    BasketMatchingService,
    ProviderProduct,
    ProviderSearchGroup,
)
from home_kitchen.basket.ports import (
    IdentityCorrectionPort,
    IdentityLookupPort,
    SkuPreferencePort,
)
from home_kitchen.catalogue.adapters import CatalogueReceiptMappingAdapter
from home_kitchen.catalogue.identity import (
    AliasResolution,
    CanonicalProduct,
    IdentityCorrectionCitation,
    IdentityCorrectionEvent,
)
from home_kitchen.features import KitchenFeatures
from home_kitchen.inventory import PurchasePlan, PurchasePlanLine
from home_kitchen.purchase_sync import (
    ProviderOrderRecord,
    PurchaseHistorySyncService,
    enrich_and_map_receipt_record,
)
from home_kitchen.settings import Household, Settings


def test_product_overview_sku_preference_adapter_returns_ids_and_handles_missing():
    known_id = uuid4()
    missing_id = uuid4()

    class _Overview:
        def get_preferences(self, product_id):
            if product_id == missing_id:
                raise KeyError(product_id)
            return (
                [
                    SimpleNamespace(oda_product_id="pref-1"),
                    SimpleNamespace(oda_product_id="pref-2"),
                ],
                [SimpleNamespace(oda_product_id="excl-1")],
            )

    adapter = ProductOverviewSkuPreferenceAdapter(_Overview())  # type: ignore[arg-type]
    assert adapter.get_sku_preferences(known_id) == (["pref-1", "pref-2"], ["excl-1"])
    assert adapter.get_sku_preferences(missing_id) == ([], [])


def test_canonical_identity_lookup_adapter_resolves_and_falls_back_without_household_kwarg():
    product_id = uuid4()
    household_id = uuid4()
    calls: list[tuple[str, str, str, str, object]] = []

    class _IdentitiesWithHousehold:
        def resolve(self, kind, loc, ctx, name, *, household_id=None):
            calls.append((kind, loc, ctx, name, household_id))
            if loc == "en" and ctx == "receipt" and name == "carrot":
                return AliasResolution(
                    status="resolved",
                    candidates=[CanonicalProduct(id=product_id, canonical_name="carrot")],
                )
            return AliasResolution(status="unmapped", candidates=[])

    adapter = CanonicalIdentityLookupAdapter(
        _IdentitiesWithHousehold(),  # type: ignore[arg-type]
        household_id=household_id,
    )
    assert adapter.resolve_product_id("carrot") == product_id
    assert all(call[4] == household_id for call in calls)
    assert adapter.resolve_product_id("unknown") is None

    class _LegacyIdentities:
        def resolve(self, kind, loc, ctx, name):
            if loc == "nb" and ctx == "recipe" and name == "gulrot":
                return AliasResolution(
                    status="resolved",
                    candidates=[CanonicalProduct(id=product_id, canonical_name="carrot")],
                )
            return AliasResolution(status="ambiguous", candidates=[])

    legacy_adapter = CanonicalIdentityLookupAdapter(
        _LegacyIdentities(),  # type: ignore[arg-type]
        household_id=household_id,
    )
    assert legacy_adapter.resolve_product_id("gulrot") == product_id


def test_canonical_identity_correction_adapter_filters_and_falls_back_to_recent_events():
    product_id = uuid4()
    other_id = uuid4()
    household_id = uuid4()
    matching_event = IdentityCorrectionEvent(
        id=uuid4(),
        occurred_at=datetime(2026, 11, 20, tzinfo=UTC),
        household_id=household_id,
        actor_id="malo",
        channel="mcp",
        kind="product",
        locale="nb",
        context="recipe",
        normalized_alias="gulrot",
        alias="gulrot",
        new_identity_id=product_id,
    )
    unrelated_event = matching_event.model_copy(update={"id": uuid4(), "new_identity_id": other_id})
    recent_event = matching_event.model_copy(
        update={"id": uuid4(), "alias": "gulrøtter", "normalized_alias": "gulrøtter"}
    )

    class _Identities:
        def list_correction_events(self, *, kind=None, alias=None, household_id=None, limit=50):
            if alias == "gulrot":
                return [matching_event, unrelated_event, "ignore-me"]
            if alias is None:
                return [unrelated_event, recent_event]
            return []

    adapter = CanonicalIdentityCorrectionAdapter(
        _Identities(),  # type: ignore[arg-type]
        household_id=household_id,
    )
    direct = adapter.get_identity_corrections("gulrot", product_id)
    assert [c.event_id for c in direct] == [matching_event.id]

    fallback = adapter.get_identity_corrections("unseen-alias", product_id)
    assert [c.event_id for c in fallback] == [recent_event.id]


@pytest.mark.asyncio
async def test_basket_matching_service_accepts_explicit_port_implementations():
    carrot_id = uuid4()
    citation = IdentityCorrectionCitation(
        event_id=uuid4(),
        kind="product",
        alias="gulrot",
        locale="nb",
        context="recipe",
        new_identity_id=carrot_id,
        actor_id="malo",
        channel="api",
        source_type="reviewed_correction",
        occurred_at=datetime(2026, 11, 20, tzinfo=UTC),
    )

    class _Prefs(SkuPreferencePort):
        def get_sku_preferences(self, product_id):
            assert product_id == carrot_id
            return (["sku-1"], ["sku-bad"])

    class _Lookup(IdentityLookupPort):
        def resolve_product_id(self, name):
            assert name == "gulrot"
            return carrot_id

    class _Corrections(IdentityCorrectionPort):
        def get_identity_corrections(self, need_name, product_id):
            assert need_name == "gulrot"
            assert product_id == carrot_id
            return [citation]

    plan = PurchasePlan(
        start=date(2026, 11, 20),
        end=date(2026, 11, 26),
        lines=[
            PurchasePlanLine(
                id=uuid4(),
                name="gulrot",
                unit="kg",
                required_quantity=Decimal("1"),
                available_quantity=Decimal("0"),
                covered_quantity=Decimal("0"),
                to_buy_quantity=Decimal("1"),
                origins=["Suppe"],
            )
        ],
        suggestions=[],
    )
    inventory = SimpleNamespace(plan=lambda _s, _e: plan)

    class _Provider:
        async def search_products(self, actor_id, queries, page=1, size=5):
            return [
                ProviderSearchGroup(
                    query=q,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="sku-bad",
                            name="Gulrot Bad 1 kg",
                            description="1 kg",
                            identity_status="compatible",
                        ),
                        ProviderProduct(
                            provider="oda",
                            product_id="sku-1",
                            name="Gulrot Favoritt 1 kg",
                            description="1 kg",
                            identity_status="compatible",
                        ),
                    ],
                )
                for q in queries
            ]

    service = BasketMatchingService(
        inventory=inventory,  # type: ignore[arg-type]
        provider=_Provider(),
        sku_preferences=_Prefs(),
        identity_lookup=_Lookup(),
        identity_corrections=_Corrections(),
    )
    result = await service.find_products("malo", plan.start, plan.end, plan=plan, size=5)
    assert len(result.needs) == 1
    need = result.needs[0]
    assert [c.product_id for c in need.candidates] == ["sku-1"]
    assert need.sku_preference_evidence is not None
    assert need.sku_preference_evidence.preferred_product_ids == ["sku-1"]
    assert need.sku_preference_evidence.excluded_product_ids == ["sku-bad"]
    assert [c.event_id for c in need.identity_correction_citations] == [citation.event_id]


@pytest.mark.asyncio
async def test_catalogue_receipt_mapping_adapter_and_purchase_sync_injection():
    prod_id = uuid4()
    unit_id = uuid4()

    class _IdentityResolver:
        def resolve(self, kind, locale, context, alias):
            if kind == "product" and locale == "nb" and context == "receipt" and alias == "gulrot":
                return SimpleNamespace(status="resolved", candidates=[SimpleNamespace(id=prod_id)])
            if kind == "unit" and locale == "nb" and context == "receipt" and alias == "kg":
                return SimpleNamespace(status="resolved", candidates=[SimpleNamespace(id=unit_id)])
            return SimpleNamespace(status="unmapped", candidates=[])

    record = ProviderOrderRecord(
        external_id="ord-1",
        revision_id="rev-1",
        payload={"lines": [{"name": "gulrot", "unit": "kg"}, {"name": "ukjent vare"}]},
    )
    unmapped = enrich_and_map_receipt_record(record, None, _IdentityResolver())
    assert record.payload["lines"][0]["canonical_product_id"] == str(prod_id)
    assert record.payload["lines"][0]["canonical_unit_id"] == str(unit_id)
    assert len(unmapped) == 1
    assert unmapped[0]["name"] == "ukjent vare"

    fake_store = SimpleNamespace(engine=None)
    fake_identities = MagicMock()
    settings = Settings(local_dev=True)
    mapper = CatalogueReceiptMappingAdapter(fake_store, fake_identities)
    sync_service = PurchaseHistorySyncService(
        settings=settings,
        identity_service=_IdentityResolver(),
        receipt_mapper=mapper,
    )
    assert sync_service.receipt_mapper is mapper
    assert not hasattr(sync_service, "mapping_store")

    with patch(
        "home_kitchen.catalogue.adapters.auto_map_unmapped_receipt_items_async",
        new=AsyncMock(return_value={"processed_unique": 1}),
    ) as mock_map:
        result = await mapper.map_unmapped_items(unmapped, settings=settings)
        assert result == {"processed_unique": 1}
        mock_map.assert_awaited_once_with(
            fake_store, settings, unmapped, identity_service=fake_identities
        )


def test_kitchen_features_wires_explicit_ports_without_monkey_patching():
    store = SimpleNamespace(
        engine=None,
        ensure_setting=lambda _k, default: default,
        photo_household_id=None,
    )
    identities = MagicMock()
    kitchen = SimpleNamespace(store=store, identities=identities, household_id=None, settings=None)
    features = KitchenFeatures(
        kitchen=kitchen,  # type: ignore[arg-type]
        settings=Settings(local_dev=True),
        household=lambda: Household(name="Test"),
    )
    assert isinstance(features.basket_matching.sku_preferences, ProductOverviewSkuPreferenceAdapter)
    assert isinstance(features.basket_matching.identity_lookup, CanonicalIdentityLookupAdapter)
    assert isinstance(
        features.basket_matching.identity_corrections, CanonicalIdentityCorrectionAdapter
    )
    assert features.purchase_sync.identity_service is identities
    assert isinstance(features.purchase_sync.receipt_mapper, CatalogueReceiptMappingAdapter)
    assert not hasattr(features.purchase_sync, "mapping_store")
