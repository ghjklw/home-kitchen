"""Focused tests for retailer-independent product mapping and package suggestions."""

from __future__ import annotations

import threading
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from home_kitchen.product_mapping import (
    ProductIdentitySuggestion,
    ProductMappingMutation,
    ProductMappingRepository,
    ProductMappingService,
    _match_mapping,
    infer_package_from_description,
    source_line_metadata,
)
from home_kitchen.purchase_history_view import (
    PurchaseHistoryBrowseResponse,
    PurchaseTableRow,
    resolve_purchase_category,
)


@pytest.fixture
def mapping_repository():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE kitchen_retailer_product_mappings ("
                "id TEXT PRIMARY KEY, retailer TEXT NOT NULL, retailer_product_id TEXT, ean TEXT, "
                "description TEXT NOT NULL, normalized_description TEXT NOT NULL, "
                "description_hash TEXT NOT NULL, canonical_product_id TEXT, suggested_product_id TEXT, "
                "suggested_product_name TEXT, suggested_category TEXT, product_evidence TEXT, "
                "category_evidence TEXT, mapping_source TEXT, mapping_confidence FLOAT, "
                "review_status TEXT NOT NULL, category_source TEXT, category_confidence FLOAT, "
                "category_review_status TEXT NOT NULL, package_size NUMERIC, package_unit TEXT, "
                "package_inference_source TEXT, package_confidence FLOAT, package_evidence TEXT, "
                "apply_to_similar BOOLEAN NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "UNIQUE(retailer, description_hash))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE kitchen_retailer_product_mapping_events ("
                "id TEXT PRIMARY KEY, mapping_id TEXT NOT NULL, occurred_at TEXT NOT NULL, "
                "actor_id TEXT NOT NULL, channel TEXT NOT NULL, action TEXT NOT NULL, scope TEXT NOT NULL, "
                "previous_state TEXT NOT NULL, new_state TEXT NOT NULL)"
            )
        )
    yield ProductMappingRepository(engine)
    engine.dispose()


def _mapping_values(product_id, *, status="confirmed"):
    return {
        "canonical_product_id": product_id,
        "suggested_product_id": None,
        "suggested_product_name": None,
        "suggested_category": None,
        "product_evidence": None,
        "category_evidence": None,
        "mapping_source": "user",
        "mapping_confidence": 1.0,
        "review_status": status,
        "category_source": None,
        "category_confidence": None,
        "category_review_status": "pending",
        "package_size": None,
        "package_unit": None,
        "package_inference_source": None,
        "package_confidence": None,
        "package_evidence": None,
    }


def test_package_parser_separates_pack_size_from_purchased_quantity():
    assert infer_package_from_description("COOP SUPERBRØD 750G") == {
        "package_size": Decimal("750"),
        "package_unit": "g",
        "package_inference_source": "description_rule",
        "package_confidence": 0.99,
    }
    assert (
        infer_package_from_description("ÄNGLAMARK BLEIER JUNIOR STR 5 44STK")["package_unit"]
        == "stk"
    )
    assert infer_package_from_description("ÄNGLAMARK LETTMELK 0.5% 1L")["package_unit"] == "l"
    assert infer_package_from_description("Kjøpt 2 til 39,90 per kg") is None


def test_category_fallback_does_not_match_lam_inside_anglamark():
    assert resolve_purchase_category(product_name="ÄNGLAMARK BLEIER JUNIOR STR 5 44STK") == (
        "Other",
        "Annet",
    )


def test_source_line_metadata_reads_oda_product_id_and_ean():
    assert source_line_metadata(
        "oda",
        {
            "order": {
                "products": [
                    {
                        "product": {
                            "id": "oda-product-1",
                            "name": "Milk 1L",
                            "ean": "7038010001000",
                        }
                    }
                ]
            }
        },
        0,
    ) == {"retailer_product_id": "oda-product-1", "ean": "7038010001000"}


