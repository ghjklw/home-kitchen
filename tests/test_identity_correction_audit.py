"""Tests for auditable product and unit mapping corrections across app/API and MCP."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic_ai.models.test import TestModel
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from home_kitchen.basket.matching import (
    BasketMatchingService,
    ProviderProduct,
    ProviderSearchGroup,
)
from home_kitchen.catalogue.identity import AliasCorrectionInput, IdentityCorrectionCitation
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.catalogue.seeds import _seed_products_into_repo, _seed_units_into_repo
from home_kitchen.context import history_context
from home_kitchen.feature_api import feature_router
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.inventory import PurchasePlan, PurchasePlanLine
from home_kitchen.models import Ingredient, Recipe, RecipeCreate
from home_kitchen.recommendations import build_planner, recommend_plan
from home_kitchen.service import KitchenService
from home_kitchen.settings import Household, Settings

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
                    household_id TEXT,
                    locale TEXT NOT NULL,
                    context TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    reviewed BOOLEAN NOT NULL DEFAULT 0,
                    source_type TEXT NOT NULL DEFAULT 'reviewed_correction'
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE UNIQUE INDEX uq_kitchen_product_aliases_global
                ON kitchen_product_aliases (locale, context, normalized_alias, product_id)
                WHERE household_id IS NULL"""
            )
        )
        conn.execute(
            text(
                """CREATE UNIQUE INDEX uq_kitchen_product_aliases_household
                ON kitchen_product_aliases (household_id, locale, context, normalized_alias, product_id)
                WHERE household_id IS NOT NULL"""
            )
        )
        conn.execute(
            text(
                """CREATE TABLE kitchen_unit_aliases (
                    household_id TEXT,
                    locale TEXT NOT NULL,
                    context TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    alias TEXT NOT NULL,
                    unit_id TEXT NOT NULL,
                    reviewed BOOLEAN NOT NULL DEFAULT 0,
                    source_type TEXT NOT NULL DEFAULT 'reviewed_correction'
                )"""
            )
        )
        conn.execute(
            text(
                """CREATE UNIQUE INDEX uq_kitchen_unit_aliases_global
                ON kitchen_unit_aliases (locale, context, normalized_alias, unit_id)
                WHERE household_id IS NULL"""
            )
        )
        conn.execute(
            text(
                """CREATE UNIQUE INDEX uq_kitchen_unit_aliases_household
                ON kitchen_unit_aliases (household_id, locale, context, normalized_alias, unit_id)
                WHERE household_id IS NOT NULL"""
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


def test_household_correction_does_not_overwrite_global_or_other_households(identity_engine):
    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)
    fresh_tom = identities.create_product("tomato", "fresh")
    canned_tom = identities.create_product("tomato", "canned")
    gram = identities.create_unit("gram")
    kilogram = identities.create_unit("kilogram")

    identities.add_alias("product", "nb", "recipe", "tomat", fresh_tom.id)
    identities.add_alias("unit", "nb", "recipe", "gr", gram.id)

    household_a = uuid4()
    household_b = uuid4()

    # Household A overrides "tomat" to canned_tom and "gr" to kilogram
    ev_prod = identities.correct_alias(
        "product",
        "nb",
        "recipe",
        "tomat",
        canned_tom.id,
        actor_id="alice",
        household_id=household_a,
        channel="app",
    )
    ev_unit = identities.correct_alias(
        "unit",
        "nb",
        "recipe",
        "gr",
        kilogram.id,
        actor_id="alice",
        household_id=household_a,
        channel="mcp",
    )
    assert ev_prod is not None
    assert ev_unit is not None

    # Household B overrides "tomat" to the SAME fresh_tom as global (no unique constraint clash)
    ev_b = identities.correct_alias(
        "product",
        "nb",
        "recipe",
        "tomat",
        fresh_tom.id,
        actor_id="bob",
        household_id=household_b,
        channel="api",
    )
    assert ev_b is not None

    # Household A sees its overrides
    res_a_prod = identities.resolve("product", "nb", "recipe", "tomat", household_id=household_a)
    assert res_a_prod.status == "resolved"
    assert res_a_prod.candidates == [canned_tom]
    assert res_a_prod.household_scoped is True
    assert res_a_prod.source_type == "reviewed_correction"
    assert res_a_prod.reviewed is True

    res_a_unit = identities.resolve("unit", "nb", "recipe", "gr", household_id=household_a)
    assert res_a_unit.status == "resolved"
    assert res_a_unit.candidates == [kilogram]
    assert res_a_unit.household_scoped is True
    assert res_a_unit.source_type == "reviewed_correction"

    # Household B sees its own product override and falls back to global for unit
    res_b_prod = identities.resolve("product", "nb", "recipe", "tomat", household_id=household_b)
    assert res_b_prod.candidates == [fresh_tom]
    assert res_b_prod.household_scoped is True
    res_b_unit = identities.resolve("unit", "nb", "recipe", "gr", household_id=household_b)
    assert res_b_unit.candidates == [gram]
    assert res_b_unit.household_scoped is False
    assert res_b_unit.source_type == "automatic_suggestion"

    # Global vocabulary remains intact and unreviewed
    res_global_prod = identities.resolve("product", "nb", "recipe", "tomat")
    assert res_global_prod.candidates == [fresh_tom]
    assert res_global_prod.household_scoped is False
    assert res_global_prod.source_type == "automatic_suggestion"
    assert res_global_prod.reviewed is False

    res_global_unit = identities.resolve("unit", "nb", "recipe", "gr")
    assert res_global_unit.candidates == [gram]
    assert res_global_unit.household_scoped is False
    assert res_global_unit.source_type == "automatic_suggestion"

    # Audit filtering by household_id stays isolated
    assert len(identities.list_correction_events(household_id=household_a)) == 2
    assert len(identities.list_correction_events(household_id=household_b)) == 1
    assert len(identities.list_correction_events()) == 3


def test_source_type_origins_for_add_alias_seeds_and_map_unmapped_ingredient(identity_engine):
    from home_kitchen.catalogue.seeds import ReferenceProduct, ReferenceUnit

    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)

    # 1. seeds.py -> imported_alias
    _seed_units_into_repo(
        repo,
        (
            ReferenceUnit(
                canonical_name="deciliter",
                physical_dimension="volume",
                bokmal_aliases=["dl"],
                english_aliases=[],
            ),
        ),
    )
    _seed_products_into_repo(
        repo,
        (
            ReferenceProduct(
                canonical_name="oat",
                variant="rolled",
                category="dry",
                bokmal_aliases=["havregryn"],
                english_aliases=[],
            ),
        ),
    )
    seeded_prod_res = identities.resolve("product", "nb", "", "havregryn")
    assert seeded_prod_res.status == "resolved"
    assert seeded_prod_res.source_type == "imported_alias"
    assert seeded_prod_res.reviewed is False

    seeded_unit_res = identities.resolve("unit", "nb", "", "dl")
    assert seeded_unit_res.status == "resolved"
    assert seeded_unit_res.source_type == "imported_alias"
    assert seeded_unit_res.reviewed is False

    # 2. add_alias default -> automatic_suggestion
    garlic = identities.create_product("garlic", "")
    identities.add_alias("product", "nb", "recipe", "hvitløk", garlic.id)
    auto_res = identities.resolve("product", "nb", "recipe", "hvitløk")
    assert auto_res.status == "resolved"
    assert auto_res.source_type == "automatic_suggestion"

    # 3. map_unmapped_ingredient -> reviewed_correction (household-scoped when household_id set)
    class _MemStore:
        def __init__(self) -> None:
            self.recipes: dict[UUID, Recipe] = {}
            self.engine = None

        def get_setting(self, _key: str) -> str | None:
            return None

        def save(self, kind: str, entity: object) -> None:
            if kind == "recipe" and isinstance(entity, Recipe):
                self.recipes[entity.id] = entity

        def get(self, kind: str, entity_id: UUID, _model: type) -> object:
            if kind == "recipe":
                return self.recipes[entity_id]
            raise KeyError(str(entity_id))

        def list(self, kind: str, _model: type) -> list[object]:
            return list(self.recipes.values()) if kind == "recipe" else []

    household_id = uuid4()
    kitchen = KitchenService(
        store=_MemStore(), identity_service=identities, household_id=household_id
    )
    clove = identities.create_unit("clove")
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Pasta",
            servings=2,
            ingredients=[Ingredient(name="sjalottløk", quantity=Decimal("2"), unit="fedd")],
            instructions=["Cook"],
        )
    )
    shallot = identities.create_product("shallot", "")
    kitchen.map_unmapped_ingredient(
        recipe.id,
        0,
        canonical_product_id=shallot.id,
        canonical_unit_id=clove.id,
        add_alias=True,
    )

    mapped_prod_res = identities.resolve(
        "product", "nb", "recipe", "sjalottløk", household_id=household_id
    )
    assert mapped_prod_res.status == "resolved"
    assert mapped_prod_res.candidates == [shallot]
    assert mapped_prod_res.source_type == "reviewed_correction"
    assert mapped_prod_res.household_scoped is True

    mapped_unit_res = identities.resolve("unit", "nb", "recipe", "fedd", household_id=household_id)
    assert mapped_unit_res.status == "resolved"
    assert mapped_unit_res.candidates == [clove]
    assert mapped_unit_res.source_type == "reviewed_correction"

    # Global remains unmapped for household-scoped map_unmapped_ingredient
    assert identities.resolve("product", "nb", "recipe", "sjalottløk").status == "unmapped"


