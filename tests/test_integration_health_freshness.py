"""Tests for integration health, background matching freshness, and actionable errors (Refs #93)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from home_kitchen.basket.jobs import BasketMatchJob, BasketMatchJobs, _Job
from home_kitchen.basket.matching import BasketMatchingService
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.integrations import IntegrationAccountInfo, IntegrationInfo

pytestmark = pytest.mark.mocked_provider


def _make_jobs(clock_fn):
    matching = MagicMock(spec=BasketMatchingService)
    inventory = MagicMock()
    return BasketMatchJobs(
        matching=matching,
        inventory=inventory,
        scope_provider=lambda _actor: 1,
        ttl_seconds=600,
        clock=clock_fn,
    )


def test_background_matching_health_marks_stale_and_failed_without_leaking_actors():
    now = [100.0]
    jobs = _make_jobs(lambda: now[0])

    # Idle when no jobs exist
    idle = jobs.health_snapshot("malo")
    assert idle.status == "idle"
    assert idle.stale is False

    # Completed job for malo
    done_job = _Job(
        actor="malo",
        scope=1,
        fingerprint=("fp",),
        created=50.0,
        finished=55.0,
        observed_at=datetime.now(UTC) - timedelta(minutes=2),
        value=BasketMatchJob(
            job_id="j1",
            start=date(2026, 11, 20),
            end=date(2026, 11, 26),
            status="complete",
        ),
    )
    jobs._jobs["j1"] = done_job

    # Other actor must not see malo's job
    assert jobs.health_snapshot("other").status == "idle"

    healthy = jobs.health_snapshot("malo")
    assert healthy.status == "healthy"
    assert healthy.stale is False

    # Advance clock past ttl_seconds -> stale!
    now[0] = 700.0
    stale = jobs.health_snapshot("malo")
    assert stale.status == "stale"
    assert stale.stale is True
    assert "utdaterte" in (stale.freshness_note or "")

    # Subsequent failure overrides as failed & stale
    failed_job = _Job(
        actor="malo",
        scope=1,
        fingerprint=("fp2",),
        created=701.0,
        finished=702.0,
        observed_at=datetime.now(UTC),
        value=BasketMatchJob(
            job_id="j2",
            start=date(2026, 11, 20),
            end=date(2026, 11, 26),
            status="failed",
            detail="Oda-sesjonen er utløpt.",
        ),
    )
    jobs._jobs["j2"] = failed_job
    failed = jobs.health_snapshot("malo")
    assert failed.status == "failed"
    assert failed.stale is True
    assert failed.last_error == "Oda-sesjonen er utløpt."


@pytest.mark.asyncio
async def test_integrations_health_endpoint_and_mcp_surface_actionable_issues():
    now = [100.0]
    jobs = _make_jobs(lambda: now[0])
    jobs._jobs["j_fail"] = _Job(
        actor="malo",
        scope=1,
        fingerprint=("fp",),
        created=90.0,
        finished=91.0,
        observed_at=datetime.now(UTC),
        value=BasketMatchJob(
            job_id="j_fail",
            start=date(2026, 11, 20),
            end=date(2026, 11, 26),
            status="failed",
            detail="Nettverksfeil mot Oda.",
        ),
    )

    expired_trumf = IntegrationInfo(
        id="trumf",
        name="Trumf",
        status="expired",
        token_status="expired",
        supported_auth_modes=["session_cookie"],
        capabilities=["purchase_history", "receipt_sync"],
        detail="Trumf-tilkobling",
        expiry_prompt="Trumf-sesjonen har utløpt. Koble til på nytt.",
        accounts=[
            IntegrationAccountInfo(
                account_id="default",
                auth_mode="session_cookie",
                auth_type="session_cookie",
                status="expired",
                token_status="expired",
                expiry_prompt="Trumf-sesjonen har utløpt.",
            )
        ],
    )

    features = SimpleNamespace(
        integrations=SimpleNamespace(describe=lambda _actor: [expired_trumf]),
        basket_jobs=jobs,
        purchase_sync=None,
    )

    app = FastAPI()

    @app.middleware("http")
    async def inject_actor(request, call_next):
        request.state.actor_id = "malo"
        return await call_next(request)

    app.include_router(feature_router(features, basket=MagicMock()))

    with TestClient(app) as client:
        res = client.get("/api/integrations/health")
        assert res.status_code == 200
        body = res.json()
        assert body["overall_status"] == "attention_needed"
        assert body["background_matching"]["status"] == "failed"
        assert any("Trumf-sesjonen har utløpt" in msg for msg in body["actionable_issues"])
        assert any("Siste bakgrunnssøk feilet" in msg for msg in body["actionable_issues"])

    mcp = FastMCP("test-health")
    install_feature_tools(mcp, features, basket=MagicMock())
    tool = await mcp.get_tool("get_integrations_health")
    from home_kitchen.context import history_context

    token = history_context.set(SimpleNamespace(actor_id="malo", channel="mcp", session_id=None))
    try:
        mcp_out = tool.fn()
        assert mcp_out["overall_status"] == "attention_needed"
        assert mcp_out["background_matching"]["status"] == "failed"
    finally:
        history_context.reset(token)