def test_near_similar_description_does_not_inherit_product_mapping(mapping_repository):
    product_id = uuid4()
    mapping_repository.upsert(
        retailer="coop",
        retailer_product_id="coop-12",
        ean=None,
        description="Änglamark lettmelk 0.5% 1L",
        values=_mapping_values(product_id),
        actor_id="tester",
        channel="api",
        action="remap",
        apply_to_similar=True,
    )

    assert mapping_repository.find("coop", "coop-12", None, "Totalt endret produktnavn") is not None
    assert mapping_repository.find("coop", None, None, "Änglamark lettmelk 0.5% 1,5L") is None
    assert mapping_repository.find("coop", None, None, "Änglamark lettmelk 0.5% 1L Extra") is None
    cross_retailer = mapping_repository.find("rema1000", None, None, "ÄNGLAMARK LETTMELK 0.5% 1L")
    assert cross_retailer is not None
    assert cross_retailer["mapping_match_source"] == "opted_in_exact_description"
    assert mapping_repository.find("rema1000", None, None, "Änglamark lettmelk 1.5% 1L") is None


@pytest.mark.parametrize(
    (
        "match_source",
        "stored_retailer",
        "stored_product_id",
        "stored_ean",
        "stored_description",
        "apply_to_similar",
        "target_retailer",
        "target_product_id",
        "target_ean",
        "target_description",
    ),
    [
        (
            "retailer_product_id",
            "coop",
            "coop-item-1",
            None,
            "Stored Coop description",
            False,
            "coop",
            "coop-item-1",
            None,
            "Different current description",
        ),
        (
            "ean",
            "rema1000",
            "rema-item-1",
            "7038010001000",
            "Stored Rema description",
            False,
            "coop",
            None,
            "7038010001000",
            "Different current description",
        ),
        (
            "exact_description",
            "coop",
            None,
            None,
            "Same exact description",
            False,
            "coop",
            None,
            None,
            "Same exact description",
        ),
        (
            "opted_in_exact_description",
            "rema1000",
            None,
            None,
            "Same exact description",
            True,
            "coop",
            None,
            None,
            "Same exact description",
        ),
    ],
)
def test_mapping_match_source_reports_resolution_key(
    mapping_repository,
    match_source,
    stored_retailer,
    stored_product_id,
    stored_ean,
    stored_description,
    apply_to_similar,
    target_retailer,
    target_product_id,
    target_ean,
    target_description,
):
    mapping_repository.upsert(
        retailer=stored_retailer,
        retailer_product_id=stored_product_id,
        ean=stored_ean,
        description=stored_description,
        values=_mapping_values(uuid4()),
        actor_id="user:test",
        channel="test",
        action="remap",
        apply_to_similar=apply_to_similar,
    )

    from_repository = mapping_repository.find(
        target_retailer,
        target_product_id,
        target_ean,
        target_description,
    )
    from_history = _match_mapping(
        mapping_repository.list_all(),
        target_retailer,
        target_product_id,
        target_ean,
        target_description,
    )

    assert from_repository["mapping_match_source"] == match_source
    assert from_history["mapping_match_source"] == match_source


def test_exact_ean_mapping_is_cross_retailer_and_requires_review(mapping_repository):
    product_id = uuid4()
    mapping_repository.upsert(
        retailer="coop",
        retailer_product_id=None,
        ean="7038010001000",
        description="Coop melk 1L",
        values=_mapping_values(product_id),
        actor_id="tester",
        channel="api",
        action="remap",
        apply_to_similar=False,
    )
    resolved = mapping_repository.find(
        "rema1000", None, "7038010001000", "Different retailer title"
    )
    assert resolved is not None
    assert resolved["canonical_product_id"] == str(product_id)

    mapping_repository.upsert(
        retailer="coop",
        retailer_product_id=None,
        ean="7038010002000",
        description="Other item",
        values=_mapping_values(product_id, status="pending"),
        actor_id="system:ai_suggestion",
        channel="system",
        action="ai_suggestion",
        apply_to_similar=False,
    )
    assert mapping_repository.find("rema1000", None, "7038010002000", "Other item") is None


def test_non_food_ai_suggestion_is_valid_without_canonical_food_product():
    suggestion = ProductIdentitySuggestion(
        description="ÄNGLAMARK BLEIER JUNIOR STR 5 44STK",
        suggested_category="non_food",
        product_confidence=0.94,
        category_confidence=0.98,
        product_evidence="Disposable diapers are a household non-food item.",
        category_evidence="The title explicitly says diapers.",
    )
    assert suggestion.suggested_existing_product_id is None
    assert suggestion.suggested_product_name is None