@pytest.mark.asyncio
async def test_basket_matching_surfaces_identity_correction_citations_separately(identity_engine):
    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)
    carrot = identities.create_product("carrot", "")
    household_id = uuid4()
    ev = identities.correct_alias(
        "product",
        "nb",
        "recipe",
        "gulrot",
        carrot.id,
        actor_id="malo",
        household_id=household_id,
        channel="mcp",
    )
    assert ev is not None

    plan = PurchasePlan(
        start=date(2026, 11, 20),
        end=date(2026, 11, 26),
        lines=[
            PurchasePlanLine(
                id=uuid4(),
                name="gulrot",
                canonical_product_id=carrot.id,
                unit="kg",
                required_quantity=Decimal("2"),
                available_quantity=Decimal("0.5"),
                covered_quantity=Decimal("0.5"),
                to_buy_quantity=Decimal("1.5"),
                origins=["Suppe"],
            )
        ],
        suggestions=[],
    )
    inventory = SimpleNamespace(plan=lambda _s, _e: plan)

    class _Provider:
        async def search_products(
            self, actor_id: str, queries: list[str], page: int = 1, size: int = 5
        ) -> list[ProviderSearchGroup]:
            return [
                ProviderSearchGroup(
                    query=q,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="sku-pref-1",
                            name="Gulrot Favoritt 1 kg",
                            description="1 kg",
                            identity_status="compatible",
                        )
                    ],
                )
                for q in queries
            ]

    service = BasketMatchingService(
        inventory=inventory,
        provider=_Provider(),
        preferences_provider=lambda pid: (
            (["sku-pref-1"], ["sku-bad"]) if pid == carrot.id else ([], [])
        ),
        identity_corrections_provider=lambda alias, _pid: [
            IdentityCorrectionCitation.from_event(item)
            for item in identities.list_correction_events(
                kind="product", alias=alias, household_id=household_id
            )
        ],
        purchase_evidence_provider=lambda pid: (
            [{"provider": "oda", "purchased_at": datetime(2026, 11, 15, tzinfo=UTC).isoformat()}]
            if pid == carrot.id
            else []
        ),
    )

    result = await service.find_products("malo", plan.start, plan.end, plan=plan, size=5)
    assert len(result.needs) == 1
    need = result.needs[0]

    # Identity correction citations are separate from stock, SKU preference, and purchase evidence
    assert len(need.identity_correction_citations) == 1
    assert need.identity_correction_citations[0].event_id == ev.id
    assert need.identity_correction_citations[0].actor_id == "malo"
    assert need.identity_correction_citations[0].channel == "mcp"
    assert need.identity_correction_citations[0].source_type == "reviewed_correction"

    assert need.stock_evidence is not None
    assert need.stock_evidence.available_quantity == Decimal("0.5")
    assert need.sku_preference_evidence is not None
    assert need.sku_preference_evidence.preferred_product_ids == ["sku-pref-1"]
    assert need.sku_preference_evidence.excluded_product_ids == ["sku-bad"]
    assert len(need.purchase_evidence) == 1

    cand = need.candidates[0]
    assert len(cand.identity_correction_citations) == 1
    assert cand.identity_correction_citations[0].event_id == ev.id
    assert "Foretrukket Oda-vare (rang 1)" in cand.match_reason
    assert "Identitetsretting (reviewed_correction, mcp av malo)" in cand.match_reason


