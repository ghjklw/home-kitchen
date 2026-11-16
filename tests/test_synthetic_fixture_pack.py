"""Contract checks for original, isolated synthetic fixtures."""

from typing import Any, cast

import pytest
from fixtures.synthetic_kitchen.fakes import (
    ScriptedHistoryReader,
    StaticOdaClient,
    SyntheticReaderFailure,
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
from fixtures.synthetic_kitchen.recipes import SYNTHETIC_RECIPE_ID, synthetic_recipe

from home_kitchen.basket.matching import OdaSearchAdapter
from home_kitchen.models import Recipe
from home_kitchen.oda.models import OdaSearchResponse
from home_kitchen.oda.service import OdaService
from home_kitchen.recipes.collections import OdaCollectionProvider


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