def test_mapping_mutation_is_audited(mapping_repository):
    product_id = uuid4()
    result = mapping_repository.upsert(
        retailer="coop",
        retailer_product_id="42",
        ean=None,
        description="COOP SUPERBRØD 750G",
        values=_mapping_values(product_id),
        actor_id="person@example.test",
        channel="app",
        action="remap",
        apply_to_similar=True,
    )
    assert result["review_status"] == "confirmed"
    assert result["canonical_product_id"] == str(product_id)
    with mapping_repository.engine.connect() as connection:
        event = connection.execute(
            text("SELECT actor_id, action, scope FROM kitchen_retailer_product_mapping_events")
        ).one()
    assert event == ("person@example.test", "remap", "description_and_explicit_similar")


def test_existing_purchase_rows_receive_mapping_overlay_without_resync(mapping_repository):
    product_id = uuid4()
    mapping_repository.upsert(
        retailer="coop",
        retailer_product_id=None,
        ean=None,
        description="COOP SUPERBRØD 750G",
        values=_mapping_values(product_id),
        actor_id="tester",
        channel="api",
        action="remap",
        apply_to_similar=False,
    )

    class ProductRepository:
        def get_product(self, identity_id):
            return type("Product", (), {"canonical_name": "brød"})()

    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.product_repository = ProductRepository()
    service.settings = None
    service._background_lock = threading.Lock()
    service._background_descriptions = set()
    response = PurchaseHistoryBrowseResponse(
        rows=[
            PurchaseTableRow(
                id="coop:receipt-1:line:0",
                row_id="coop:receipt-1:line:0",
                basket_id="coop:receipt-1",
                provider="coop",
                provider_label="Coop",
                product_name="COOP SUPERBRØD 750G",
                raw_name="COOP SUPERBRØD 750G",
                quantity=Decimal("2"),
                unit="stk",
            )
        ]
    )

    result = service.enrich_purchase_history(response)
    row = result.rows[0]
    assert row.base_product_id == str(product_id)
    assert row.base_product_name == "brød"
    assert row.package_size == Decimal("750")
    assert row.package_unit == "g"
    assert row.quantity == Decimal("2")
    assert row.unit == "stk"
    assert row.mapping_review_status == "confirmed"


def test_mapping_enrichment_accepts_table_rows_with_product_name(mapping_repository):
    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.product_repository = SimpleNamespace(get_product=lambda _identity_id: None)
    service.settings = None
    service._background_lock = threading.Lock()
    service._background_descriptions = set()
    response = PurchaseHistoryBrowseResponse(
        rows=[
            PurchaseTableRow(
                id="oda:order-1:line:0",
                basket_id="oda:order-1",
                provider="oda",
                provider_label="Oda",
                product_name="Oda product title",
                raw_name=None,
            )
        ]
    )

    result = service.enrich_purchase_history(response)

    assert result.rows[0].product_name == "Oda product title"
    assert result.rows[0].mapping_match == "none"


def test_ai_mapping_overlay_stays_suggestion_until_confirmed(mapping_repository):
    mapping_repository.upsert(
        retailer="coop",
        retailer_product_id=None,
        ean=None,
        description="ÄNGLAMARK BLEIER JUNIOR STR 5 44STK",
        values={
            **_mapping_values(None, status="pending"),
            "suggested_product_name": "bleier",
            "suggested_category": "non_food",
            "product_evidence": "Disposable diaper product description.",
            "category_evidence": "Non-food household product.",
            "mapping_source": "ai",
            "mapping_confidence": 0.93,
            "category_source": "ai",
            "category_confidence": 0.97,
        },
        actor_id="system:ai_suggestion",
        channel="system",
        action="ai_suggestion",
        apply_to_similar=False,
    )
    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.product_repository = SimpleNamespace(get_product=lambda _identity_id: None)
    service.settings = None
    service._background_lock = threading.Lock()
    service._background_descriptions = set()
    response = PurchaseHistoryBrowseResponse(
        rows=[
            PurchaseTableRow(
                id="coop:receipt-2:line:0",
                row_id="coop:receipt-2:line:0",
                basket_id="coop:receipt-2",
                provider="coop",
                provider_label="Coop",
                product_name="ÄNGLAMARK BLEIER JUNIOR STR 5 44STK",
                raw_name="ÄNGLAMARK BLEIER JUNIOR STR 5 44STK",
            )
        ]
    )

    row = service.enrich_purchase_history(response).rows[0]
    assert row.base_product_id is None
    assert row.base_product_name is None
    assert row.suggested_product_name == "bleier"
    assert row.suggested_category == "non_food"
    assert row.mapping_review_status == "pending"
    assert row.mapping_confidence == 0.93
    assert row.category_review_status == "pending"