@pytest.mark.asyncio
async def test_recommendations_surface_identity_correction_citations_separately(identity_engine):
    repo = PostgresIdentityRepository(identity_engine)
    identities = CanonicalIdentityService(repo)
    carrot = identities.create_product("carrot", "")
    household_id = uuid4()
    ev = identities.correct_alias(
        "product",
        "nb",
        "recipe",
        "gulrot",
        carrot.id,
        actor_id="malo",
        household_id=household_id,
        channel="app",
    )
    assert ev is not None

    start = date(2026, 11, 23)
    rec = Recipe(
        id=uuid4(),
        title="Gulrotsuppe",
        servings=2,
        ingredients=[Ingredient(name="gulrot", quantity=Decimal("2"), unit="stk")],
        instructions=["Kok"],
    )
    agent = build_planner(
        Settings(local_dev=True),
        TestModel(
            custom_output_args={
                "meal_entries": [
                    {
                        "meal_id": None,
                        "date": start.isoformat(),
                        "recipe_id": str(rec.id),
                        "servings": 2,
                    }
                ],
                "evidence_ids": ["purchase:123", "inventory:456"],
            }
        ),
    )

    draft = await recommend_plan(
        agent,
        Household(),
        [rec],
        start,
        days=7,
        evidence_context='{"kind": "purchase", "id": "purchase:123"}',
        identity_corrections=[ev],
    )
    assert draft.evidence_ids == ["purchase:123", "inventory:456"]
    assert len(draft.identity_correction_citations) == 1
    citation = draft.identity_correction_citations[0]
    assert citation.event_id == ev.id
    assert citation.alias == "gulrot"
    assert citation.channel == "app"
    assert citation.source_type == "reviewed_correction"
