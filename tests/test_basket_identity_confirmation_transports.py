from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP

from home_kitchen.basket.matching import BasketCandidate, NeedMatchResult
from home_kitchen.basket.models import BasketReview
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools

START = date(2026, 11, 16)
END = date(2026, 11, 22)
NEED_IDS = (UUID(int=1), UUID(int=2))


def transport_fixtures():
    resolved = {}
    for index, need_id in enumerate(NEED_IDS, start=1):
        candidate = BasketCandidate(
            id=f"provider:milk-{index}",
            provider="provider",
            product_id=f"milk-{index}",
            name=f"Milk {index}",
            description="1 item",
            pack_quantity=Decimal(1),
            pack_unit="item",
            pack_metadata_source="product_page",
            pack_metadata_status="unverified",
            match_query="milk",
            match_reason="Search result",
        )
        need = NeedMatchResult(
            need_id=need_id,
            name="Milk",
            quantity=Decimal(1),
            unit="item",
            page=1,
            has_more=False,
            candidates=[candidate],
        )
        resolved[(need_id, candidate.id)] = (need, candidate, datetime.now(UTC))
    return resolved


def transport_services():
    return (
        SimpleNamespace(
            basket_jobs=SimpleNamespace(
                resolve_batch_candidates=AsyncMock(return_value=transport_fixtures())
            )
        ),
        SimpleNamespace(
            select_batch=Mock(
                return_value=BasketReview(
                    needs=[],
                    total_cost=Decimal(0),
                    price_complete=False,
                    currency="NOK",
                    ready_for_review=False,
                    issues=[],
                )
            )
        ),
    )


def selection(need_id: UUID, candidate_id: str, *, confirm_identity: bool | None = None):
    value = {"need_id": str(need_id), "candidate_id": candidate_id, "packages": 1}
    if confirm_identity is not None:
        value["confirm_identity"] = confirm_identity
    return value


def expected_references():
    return [(need_id, f"provider:milk-{index}") for index, need_id in enumerate(NEED_IDS, 1)]


def assert_confirmation_calls(resolver: AsyncMock, actor: str):
    references = expected_references()
    assert resolver.await_count == 2
    assert resolver.await_args_list[0].args == (actor, "current-job", START, END, references)
    assert resolver.await_args_list[0].kwargs == {}
    assert resolver.await_args_list[1].args == resolver.await_args_list[0].args
    assert resolver.await_args_list[1].kwargs == {"identity_confirmations": {references[1]}}


def test_rest_batch_selection_defaults_identity_confirmation_to_false():
    features, basket = transport_services()
    app = FastAPI()

    @app.middleware("http")
    async def household_actor(request, call_next):
        request.state.actor_id = "test-household"
        return await call_next(request)

    app.include_router(feature_router(features, basket))
    references = expected_references()
    with TestClient(app) as client:
        defaulted = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(START),
                "end": str(END),
                "job_id": "current-job",
                "selections": [selection(*references[0]), selection(*references[1])],
            },
        )
        assert defaulted.status_code == 200, defaulted.text

        confirmed = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(START),
                "end": str(END),
                "job_id": "current-job",
                "selections": [
                    selection(*references[0]),
                    selection(*references[1], confirm_identity=True),
                ],
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        generic_review = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(START),
                "end": str(END),
                "job_id": "current-job",
                "reviewed": True,
                "selections": [selection(*references[0]), selection(*references[1])],
            },
        )
        assert generic_review.status_code == 422

    assert_confirmation_calls(features.basket_jobs.resolve_batch_candidates, "test-household")
    assert basket.select_batch.call_count == 2


@pytest.mark.asyncio
async def test_mcp_batch_selection_defaults_identity_confirmation_to_false():
    features, basket = transport_services()
    mcp = FastMCP("identity-transport-test")
    install_feature_tools(mcp, features, basket)
    references = expected_references()
    token = history_context.set(HistoryContext(actor_id="test-household", channel="mcp"))
    try:
        async with Client(mcp) as client:
            for second_selection in (
                selection(*references[1]),
                selection(*references[1], confirm_identity=True),
            ):
                result = await client.call_tool(
                    "select_basket_products",
                    {
                        "data": {
                            "start": str(START),
                            "end": str(END),
                            "job_id": "current-job",
                            "selections": [selection(*references[0]), second_selection],
                        }
                    },
                )
                assert not result.is_error
    finally:
        history_context.reset(token)

    assert_confirmation_calls(features.basket_jobs.resolve_batch_candidates, "test-household")
    assert basket.select_batch.call_count == 2