def test_ai_existing_product_id_resolves_suggestion_name_without_confirming(mapping_repository):
    product_id = uuid4()
    mapping_repository.upsert(
        retailer="coop",
        retailer_product_id=None,
        ean=None,
        description="COOP GROVBRØD",
        values={
            **_mapping_values(None, status="pending"),
            "suggested_product_id": product_id,
            "suggested_product_name": None,
            "suggested_category": "Bakery",
            "product_evidence": "The description identifies a loaf of bread.",
            "category_evidence": "Bread belongs to the bakery category.",
            "mapping_source": "ai",
            "mapping_confidence": 0.91,
            "category_source": "ai",
            "category_confidence": 0.88,
        },
        actor_id="system:ai_suggestion",
        channel="system",
        action="ai_suggestion",
        apply_to_similar=False,
    )
    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.product_repository = SimpleNamespace(
        get_product=lambda requested_id: (
            SimpleNamespace(canonical_name="Grovbrød") if requested_id == product_id else None
        )
    )
    service.settings = None
    service._background_lock = threading.Lock()
    service._background_descriptions = set()
    response = PurchaseHistoryBrowseResponse(
        rows=[
            PurchaseTableRow(
                id="coop:receipt-3:line:0",
                row_id="coop:receipt-3:line:0",
                basket_id="coop:receipt-3",
                provider="coop",
                provider_label="Coop",
                product_name="COOP GROVBRØD",
                raw_name="COOP GROVBRØD",
            )
        ]
    )

    row = service.enrich_purchase_history(response).rows[0]
    assert row.base_product_id is None
    assert row.base_product_name is None
    assert row.suggested_product_name == "Grovbrød"
    assert row.suggested_category == "Bakery"
    assert row.product_evidence == "The description identifies a loaf of bread."
    assert row.category_evidence == "Bread belongs to the bakery category."
    assert row.mapping_match_source == "exact_description"
    assert row.mapping_confidence == 0.91
    assert row.category_confidence == 0.88
    assert row.mapping_review_status == "pending"


@pytest.mark.asyncio
async def test_reviewed_remap_and_package_edit_are_persisted_together(mapping_repository):
    canonical_id = uuid4()
    description = "COOP SUPERBRØD 750G"
    mapping_repository.upsert(
        retailer="rema1000",
        retailer_product_id="rema-bread-750",
        ean=None,
        description=description,
        values=_mapping_values(None, status="pending"),
        actor_id="system:ai_suggestion",
        channel="system",
        action="ai_suggestion",
        apply_to_similar=False,
    )
    line = SimpleNamespace(
        name=description,
        raw_name=description,
        ean=None,
        canonical_product_id=None,
    )
    order = SimpleNamespace(
        external_id="receipt-3",
        lines=[line],
        payload={"lines": [{"name": description, "productId": "bread-750"}]},
    )

    class IdentityService:
        def create_product(self, name):
            assert name == "wholegrain bread"
            return SimpleNamespace(id=canonical_id)

        def validate_identities(self, kind, ids):
            assert kind == "product"
            assert ids == {canonical_id}

    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.identities = IdentityService()
    service.product_repository = SimpleNamespace(get_product=lambda _identity_id: None)
    service.purchase_sync = SimpleNamespace(get_effective_orders=lambda **_kwargs: [order])
    service.settings = None

    result = await service.mutate(
        ProductMappingMutation(
            row_id="coop:receipt-3:line:0",
            action="remap",
            canonical_product_name="wholegrain bread",
            package_size=Decimal("750"),
            package_unit="g",
            apply_to_similar=True,
        ),
        actor_id="person@example.test",
        channel="app",
    )
    assert result.mapping["canonical_product_id"] == str(canonical_id)
    assert result.mapping["package_size"] == Decimal("750")
    assert result.mapping["package_unit"] == "g"
    assert result.mapping["package_inference_source"] == "user"
    assert result.mapping["review_status"] == "confirmed"
    assert result.similar_rows == 1
    reused = mapping_repository.find("rema1000", "rema-bread-750", None, description)
    assert reused is not None
    assert reused["canonical_product_id"] == str(canonical_id)
    assert reused["retailer_product_id"] == "rema-bread-750"


