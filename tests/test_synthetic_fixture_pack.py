"""Contract checks for original, isolated synthetic fixtures."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fixtures.synthetic_kitchen.basket import (
    SYNTHETIC_BASKET_NEED_ID,
    SYNTHETIC_UNVERIFIED_NEED_ID,
    SyntheticBasketMatchScenario,
    synthetic_basket_match_scenarios,
    synthetic_compatible_candidate,
    synthetic_incompatible_unit_candidate,
    synthetic_unverified_candidate,
)
from fixtures.synthetic_kitchen.fakes import (
    ScriptedHistoryReader,
    StaticIdentityRepository,
    StaticOdaClient,
    SyntheticReaderFailure,
)
from fixtures.synthetic_kitchen.identity import (
    SYNTHETIC_BARLEY_PRODUCT_ID,
    SYNTHETIC_CARROT_PRODUCT_ID,
    SYNTHETIC_CHICKPEAS_PRODUCT_ID,
    SYNTHETIC_CITRUS_OIL_PRODUCT_ID,
    SYNTHETIC_GRAM_UNIT_ID,
    SYNTHETIC_TBSP_UNIT_ID,
    SYNTHETIC_TOMATO_FRESH_PRODUCT_ID,
    synthetic_alias_scenarios,
    synthetic_canonical_products,
    synthetic_canonical_units,
    synthetic_reviewed_aliases,
    synthetic_unreviewed_aliases,
)
from fixtures.synthetic_kitchen.oda import (
    synthetic_oda_product_search_response,
    synthetic_oda_recipe_collection_response,
)
from fixtures.synthetic_kitchen.purchase_history import (
    coop_purchase_list_pages,
    coop_receipt_detail_revisions,
    history_failure_scenarios,
    oda_order_receipt_scenarios,
    synthetic_trumf_receipt_revisions,
    synthetic_trumf_rsc_list_html,
    synthetic_trumf_rsc_receipt_html,
    synthetic_trumf_rsc_unauthenticated_html,
    trumf_transaction_pages,
)
from fixtures.synthetic_kitchen.recipes import (
    SYNTHETIC_LINKED_RECIPE_ID,
    SYNTHETIC_RECIPE_ID,
    synthetic_linked_recipe,
    synthetic_recipe,
)

from home_kitchen.basket.jobs import BasketMatchJob, BasketMatchJobs, _Job
from home_kitchen.basket.matching import (
    NeedMatchResult,
    OdaSearchAdapter,
    _identity_status,
    _product_matches_ingredient,
)
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.models import Recipe, RecipeCreate
from home_kitchen.oda.models import OdaSearchResponse
from home_kitchen.oda.service import OdaService
from home_kitchen.recipes.collections import OdaCollectionProvider
from home_kitchen.service import KitchenService


def test_fictional_recipe_roundtrip_preserves_source_mapping_and_unknown_quantity():
    original = synthetic_recipe()
    round_trip = Recipe.model_validate(original.model_dump(mode="json"))

    assert original == round_trip
    assert round_trip.id == SYNTHETIC_RECIPE_ID
    assert round_trip.source_url is None
    assert round_trip.image_url is None
    assert [ingredient.source_indices for ingredient in round_trip.ingredients] == [
        [0],
        [1],
        [2],
        [3],
        [4],
    ]
    assert round_trip.source_ingredients[1].text == "170 g gulrot (carrot), cut into ribbons"
    assert round_trip.ingredients[-1].quantity is None
    assert round_trip.ingredients[-1].unit is None


def test_oda_product_payload_uses_current_shape_and_fake_adapter_has_no_network():
    payload = synthetic_oda_product_search_response()
    response = OdaSearchResponse.model_validate(payload)
    assert len(response.result) == 2
    assert response.result[0].products[0].id == 9900001
    assert str(response.result[0].products[0].url).startswith("https://fixtures.invalid/")
    assert response.result[1].products[0].availability is None


@pytest.mark.asyncio
async def test_fake_oda_search_and_recipe_collection_use_app_adapter_contracts():
    oda = StaticOdaClient()
    groups = await OdaSearchAdapter(cast(OdaService, oda)).search_products(
        "synthetic-household", ["havredrikk", "gulrot"], page=1, size=5
    )
    collection = await OdaCollectionProvider(cast(OdaService, oda)).page(
        "synthetic-household", "liked", page=1, size=5
    )

    assert [group.query for group in groups] == ["havredrikk", "gulrot"]
    assert [product.product_id for product in groups[0].products] == ["9900001"]
    assert collection.has_more is True
    assert collection.entries[0].id == "synthetic-oda-recipe-0001"
    assert collection.entries[0].source_url == (
        "https://fixtures.invalid/oda/recipes/synthetic-oda-recipe-0001"
    )
    assert oda.search_calls == [("synthetic-household", ("havredrikk", "gulrot"), 1, 5)]
    assert oda.collection_calls == [("synthetic-household", "liked", 1, 5)]
    assert oda.network_calls == 0


def test_oda_recipe_collection_fixture_is_fresh_and_explicitly_synthetic():
    first = synthetic_oda_recipe_collection_response()
    second = synthetic_oda_recipe_collection_response()
    assert first == second
    assert first is not second
    first["recipes"][0]["title"] = "mutated fixture"
    assert second["recipes"][0]["title"] == "TEST ONLY Barley cloud bake"
    assert second["recipes"][0]["url"].startswith("https://fixtures.invalid/")


def test_coop_pages_overlap_without_inventing_provider_pagination_fields():
    pages = coop_purchase_list_pages()
    replay = coop_purchase_list_pages()

    assert pages == replay
    assert pages[0] is not replay[0]
    assert pages[0].continuation_observed is True
    assert pages[1].continuation_observed is False
    first_response = cast(dict[str, Any], pages[0].response)
    overlap_response = cast(dict[str, Any], pages[1].response)
    first_stub = first_response["purchases"][0]
    overlap_stub = overlap_response["purchases"][0]
    assert first_stub == overlap_stub
    assert first_stub["summaryId"] == "synthetic-coop-summary-001"
    assert "receiptId" not in first_stub
    assert "hasMore" not in first_response
    assert "nextCursor" not in first_response


def test_coop_detail_revisions_keep_harness_metadata_out_of_provider_payload():
    revisions = coop_receipt_detail_revisions()
    base, corrected, refund = revisions

    assert base.external_id == corrected.external_id
    assert base.revision_id != corrected.revision_id
    assert corrected.lifecycle == "corrected"
    assert refund.lifecycle == "refund_scenario"
    summary = base.payload["summary"]
    assert summary["receiptId"] == "synthetic-coop-receipt-001"
    assert summary["lines"][0]["productName"] == "Fictional gulrot / carrot ribbons"
    assert summary["lines"][0]["quantity"] == 2
    assert summary["lines"][0]["ean13"] is None
    assert summary["lines"][1]["quantity"] is None
    assert "source_updated_at" not in summary
    assert "revision_id" not in summary
    assert "lifecycle" not in summary
    assert corrected.payload["summary"]["amount"] != summary["amount"]


def test_trumf_pages_keep_fake_ids_amounts_and_unknown_quantities():
    pages = trumf_transaction_pages()
    transactions = cast(list[dict[str, Any]], pages[0].response)
    overlap = cast(list[dict[str, Any]], pages[1].response)
    details = cast(dict[str, Any], pages[2].response)

    assert transactions[0] == overlap[0]
    assert transactions[0]["batchid"] == "synthetic-trumf-batch-001"
    assert transactions[0]["dato"].startswith("2026-04-08")
    assert transactions[0]["belop"] == 92.80
    assert details["varelinjer"][0]["vareTekst"] == ("Fictional gulrotbånd / carrot ribbons")
    assert details["varelinjer"][0]["antall"] == 2
    assert details["varelinjer"][1]["antall"] is None
    assert "source_updated_at" not in transactions[0]


def test_trumf_rsc_fixtures_and_revisions_structure():
    list_html = synthetic_trumf_rsc_list_html()
    assert "transactionsData" in list_html
    assert "synthetic-trumf-b-100" in list_html
    assert "$D2026-09-05" in list_html

    receipt_html = synthetic_trumf_rsc_receipt_html()
    assert "purchaseDetails" in receipt_html
    assert "FIKTIV HELMELK 1L" in receipt_html
    assert "$undefined" in receipt_html

    unauth = synthetic_trumf_rsc_unauthenticated_html()
    assert "Logg inn" in unauth
    assert "transactionsData" not in unauth

    revisions = synthetic_trumf_receipt_revisions()
    base, corrected, stub, refund = revisions

    assert base.provider == "trumf"
    assert base.external_id == "synthetic-trumf-b-100"
    assert base.lifecycle == "completed"
    assert corrected.external_id == base.external_id
    assert corrected.lifecycle == "corrected"
    assert corrected.revision_id != base.revision_id
    assert stub.lifecycle == "completed"
    assert stub.payload["hasReceipt"] is False
    assert refund.lifecycle == "refund_scenario"
    assert refund.payload["belop"] < 0


def test_oda_order_scenarios_keep_internal_lifecycle_separate_from_payload():
    scenarios = oda_order_receipt_scenarios()
    pending, corrected, refund = scenarios

    assert pending.external_id == corrected.external_id
    assert pending.revision_id != corrected.revision_id
    assert pending.lifecycle == "pending"
    assert corrected.lifecycle == "corrected"
    assert refund.lifecycle == "refund_scenario"
    line = pending.payload["lines"][0]
    assert line["sourceName"] == "Fictional gulrotbånd / carrot ribbons"
    assert line["unit"] == "stk"
    assert pending.payload["lines"][1]["quantity"] is None
    assert "lifecycle" not in pending.payload
    assert "revision_id" not in pending.payload


def test_scripted_history_reader_is_network_free_and_returns_fresh_pages():
    reader = ScriptedHistoryReader(coop_purchase_list_pages())
    first = reader.page(0)
    cast(dict[str, Any], first.response)["purchases"][0]["amount"] = 0
    replayed = reader.page(0)

    assert cast(dict[str, Any], replayed.response)["purchases"][0]["amount"] == 86.75
    assert reader.calls == 2
    assert reader.network_calls == 0


@pytest.mark.parametrize("scenario", history_failure_scenarios())
def test_scripted_history_reader_emits_failure_class_without_network(scenario):
    reader = ScriptedHistoryReader((), failure=scenario)

    with pytest.raises(SyntheticReaderFailure) as error:
        reader.page(0)

    assert error.value.provider == scenario.provider
    assert error.value.kind == scenario.kind
    assert reader.network_calls == 0


def test_canonical_identity_fixtures_schema_and_isolation():
    products = synthetic_canonical_products()
    units = synthetic_canonical_units()
    reviewed = synthetic_reviewed_aliases()
    unreviewed = synthetic_unreviewed_aliases()

    assert len(products) == 9
    assert len(units) == 4
    assert len(reviewed) >= 12
    assert len(unreviewed) >= 2

    # Verify deterministic UUIDs and required properties
    product_map = {p.id: p for p in products}
    assert SYNTHETIC_CARROT_PRODUCT_ID in product_map
    assert product_map[SYNTHETIC_CARROT_PRODUCT_ID].canonical_name == "carrot"
    assert product_map[SYNTHETIC_CARROT_PRODUCT_ID].variant == ""
    assert product_map[SYNTHETIC_CHICKPEAS_PRODUCT_ID].variant == "canned"

    unit_map = {u.id: u for u in units}
    assert SYNTHETIC_GRAM_UNIT_ID in unit_map
    assert unit_map[SYNTHETIC_GRAM_UNIT_ID].canonical_name == "g"

    # Isolation: mutating a returned model does not mutate subsequent calls
    products[0].canonical_name = "mutated-name"  # type: ignore[assignment]
    fresh_products = synthetic_canonical_products()
    assert fresh_products[0].canonical_name != "mutated-name"


def test_static_identity_repository_resolves_scenarios_and_handles_reviewed_corrections():
    repository = StaticIdentityRepository()
    service = CanonicalIdentityService(repository)

    # Validate each pre-authored alias scenario
    for scenario in synthetic_alias_scenarios():
        resolution = service.resolve(
            scenario.kind, scenario.locale, scenario.context, scenario.query_alias
        )
        assert resolution.status == scenario.expected_status, f"Failed on scenario {scenario.name}"
        candidate_ids = [c.id for c in resolution.candidates]
        assert candidate_ids == scenario.expected_identity_ids, (
            f"Candidates mismatch for {scenario.name}"
        )

    # Scenario: resolving ambiguous unreviewed alias, then correcting it via reviewed update
    ambiguous = service.resolve("product", "nb", "recipe", "tomat")
    assert ambiguous.status == "ambiguous"
    assert len(ambiguous.candidates) == 2

    # Reviewer confirms tomato is the fresh variant
    service.correct_alias("product", "nb", "recipe", "tomat", SYNTHETIC_TOMATO_FRESH_PRODUCT_ID)
    resolved = service.resolve("product", "nb", "recipe", "tomat")
    assert resolved.status == "resolved"
    assert [c.id for c in resolved.candidates] == [SYNTHETIC_TOMATO_FRESH_PRODUCT_ID]
    assert ("product", "nb", "recipe", "tomat") in repository.reviewed
    assert repository.network_calls == 0


def test_linked_recipe_roundtrip_preserves_canonical_identities_and_source_expression():
    recipe = synthetic_linked_recipe()
    round_trip = Recipe.model_validate(recipe.model_dump(mode="json"))

    assert recipe == round_trip
    assert round_trip.id == SYNTHETIC_LINKED_RECIPE_ID

    barley = round_trip.ingredients[0]
    assert barley.canonical_product_id == SYNTHETIC_BARLEY_PRODUCT_ID
    assert barley.canonical_unit_id == SYNTHETIC_GRAM_UNIT_ID
    assert barley.source_unit_expression == "grams (g)"
    assert barley.unit == "g"

    carrot = round_trip.ingredients[1]
    assert carrot.canonical_product_id == SYNTHETIC_CARROT_PRODUCT_ID
    assert carrot.canonical_unit_id == SYNTHETIC_GRAM_UNIT_ID
    assert carrot.source_unit_expression == "g"

    chickpeas = round_trip.ingredients[2]
    assert chickpeas.canonical_product_id == SYNTHETIC_CHICKPEAS_PRODUCT_ID
    assert chickpeas.canonical_unit_id == SYNTHETIC_GRAM_UNIT_ID

    citrus_oil = round_trip.ingredients[3]
    assert citrus_oil.canonical_product_id == SYNTHETIC_CITRUS_OIL_PRODUCT_ID
    assert citrus_oil.canonical_unit_id == SYNTHETIC_TBSP_UNIT_ID
    assert citrus_oil.source_unit_expression == "Tablespoons"
    assert citrus_oil.unit == "tbsp"

    salt = round_trip.ingredients[4]
    assert salt.canonical_product_id is None
    assert salt.canonical_unit_id is None
    assert salt.source_unit_expression is None
    assert salt.quantity is None
    assert salt.unit is None

    # Verify legacy recipe editor client update retains canonical identities
    legacy_payload = RecipeCreate.model_validate(
        {
            "title": round_trip.title,
            "servings": round_trip.servings,
            "ingredients": [
                {
                    "name": item.name,
                    "quantity": str(item.quantity) if item.quantity is not None else None,
                    "unit": item.unit,
                    "category": item.category,
                    "source_indices": item.source_indices,
                }
                for item in round_trip.ingredients
            ],
            "instructions": round_trip.instructions,
            "source_ingredients": [
                {"index": item.index, "text": item.text} for item in round_trip.source_ingredients
            ],
        }
    )
    incoming_values = [item.model_dump() for item in legacy_payload.ingredients]
    retained = KitchenService._retain_omitted_ingredient_identity_fields(
        legacy_payload, round_trip, incoming_values
    )
    assert retained[0]["canonical_product_id"] == SYNTHETIC_BARLEY_PRODUCT_ID
    assert retained[0]["canonical_unit_id"] == SYNTHETIC_GRAM_UNIT_ID
    assert retained[0]["source_unit_expression"] == "grams (g)"
    assert retained[3]["canonical_product_id"] == SYNTHETIC_CITRUS_OIL_PRODUCT_ID
    assert retained[3]["source_unit_expression"] == "Tablespoons"


def test_basket_candidate_identity_confirmation_states_and_freshness():
    compatible = synthetic_compatible_candidate()
    unverified = synthetic_unverified_candidate()
    incompatible_unit = synthetic_incompatible_unit_candidate()

    assert compatible.identity_status == "compatible"
    assert compatible.product_url is not None
    assert str(compatible.product_url).startswith("https://fixtures.invalid/")

    assert unverified.identity_status == "unverified"
    assert unverified.product_url is not None
    assert str(unverified.product_url).startswith("https://fixtures.invalid/")

    assert incompatible_unit.pack_unit == "l"

    # Isolation check
    compatible.name = "Mutated candidate name"
    fresh_compatible = synthetic_compatible_candidate()
    assert fresh_compatible.name == "TEST ONLY Gulrotbånd, 2 stk"


@pytest.mark.parametrize("scenario", synthetic_basket_match_scenarios())
def test_basket_match_scenarios_and_negative_exclusions(scenario: SyntheticBasketMatchScenario):
    status = _identity_status(scenario.query, scenario.product)
    matches = _product_matches_ingredient(scenario.query, scenario.product)

    if scenario.expected_identity_status == "compatible":
        assert status == "compatible"
        assert matches is True
        assert scenario.is_excluded is False
        assert scenario.requires_confirmation is False
    elif scenario.expected_identity_status == "unverified":
        assert status == "unverified"
        assert matches is False
        assert scenario.is_excluded is False
        assert scenario.requires_confirmation is True
    elif scenario.expected_identity_status == "incompatible":
        assert status is None
        assert matches is False
        assert scenario.is_excluded is True


@pytest.mark.asyncio
async def test_basket_job_identity_confirmation_with_synthetic_fixtures():
    jobs = BasketMatchJobs(
        matching=cast(Any, None),
        inventory=cast(Any, None),
        scope_provider=lambda actor: 1,
    )
    actor = "synthetic-actor"
    start, end = date(2026, 11, 16), date(2026, 11, 22)
    job_id = "synthetic-job-001"

    compatible = synthetic_compatible_candidate()
    unverified = synthetic_unverified_candidate()

    need_comp = NeedMatchResult(
        need_id=SYNTHETIC_BASKET_NEED_ID,
        name="gulrot",
        quantity=Decimal(2),
        unit="item",
        page=1,
        has_more=False,
        candidates=[compatible],
        status="matched",
    )
    need_unver = NeedMatchResult(
        need_id=SYNTHETIC_UNVERIFIED_NEED_ID,
        name="citrus oil shower gel",
        quantity=Decimal(1),
        unit="item",
        page=1,
        has_more=False,
        candidates=[unverified],
        status="matched",
    )

    job_value = BasketMatchJob(
        job_id=job_id,
        status="complete",
        start=start,
        end=end,
        needs=[need_comp, need_unver],
    )
    now = jobs.clock()
    jobs._jobs[job_id] = _Job(
        actor=actor,
        scope=1,
        fingerprint=(),
        created=now,
        value=job_value,
        finished=now,
    )

    # 1. Compatible candidate resolves without requiring explicit confirmation
    comp_ref = [(SYNTHETIC_BASKET_NEED_ID, compatible.id)]
    resolved_comp = await jobs.resolve_batch_candidates(actor, job_id, start, end, comp_ref)
    assert resolved_comp[comp_ref[0]][1].identity_status == "compatible"

    # 2. Unverified candidate fails without identity confirmation (#100)
    unver_ref = [(SYNTHETIC_UNVERIFIED_NEED_ID, unverified.id)]
    with pytest.raises(ValueError, match="Product identity is unverified"):
        await jobs.resolve_batch_candidates(actor, job_id, start, end, unver_ref)

    # 3. Invalid confirmation pair not matching references raises ValueError
    wrong_pair = {(SYNTHETIC_BASKET_NEED_ID, unverified.id)}
    with pytest.raises(ValueError, match="must name a selected candidate"):
        await jobs.resolve_batch_candidates(
            actor, job_id, start, end, unver_ref, identity_confirmations=wrong_pair
        )

    # 4. Unverified candidate succeeds when explicitly confirmed
    resolved_unver = await jobs.resolve_batch_candidates(
        actor, job_id, start, end, unver_ref, identity_confirmations=set(unver_ref)
    )
    assert resolved_unver[unver_ref[0]][1].identity_status == "unverified"


def test_fixture_isolation_and_production_safety():
    repo_root = Path(__file__).parents[1]
    src_dir = repo_root / "src" / "home_kitchen"

    # Verify no source code imports from tests or fixtures
    for py_file in src_dir.rglob("*.py"):
        content = py_file.read_text()
        assert not re.search(r"\bfrom\s+(tests|fixtures)\b", content), (
            f"{py_file} improperly imports test or fixture modules"
        )
        assert not re.search(r"\bimport\s+(tests|fixtures)\b", content), (
            f"{py_file} improperly imports test or fixture modules"
        )

    # Verify all synthetic URLs across synthetic_kitchen fixtures use RFC 2606 safe domains
    fixtures_dir = repo_root / "tests" / "fixtures" / "synthetic_kitchen"
    for py_file in fixtures_dir.glob("*.py"):
        content = py_file.read_text()
        for url_match in re.finditer(r"https?://([^/\"'\s]+)", content):
            domain = url_match.group(1).casefold()
            assert domain.endswith(".invalid") or domain.endswith(".test"), (
                f"Non-isolated URL domain '{domain}' found in {py_file}"
            )
