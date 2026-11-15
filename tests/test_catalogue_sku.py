"""Household SKU memory keeps exact identity, preferences and purchases distinct."""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from home_kitchen.catalogue.models import food_catalogue_id
from home_kitchen.catalogue.repository import PostgresFoodCatalogueRepository
from home_kitchen.catalogue.service import FoodCatalogueService
from home_kitchen.catalogue.sku_models import ExplicitPurchaseEvidence, RetailerProductSnapshot
from home_kitchen.catalogue.sku_repository import PostgresCatalogueSkuRepository
from home_kitchen.catalogue.sku_service import CatalogueSkuService


@pytest.mark.skipif(
    not os.environ.get("KITCHEN_TEST_DATABASE_URL"),
    reason="requires the explicitly configured disposable PostgreSQL test database",
)
def test_household_sku_memory_requires_explicit_preference_and_purchase_evidence(test_database_url):
    engine = create_engine(test_database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM kitchen_catalogue_sku_options")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM kitchen_catalogue_sku_purchases")
                ).scalar_one()
                == 0
            )

        service = CatalogueSkuService(
            FoodCatalogueService(PostgresFoodCatalogueRepository(engine)),
            PostgresCatalogueSkuRepository(engine),
        )
        food_id = food_catalogue_id("01.291")
        household_a, household_b = uuid4(), uuid4()
        snapshot = RetailerProductSnapshot(
            provider="test-shop",
            product_id="sku-001",
            name="Melk 1 L",
            url="https://shop.example/products/sku-001",
            raw_pack={"label": "1 l"},
            quantity=1,
            quantity_unit="l",
            snapshot_ref="test-catalogue-revision-1",
            observed_at=datetime.now(UTC),
            evidence_status="provider_verified",
        )
        remembered = service.remember_selection(household_a, food_id, snapshot, actor_id="member-a")
        assert remembered.preferred is False
        assert remembered.snapshot.evidence_status == "unverified"
        assert service.list_selections(household_a, remembered.id)[0]["actor_id"] == "member-a"
        assert service.list_purchases(household_a, remembered.id) == []
        assert service.list_options(household_b, food_id) == []

        preferred = service.set_preferred(household_a, remembered.id, actor_id="member-a")
        assert preferred.preferred is True
        assert service.list_options(household_b, food_id) == []
        assert (
            service.unset_preferred(household_a, remembered.id, actor_id="member-b").preferred
            is False
        )
        assert [
            event["action"] for event in service.list_preference_events(household_a, remembered.id)
        ] == ["set", "unset"]
        assert service.list_preference_events(household_b, remembered.id) == []

        purchase_id = service.record_purchase(
            household_a,
            remembered.id,
            remembered.snapshot_id,
            ExplicitPurchaseEvidence(
                evidence_kind="retailer_order",
                evidence_ref="order-123",
                purchased_at=datetime.now(UTC),
                quantity=2,
                quantity_unit="l",
            ),
            actor_id="member-a",
        )
        purchases = service.list_purchases(household_a, remembered.id)
        assert [purchase["id"] for purchase in purchases] == [purchase_id]
        assert purchases[0]["evidence_ref"] == "order-123"
        assert service.list_purchases(household_b, remembered.id) == []

        # An identical exact provider/product mapping is reused; its newer observation
        # is a new snapshot and a separate selection event, not another SKU or purchase.
        updated = service.remember_selection(
            household_a,
            food_id,
            snapshot.model_copy(update={"name": "Melk 1 L, ny etikett"}),
            actor_id="member-a",
        )
        assert updated.id == remembered.id
        assert updated.snapshot_id != remembered.snapshot_id
        assert len(service.list_options(household_a, food_id)) == 1
        assert len(service.list_selections(household_a, remembered.id)) == 2
        assert len(service.list_purchases(household_a, remembered.id)) == 1

        variant = service.remember_selection(
            household_a,
            food_id,
            snapshot.model_copy(update={"product_id": "sku-002", "name": "Laktosefri melk 1 L"}),
            actor_id="member-a",
        )
        assert variant.id != remembered.id
        assert len(service.list_options(household_a, food_id)) == 2
        service.set_preferred(household_a, remembered.id, actor_id="member-a")
        service.set_preferred(household_a, variant.id, actor_id="member-b")
        assert all(option.preferred for option in service.list_options(household_a, food_id))
        with pytest.raises(KeyError, match="household"):
            service.set_preferred(household_b, variant.id, actor_id="member-b")
    finally:
        engine.dispose()


def test_snapshot_and_purchase_commands_reject_ambiguous_facts():
    with pytest.raises(ValueError, match="quantity and quantity_unit"):
        RetailerProductSnapshot(
            provider="store",
            product_id="1",
            name="Milk",
            quantity=1,
            observed_at=datetime.now(UTC),
        )
    with pytest.raises(ValueError, match="evidence_ref"):
        ExplicitPurchaseEvidence(
            evidence_kind="receipt",
            evidence_ref=" ",
            purchased_at=datetime.now(UTC),
        )
    with pytest.raises(ValueError, match="future"):
        ExplicitPurchaseEvidence(
            evidence_kind="receipt",
            evidence_ref="receipt-1",
            purchased_at=datetime(2999, 1, 1, tzinfo=UTC),
        )