@pytest.mark.asyncio
async def test_import_suggestion_persists_explicit_package_without_ai(mapping_repository):
    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.product_repository = SimpleNamespace(list_products=lambda: [])
    service.settings = SimpleNamespace(ai_configured=False)

    count = await service.suggest_items(
        [
            {
                "retailer": "coop",
                "retailer_product_id": "bread-750",
                "ean": None,
                "description": "COOP SUPERBRØD 750G",
            }
        ]
    )
    row = mapping_repository.find("coop", "bread-750", None, "COOP SUPERBRØD 750G")
    assert count == 1
    assert row is not None
    assert row["package_size"] == Decimal("750")
    assert row["package_unit"] == "g"
    assert row["package_inference_source"] == "description_rule"
    assert row["review_status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "batch"])
@pytest.mark.parametrize(
    ("confidence", "expected_status"),
    [(0.9, "confirmed"), (0.899, "pending")],
)
async def test_high_confidence_existing_product_match_auto_confirms_identity_only(
    mapping_repository, monkeypatch, path, confidence, expected_status
):
    product_id = uuid4()
    description = "Oda havregryn 1kg"
    suggestion = ProductIdentitySuggestion(
        description=description,
        suggested_existing_product_id=product_id,
        suggested_category="Pantry",
        product_confidence=confidence,
        category_confidence=0.99,
        product_evidence="The existing catalogue item matches the product and size in the title.",
        category_evidence="Oats are a pantry item.",
        package_size=Decimal("1"),
        package_unit="kg",
        package_confidence=0.99,
        package_evidence="The retailer title includes 1kg.",
    )

    class SuggestionAgent:
        async def run(self, _prompt):
            return SimpleNamespace(output=SimpleNamespace(suggestions=[suggestion]))

    monkeypatch.setattr(
        "home_kitchen.product_mapping._build_identity_agent",
        lambda _settings: SuggestionAgent(),
    )
    service = ProductMappingService.__new__(ProductMappingService)
    service.repository = mapping_repository
    service.product_repository = SimpleNamespace(
        list_products=lambda: [
            SimpleNamespace(id=product_id, canonical_name="Havregryn", variant=None)
        ]
    )
    service.settings = SimpleNamespace(ai_configured=True)

    source = {
        "retailer": "oda",
        "retailer_product_id": "oats-1kg",
        "ean": None,
        "description": description,
    }
    if path == "single":
        await service._infer_row(source)
    else:
        await service.suggest_items([source])

    row = mapping_repository.find("oda", "oats-1kg", None, description)
    assert row is not None
    if expected_status == "confirmed":
        assert row["canonical_product_id"] == str(product_id)
        assert row["review_status"] == "confirmed"
    else:
        assert row["canonical_product_id"] is None
        assert row["review_status"] == "pending"
        assert row["suggested_product_id"] == str(product_id)
    assert row["mapping_confidence"] == confidence
    assert row["category_review_status"] == "pending"
    assert row["suggested_category"] == "Pantry"
    assert row["package_size"] == Decimal("1")
    assert row["package_unit"] == "kg"

    with mapping_repository.engine.connect() as connection:
        event = connection.execute(
            text(
                "SELECT actor_id, action FROM kitchen_retailer_product_mapping_events "
                "WHERE actor_id IN ('system:ai_auto_confirm', 'system:ai_suggestion') "
                "ORDER BY occurred_at DESC LIMIT 1"
            )
        ).one()
    if expected_status == "confirmed":
        assert event == ("system:ai_auto_confirm", "ai_auto_confirm")
    else:
        assert event == ("system:ai_suggestion", "ai_suggestion")
