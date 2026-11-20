"""Unit and REST/MCP transport parity tests for batch basket selection."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from home_kitchen.basket.application import (
    BatchSelectionInput,
    BatchSelectionValidationError,
    select_basket_products_batch,
)
from home_kitchen.basket.jobs import BasketMatchJob, BasketMatchJobs, _Job
from home_kitchen.basket.matching import BasketCandidate, NeedMatchResult
from home_kitchen.basket.models import BasketReview, SelectedProduct
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.history import HistoryChannel

START = date(2026, 11, 16)
END = date(2026, 11, 22)
NEED_ID_1 = UUID(int=1)
NEED_ID_2 = UUID(int=2)
ACTOR = "household-actor"
OBSERVED_AT = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class _DummyInventory:
    def plan(self, start: date, end: date):  # pragma: no cover - unused by seeded jobs
        raise NotImplementedError


class _DummyMatching:
    async def find_products(self, *args, **kwargs):  # pragma: no cover - unused by seeded jobs
        raise NotImplementedError


class RecordingBasketService:
    """In-memory BasketService stub recording calls and channel audit attribution."""

    def __init__(self) -> None:
        self.calls: list[
            tuple[date, date, list[SelectedProduct], dict[UUID, NeedMatchResult], HistoryChannel]
        ] = []

    def select_batch(
        self,
        start: date,
        end: date,
        selections: list[SelectedProduct],
        evidence: dict[UUID, NeedMatchResult],
        *,
        channel: HistoryChannel = "api",
    ) -> BasketReview:
        self.calls.append((start, end, selections, evidence, channel))
        return BasketReview(
            needs=[],
            total_cost=Decimal("0"),
            price_complete=True,
            currency="NOK",
            ready_for_review=True,
            issues=[],
        )


def _candidate(
    *,
    need_suffix: int = 1,
    identity_status: str = "compatible",
    pack_quantity: Decimal | None = Decimal("1"),
    pack_unit: str | None = "l",
    pack_metadata_status: str = "unverified",
    price: str | None = "24.90",
) -> BasketCandidate:
    return BasketCandidate(
        id=f"oda:milk-{need_suffix}",
        provider="oda",
        product_id=f"milk-{need_suffix}",
        name=f"Milk {need_suffix}",
        brand="Tine",
        description="1 l",
        product_url=f"https://oda.com/no/products/milk-{need_suffix}",
        price=price,
        pack_description="1 l",
        pack_quantity=pack_quantity,
        pack_unit=pack_unit,
        pack_metadata_source="oda_description" if pack_quantity is not None else None,
        pack_metadata_status=pack_metadata_status,
        match_query="milk",
        match_reason="Search result",
        identity_status=identity_status,
        available=True,
    )


def _seed_job(
    jobs: BasketMatchJobs,
    *,
    job_id: str = "job-1",
    actor: str = ACTOR,
    scope: int = 1,
    start: date = START,
    end: date = END,
    status: str = "complete",
    candidate_1: BasketCandidate | None = None,
    candidate_2: BasketCandidate | None = None,
) -> str:
    c1 = candidate_1 or _candidate(need_suffix=1, identity_status="compatible")
    c2 = candidate_2 or _candidate(need_suffix=2, identity_status="unverified")
    needs = [
        NeedMatchResult(
            need_id=NEED_ID_1,
            name="Whole Milk",
            quantity=Decimal("1"),
            unit="l",
            page=1,
            has_more=False,
            candidates=[c1],
            status="matched",
        ),
        NeedMatchResult(
            need_id=NEED_ID_2,
            name="Oat Milk",
            quantity=Decimal("2"),
            unit="l",
            page=1,
            has_more=False,
            candidates=[c2],
            status="matched",
        ),
    ]
    created = jobs.clock()
    job_value = BasketMatchJob(
        job_id=job_id,
        status=status,
        start=start,
        end=end,
        observed_at=OBSERVED_AT,
        needs=needs,
    )
    jobs._jobs[job_id] = _Job(
        actor=actor,
        scope=scope,
        fingerprint=(),
        created=created,
        value=job_value,
        finished=created if status == "complete" else None,
        observed_at=OBSERVED_AT,
    )
    return job_id


def _harness(*, actor_id: str | None = ACTOR):
    now = [100.0]
    scopes = {ACTOR: 1, "other-actor": 1}
    jobs = BasketMatchJobs(
        _DummyMatching(),  # type: ignore[arg-type]
        _DummyInventory(),  # type: ignore[arg-type]
        scopes.get,
        ttl_seconds=600,
        clock=lambda: now[0],
    )
    basket = RecordingBasketService()
    features = SimpleNamespace(basket_jobs=jobs)

    app = FastAPI()

    @app.middleware("http")
    async def attach_actor(request, call_next):
        if actor_id is not None:
            request.state.actor_id = actor_id
        return await call_next(request)

    app.include_router(feature_router(features, basket))  # type: ignore[arg-type]

    mcp = FastMCP("basket-application-parity-test")
    install_feature_tools(mcp, features, basket)  # type: ignore[arg-type]

    return jobs, basket, app, mcp, now, scopes


@pytest.mark.asyncio
async def test_application_use_case_validates_inputs_and_maps_snapshot():
    jobs, basket, _, _, _, _ = _harness()
    job_id = _seed_job(
        jobs,
        candidate_1=_candidate(
            need_suffix=1, identity_status="compatible", price="invalid-decimal"
        ),
    )

    with pytest.raises(ValueError, match="authenticated actor"):
        await select_basket_products_batch(
            basket,
            jobs,
            start=START,
            end=END,
            job_id=job_id,
            selections=[
                BatchSelectionInput(need_id=NEED_ID_1, candidate_id="oda:milk-1", packages=1)
            ],
            actor_id="   ",
            channel="api",
        )

    with pytest.raises(BatchSelectionValidationError, match="end date"):
        await select_basket_products_batch(
            basket,
            jobs,
            start=END,
            end=START,
            job_id=job_id,
            selections=[
                BatchSelectionInput(need_id=NEED_ID_1, candidate_id="oda:milk-1", packages=1)
            ],
            actor_id=ACTOR,
            channel="api",
        )

    with pytest.raises(BatchSelectionValidationError, match="between 1 and 100"):
        await select_basket_products_batch(
            basket,
            jobs,
            start=START,
            end=END,
            job_id=job_id,
            selections=[],
            actor_id=ACTOR,
            channel="api",
        )

    with pytest.raises(BatchSelectionValidationError, match="at most one product"):
        await select_basket_products_batch(
            basket,
            jobs,
            start=START,
            end=END,
            job_id=job_id,
            selections=[
                BatchSelectionInput(need_id=NEED_ID_1, candidate_id="oda:milk-1", packages=1),
                BatchSelectionInput(need_id=NEED_ID_1, candidate_id="oda:milk-2", packages=1),
            ],
            actor_id=ACTOR,
            channel="mcp",
        )

    review = await select_basket_products_batch(
        basket,
        jobs,
        start=START,
        end=END,
        job_id=job_id,
        selections=[
            BatchSelectionInput(need_id=NEED_ID_1, candidate_id="oda:milk-1", packages=2),
            BatchSelectionInput(
                need_id=NEED_ID_2,
                candidate_id="oda:milk-2",
                packages=1,
                confirm_identity=True,
            ),
        ],
        actor_id=ACTOR,
        channel="mcp",
    )
    assert review.ready_for_review is True
    assert len(basket.calls) == 1
    call_start, call_end, selected, evidence, channel = basket.calls[0]
    assert (call_start, call_end, channel) == (START, END, "mcp")
    assert selected[0].product.price is None
    assert selected[1].product.price == Decimal("24.90")
    assert selected[0].product.observed_at == OBSERVED_AT
    assert set(evidence) == {NEED_ID_1, NEED_ID_2}


@pytest.mark.asyncio
async def test_application_use_case_rejects_unknown_package_metadata():
    jobs, basket, _, _, _, _ = _harness()
    job_id = _seed_job(
        jobs,
        candidate_1=_candidate(
            need_suffix=1,
            identity_status="compatible",
            pack_quantity=None,
            pack_unit=None,
            pack_metadata_status="unknown",
        ),
    )

    with pytest.raises(ValueError, match="unknown package details"):
        await select_basket_products_batch(
            basket,
            jobs,
            start=START,
            end=END,
            job_id=job_id,
            selections=[
                BatchSelectionInput(need_id=NEED_ID_1, candidate_id="oda:milk-1", packages=1)
            ],
            actor_id=ACTOR,
            channel="api",
        )
    assert basket.calls == []


@pytest.mark.asyncio
async def test_rest_and_mcp_parity_valid_batch_selection_and_channel_attribution():
    jobs, basket, app, mcp, _, _ = _harness()
    job_id = _seed_job(jobs)
    payload = {
        "start": str(START),
        "end": str(END),
        "job_id": job_id,
        "selections": [
            {
                "need_id": str(NEED_ID_1),
                "candidate_id": "oda:milk-1",
                "packages": 1,
            },
            {
                "need_id": str(NEED_ID_2),
                "candidate_id": "oda:milk-2",
                "packages": 2,
                "confirm_identity": True,
            },
        ],
    }

    with TestClient(app) as rest_client:
        rest_response = rest_client.put("/api/basket/selections/batch", json=payload)
        assert rest_response.status_code == 200, rest_response.text
        assert rest_response.headers["cache-control"] == "no-store"

    token = history_context.set(HistoryContext(actor_id=ACTOR, channel="mcp"))
    try:
        async with Client(mcp) as mcp_client:
            mcp_result = await mcp_client.call_tool("select_basket_products", {"data": payload})
            assert not mcp_result.is_error
    finally:
        history_context.reset(token)

    assert len(basket.calls) == 2
    api_call, mcp_call = basket.calls
    assert api_call[0:4] == mcp_call[0:4]
    assert api_call[4] == "api"
    assert mcp_call[4] == "mcp"


@pytest.mark.asyncio
async def test_rest_and_mcp_parity_rejects_unverified_identity_and_invalid_input():
    jobs, basket, app, mcp, _, _ = _harness()
    job_id = _seed_job(jobs)

    unconfirmed_payload = {
        "start": str(START),
        "end": str(END),
        "job_id": job_id,
        "selections": [
            {
                "need_id": str(NEED_ID_2),
                "candidate_id": "oda:milk-2",
                "packages": 1,
            }
        ],
    }
    bad_range_payload = {
        "start": str(END),
        "end": str(START),
        "job_id": job_id,
        "selections": [
            {
                "need_id": str(NEED_ID_1),
                "candidate_id": "oda:milk-1",
                "packages": 1,
            }
        ],
    }
    duplicate_need_payload = {
        "start": str(START),
        "end": str(END),
        "job_id": job_id,
        "selections": [
            {"need_id": str(NEED_ID_1), "candidate_id": "oda:milk-1", "packages": 1},
            {"need_id": str(NEED_ID_1), "candidate_id": "oda:milk-2", "packages": 1},
        ],
    }

    with TestClient(app) as rest_client:
        unconfirmed_rest = rest_client.put("/api/basket/selections/batch", json=unconfirmed_payload)
        assert unconfirmed_rest.status_code == 409
        assert "identity is unverified" in unconfirmed_rest.json()["detail"].casefold()

        bad_range_rest = rest_client.put("/api/basket/selections/batch", json=bad_range_payload)
        assert bad_range_rest.status_code == 422
        assert "end date must be on or after start date" in bad_range_rest.json()["detail"]

        dup_rest = rest_client.put("/api/basket/selections/batch", json=duplicate_need_payload)
        assert dup_rest.status_code == 422
        assert "Select at most one product for each purchase need" in dup_rest.json()["detail"]

    token = history_context.set(HistoryContext(actor_id=ACTOR, channel="mcp"))
    try:
        async with Client(mcp) as mcp_client:
            with pytest.raises(ToolError, match="identity is unverified"):
                await mcp_client.call_tool("select_basket_products", {"data": unconfirmed_payload})
            with pytest.raises(ToolError, match="end date must be on or after start date"):
                await mcp_client.call_tool("select_basket_products", {"data": bad_range_payload})
            with pytest.raises(
                ToolError, match="Select at most one product for each purchase need"
            ):
                await mcp_client.call_tool(
                    "select_basket_products", {"data": duplicate_need_payload}
                )
    finally:
        history_context.reset(token)

    assert basket.calls == []


@pytest.mark.asyncio
async def test_rest_and_mcp_parity_handles_missing_and_stale_jobs():
    jobs, basket, app, mcp, now, scopes = _harness()
    expired_rest_job_id = _seed_job(jobs, job_id="expired-rest-job")
    other_job_id = _seed_job(jobs, job_id="other-actor-job", actor="other-actor")

    def payload_for(target_job_id: str) -> dict:
        return {
            "start": str(START),
            "end": str(END),
            "job_id": target_job_id,
            "selections": [
                {
                    "need_id": str(NEED_ID_1),
                    "candidate_id": "oda:milk-1",
                    "packages": 1,
                }
            ],
        }

    with TestClient(app) as rest_client:
        missing_rest = rest_client.put(
            "/api/basket/selections/batch", json=payload_for("nonexistent-job")
        )
        assert missing_rest.status_code == 404
        assert missing_rest.json()["detail"] == "Product match job not found"

        wrong_actor_rest = rest_client.put(
            "/api/basket/selections/batch", json=payload_for(other_job_id)
        )
        assert wrong_actor_rest.status_code == 404

    token = history_context.set(HistoryContext(actor_id=ACTOR, channel="mcp"))
    try:
        async with Client(mcp) as mcp_client:
            with pytest.raises(ToolError, match="Product match job not found"):
                await mcp_client.call_tool(
                    "select_basket_products", {"data": payload_for("nonexistent-job")}
                )
            with pytest.raises(ToolError, match="Product match job not found"):
                await mcp_client.call_tool(
                    "select_basket_products", {"data": payload_for(other_job_id)}
                )

            # Advance clock past TTL -> stale/expired job in both transports
            now[0] += 601.0
            with TestClient(app) as rest_client:
                expired_rest = rest_client.put(
                    "/api/basket/selections/batch", json=payload_for(expired_rest_job_id)
                )
                assert expired_rest.status_code == 409
                assert "expired" in expired_rest.json()["detail"].casefold()

            expired_mcp_job_id = _seed_job(jobs, job_id="expired-mcp-job")
            now[0] += 601.0
            with pytest.raises(ToolError, match="expired"):
                await mcp_client.call_tool(
                    "select_basket_products", {"data": payload_for(expired_mcp_job_id)}
                )

            # Re-seed and invalidate via provider context change in both transports
            context_rest_job_id = _seed_job(jobs, job_id="context-rest-job", scope=1)
            scopes[ACTOR] = 2
            with TestClient(app) as rest_client:
                context_rest = rest_client.put(
                    "/api/basket/selections/batch", json=payload_for(context_rest_job_id)
                )
                assert context_rest.status_code == 409
                assert "expired or provider context changed" in context_rest.json()["detail"]

            context_mcp_job_id = _seed_job(jobs, job_id="context-mcp-job", scope=2)
            scopes[ACTOR] = 3
            with pytest.raises(ToolError, match="expired or provider context changed"):
                await mcp_client.call_tool(
                    "select_basket_products", {"data": payload_for(context_mcp_job_id)}
                )
    finally:
        history_context.reset(token)

    assert basket.calls == []


@pytest.mark.asyncio
async def test_rest_and_mcp_parity_require_authenticated_actor():
    jobs, basket, app, mcp, _, _ = _harness(actor_id=None)
    job_id = _seed_job(jobs)
    payload = {
        "start": str(START),
        "end": str(END),
        "job_id": job_id,
        "selections": [{"need_id": str(NEED_ID_1), "candidate_id": "oda:milk-1", "packages": 1}],
    }

    with TestClient(app) as rest_client:
        unauth_rest = rest_client.put("/api/basket/selections/batch", json=payload)
        assert unauth_rest.status_code == 401

    async with Client(mcp) as mcp_client:
        with pytest.raises(ToolError, match="Household authentication required"):
            await mcp_client.call_tool("select_basket_products", {"data": payload})

    assert basket.calls == []
