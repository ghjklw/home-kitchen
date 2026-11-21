"""Tests for auditable product and unit mapping corrections across app/API and MCP."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from home_kitchen.catalogue.identity import AliasCorrectionInput
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.context import history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools

sqlite3.register_adapter(UUID, str)


@pytest.fixture
def identity_engine():
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                """CREATE TABLE kitchen_canonical_products (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    variant TEXT NOT NULL DEFAULT '',
                    UNIQUE(canonical_name, variant)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_canonical_units (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL UNIQUE
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_product_aliases (
                    locale TEXT NOT NULL,
                    context TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    reviewed BOOLEAN NOT NULL DEFAULT 0,
                    PRIMARY KEY (locale, context, normalized_alias)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_unit_aliases (
                    locale TEXT NOT NULL,
                    context TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    reviewed BOOLEAN NOT NULL DEFAULT 0,
                    PRIMARY KEY (locale, context, normalized_alias)
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_identity_correction_events (
                    id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    household_id TEXT,
                    actor_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    locale TEXT NOT NULL,
                    context TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    previous_identity_id TEXT,
                    new_identity_id TEXT NOT NULL,
                    previous_reviewed BOOLEAN,
                    new_reviewed BOOLEAN NOT NULL,
                    source_type TEXT NOT NULL
                )"""
            )
        )
    yield engine
    engine.dispose()


def test_correction_records_audit_event_and_deduplicates_unchanged_retry(identity_engine):
    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)
    old_prod = identities.create_product("tomato", "")
    new_prod = identities.create_product("carrot", "")
    identities.add_alias("product", "nb", "recipe", "gulrot", old_prod.id)

    household_a = uuid4()
    household_b = uuid4()

    ev1 = identities.correct_alias(
        "product",
        "nb",
        "recipe",
        "gulrot",
        new_prod.id,
        actor_id="malo",
        household_id=household_a,
        channel="api",
    )
    assert ev1 is not None
    assert ev1.previous_identity_id == old_prod.id
    assert ev1.new_identity_id == new_prod.id
    assert ev1.previous_reviewed is False
    assert ev1.new_reviewed is True
    assert ev1.actor_id == "malo"
    assert ev1.channel == "api"

    # Unchanged retry must not emit a duplicate audit event
    ev2 = identities.correct_alias(
        "product",
        "nb",
        "recipe",
        "gulrot",
        new_prod.id,
        actor_id="malo",
        household_id=household_a,
        channel="api",
    )
    assert ev2 is None

    # Household isolation
    assert len(identities.list_correction_events(household_id=household_a)) == 1
    assert len(identities.list_correction_events(household_id=household_b)) == 0


def test_invalid_identity_leaves_no_partial_alias_or_audit_event(identity_engine):
    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)
    prod = identities.create_product("milk", "")
    identities.add_alias("product", "nb", "recipe", "melk", prod.id)

    with pytest.raises(ValueError, match="Unknown canonical product"):
        identities.correct_alias(
            "product",
            "nb",
            "recipe",
            "melk",
            uuid4(),
            actor_id="malo",
            household_id=uuid4(),
            channel="api",
        )

    assert identities.resolve("product", "nb", "recipe", "melk").candidates[0].id == prod.id
    assert identities.list_correction_events() == []


async def test_api_and_mcp_correction_parity_and_review_boundary(identity_engine):
    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)
    butter = identities.create_product("butter", "")
    margarine = identities.create_product("margarine", "")
    household_id = uuid4()

    features = SimpleNamespace(
        catalogue_identities=identities,
        household_id=household_id,
        kitchen=SimpleNamespace(),
    )

    app = FastAPI()

    @app.middleware("http")
    async def inject_actor(request, call_next):
        request.state.actor_id = "api-user"
        return await call_next(request)

    app.include_router(feature_router(features, basket=None))

    with TestClient(app) as client:
        res = client.put(
            f"/api/catalogue/aliases/product/{butter.id}/review",
            json={"alias": "smør", "locale": "nb", "context": "recipe"},
        )
        assert res.status_code == 204

        missing = client.put(
            f"/api/catalogue/aliases/product/{uuid4()}/review",
            json={"alias": "smør", "locale": "nb", "context": "recipe"},
        )
        assert missing.status_code == 404

        history = client.get("/api/catalogue/aliases/corrections")
        assert history.status_code == 200
        events = history.json()
        assert len(events) == 1
        assert events[0]["actor_id"] == "api-user"
        assert events[0]["channel"] == "api"

        unreviewed = client.post(
            "/api/catalogue/aliases/correct",
            json={
                "kind": "product",
                "identity_id": str(margarine.id),
                "alias": "smør",
                "locale": "nb",
                "context": "recipe",
                "reviewed": False,
            },
        )
        assert unreviewed.status_code == 422

        reassigned = client.post(
            "/api/catalogue/aliases/correct",
            json={
                "kind": "product",
                "identity_id": str(margarine.id),
                "alias": "smør",
                "locale": "nb",
                "context": "recipe",
                "reviewed": True,
            },
        )
        assert reassigned.status_code == 200
        body = reassigned.json()
        assert body["status"] == "updated"
        assert body["event"]["previous_identity_id"] == str(butter.id)
        assert body["event"]["new_identity_id"] == str(margarine.id)

        # Re-assign back to butter so the MCP assertion below keeps checking butter -> margarine
        reverted = client.post(
            "/api/catalogue/aliases/correct",
            json={
                "kind": "product",
                "identity_id": str(butter.id),
                "alias": "smør",
                "locale": "nb",
                "context": "recipe",
                "reviewed": True,
            },
        )
        assert reverted.status_code == 200

    mcp = FastMCP("test-kitchen")
    install_feature_tools(mcp, features, basket=SimpleNamespace())
    correct_tool = await mcp.get_tool("correct_identity_mapping")
    list_tool = await mcp.get_tool("list_mapping_corrections")

    # Requires explicit reviewed=True confirmation
    with pytest.raises(ToolError, match="reviewed=true"):
        correct_tool.fn(
            AliasCorrectionInput(
                kind="product",
                identity_id=margarine.id,
                alias="smør",
                locale="nb",
                context="recipe",
                reviewed=False,
            )
        )

    token = history_context.set(
        SimpleNamespace(actor_id="mcp-user", channel="mcp", session_id=None)
    )
    try:
        out = correct_tool.fn(
            AliasCorrectionInput(
                kind="product",
                identity_id=margarine.id,
                alias="smør",
                locale="nb",
                context="recipe",
                reviewed=True,
            )
        )
        assert out["status"] == "updated"
        assert out["event"]["actor_id"] == "mcp-user"
        assert out["event"]["channel"] == "mcp"
        assert out["event"]["previous_identity_id"] == str(butter.id)

        listed = list_tool.fn(kind="product", alias="smør")
        assert len(listed) == 4
    finally:
        history_context.reset(token)
