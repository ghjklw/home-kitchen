"""Unit tests for meal-plan recommendations with large recipe catalogs."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import UsageLimits
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from home_kitchen.context import (
    BoughtBeforeItem,
    PurchaseHistoryContext,
    build_purchase_history_context,
)
from home_kitchen.models import Ingredient, Recipe
from home_kitchen.purchase_sync import (
    ProviderOrderRecord,
    ProviderPage,
    persist_page,
    resolve_effective_orders,
)
from home_kitchen.purchase_sync import (
    metadata as purchase_metadata,
)
from home_kitchen.recommendations import PlanDraft, build_planner, recommend_plan
from home_kitchen.settings import Household, Settings

pytestmark = pytest.mark.mocked_provider


def _make_recipe(title: str, ingredient_count: int = 3) -> Recipe:
    return Recipe(
        id=uuid4(),
        title=title,
        servings=4,
        ingredients=[
            Ingredient(name=f"ingrediens_{idx}_{title}", quantity="100", unit="g")
            for idx in range(ingredient_count)
        ],
        instructions=["Kok opp og server"],
    )


@pytest.mark.asyncio
async def test_recommend_plan_uses_high_token_limit_and_compacts_ingredients():
    recipes = [_make_recipe(f"Oppskrift {idx}", ingredient_count=12) for idx in range(200)]
    captured_prompt: str | None = None
    captured_limits: UsageLimits | None = None

    class CaptureAgent:
        async def run(self, prompt, **kwargs):
            nonlocal captured_prompt, captured_limits
            captured_prompt = prompt
            captured_limits = kwargs.get("usage_limits")
            res = MagicMock()
            res.output = PlanDraft(
                meals=[
                    {
                        "date": "2026-04-06",
                        "slot": "dinner",
                        "recipe_id": str(recipes[0].id),
                        "servings": 4,
                    }
                ]
            )
            return res

    draft = await recommend_plan(
        CaptureAgent(),  # type: ignore[arg-type]
        Household(),
        recipes,
        date(2026, 4, 6),
        days=7,
    )

    assert len(draft.meals) == 1
    assert captured_limits is not None
    assert captured_limits.total_tokens_limit is not None
    assert captured_limits.total_tokens_limit >= 100000
    assert captured_prompt is not None
    assert "ingrediens_0_Oppskrift 0" in captured_prompt
    assert "ingrediens_5_Oppskrift 0" in captured_prompt
    assert "(+6)" in captured_prompt
    assert "ingrediens_6_Oppskrift 0" not in captured_prompt
    assert "ingrediens_11_Oppskrift 0" not in captured_prompt


def test_build_purchase_history_context_surfaces_active_bought_before_only():
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    rows = [
        {
            "id": "1",
            "provider": "oda",
            "account_id": "malo",
            "external_id": "oda-delivered-1",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "order": {
                    "orderNumber": "oda-delivered-1",
                    "deliveryDate": "2026-11-18",
                    "storeName": "Oda",
                    "products": [
                        {
                            "product": {"id": 101, "name": "Tine Helmelk 1L", "unitName": "l"},
                            "quantity": 2,
                        }
                    ],
                },
                "tracking": {"orderNumber": "oda-delivered-1", "status": "delivered"},
            },
        },
        {
            "id": "2",
            "provider": "oda",
            "account_id": "malo",
            "external_id": "oda-cancelled-1",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "order": {
                    "orderNumber": "oda-cancelled-1",
                    "deliveryDate": "2026-11-19",
                    "products": [
                        {
                            "product": {"id": 202, "name": "Kansellert Ost", "unitName": "stk"},
                            "quantity": 1,
                        }
                    ],
                },
                "tracking": {"orderNumber": "oda-cancelled-1", "status": "cancelled"},
            },
        },
        {
            "id": "3",
            "provider": "trumf",
            "account_id": "malo",
            "external_id": "trumf-stub-1",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "batchId": "trumf-stub-1",
                "storeName": "MENY",
                "transaksjonsTidspunkt": "2026-11-17T10:00:00Z",
                "hasReceipt": False,
                "varelinjer": [{"produktBeskrivelse": "Uten kvittering", "antall": 1, "belop": 10}],
            },
        },
        {
            "id": "4",
            "provider": "rema1000",
            "account_id": "malo",
            "external_id": "rema-partial-ret",
            "revision_id": "r1",
            "source_updated_at": None,
            "observed_at": now,
            "payload": {
                "receiptId": "rema-partial-ret",
                "storeName": "REMA 1000 Vulkan",
                "timestamp": "2026-11-19T14:00:00Z",
                "hasReceipt": True,
                "lines": [
                    {"name": "Gulrot 1kg", "quantity": 2, "unit": "kg", "amount": 50},
                    {"name": "Gulrot 1kg", "quantity": -1, "unit": "kg", "amount": -25},
                    {"name": "Fullt Returnert Brød", "quantity": 1, "unit": "stk", "amount": 35},
                    {"name": "Fullt Returnert Brød", "quantity": -1, "unit": "stk", "amount": -35},
                ],
            },
        },
    ]

    effective = resolve_effective_orders(rows)
    ctx = build_purchase_history_context(effective)

    names = {item.product_name for item in ctx.bought_before}
    assert "Tine Helmelk 1L" in names
    assert "Gulrot 1kg" in names
    assert "Kansellert Ost" not in names
    assert "Uten kvittering" not in names
    assert "Fullt Returnert Brød" not in names

    gulrot = next(item for item in ctx.bought_before if item.product_name == "Gulrot 1kg")
    assert gulrot.last_quantity == "1"
    assert gulrot.provenance == "provider_verified"
    assert "never assert current stock" in ctx.boundary_note.lower()


@pytest.mark.asyncio
async def test_recommend_plan_includes_bought_before_context_and_boundary():
    item = _make_recipe("Fiskegrateng", ingredient_count=2)
    captured_prompt: str | None = None

    class CaptureAgent:
        async def run(self, prompt, **kwargs):
            nonlocal captured_prompt
            captured_prompt = prompt
            res = MagicMock()
            res.output = PlanDraft(
                meals=[
                    {
                        "date": "2026-04-06",
                        "slot": "dinner",
                        "recipe_id": str(item.id),
                        "servings": 4,
                    }
                ]
            )
            return res

    planner = build_planner(Settings(local_dev=True), TestModel())
    joined_instructions = " ".join(str(x) for x in planner._instructions)
    assert "Bought-before purchase history" in joined_instructions
    assert "never to assert current stock" in joined_instructions

    purchase_ctx = PurchaseHistoryContext(
        source_providers=["oda"],
        bought_before=[
            BoughtBeforeItem(
                provider="oda",
                product_name="Fersk Torsk",
                purchase_count=3,
                last_quantity="500",
                unit="g",
            )
        ],
    )
    await recommend_plan(
        CaptureAgent(),  # type: ignore[arg-type]
        Household(),
        [item],
        date(2026, 4, 6),
        days=7,
        purchase_history_context=purchase_ctx,
    )
    assert captured_prompt is not None
    assert "Bought-before purchase history" in captured_prompt
    assert "Fersk Torsk" in captured_prompt
    assert "never assert current stock" in captured_prompt.lower()


@pytest.mark.asyncio
async def test_features_surface_bought_before_in_evidence_and_recommend_week_without_polluting_discovery(
    monkeypatch,
):
    from home_kitchen.catalogue.identity_service import CanonicalIdentityService
    from home_kitchen.connector_store import metadata as connector_metadata
    from home_kitchen.features import KitchenFeatures
    from home_kitchen.service import KitchenService

    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    connector_metadata.create_all(engine)
    purchase_metadata.create_all(engine)

    class _TestStore:
        def __init__(self, eng):
            self.engine = eng
            self.settings: dict[str, str] = {}
            self.photo_household_id = None

        def ensure_setting(self, key: str, value: str) -> str:
            return self.settings.setdefault(key, value)

        def list(self, kind: str, model: type) -> list:
            return []

    recipe = _make_recipe("Tomatsuppe", ingredient_count=2)
    kitchen = MagicMock(spec=KitchenService)
    kitchen.store = _TestStore(engine)
    kitchen.household_id = uuid4()
    kitchen.settings = Settings(local_dev=True)
    kitchen.identities = MagicMock(spec=CanonicalIdentityService)
    kitchen.identities.list_correction_events.return_value = []
    kitchen.list_recipes.return_value = [recipe]
    kitchen.list_meals.return_value = []

    features = KitchenFeatures(kitchen, Settings(local_dev=True), Household)
    features.history = MagicMock()
    features.history.engine = None
    features.history.search.return_value = []
    features.inventory = MagicMock()
    features.inventory.list.return_value = []

    persist_page(
        engine,
        "coop",
        "malo",
        ProviderPage(
            records=[
                ProviderOrderRecord(
                    external_id="coop-bought-1",
                    revision_id="rev-1",
                    payload={
                        "summary": {
                            "summaryId": "coop-bought-1",
                            "receiptId": "rcpt-1",
                            "purchaseDate": "2026-11-18T12:00:00Z",
                            "storeName": "Coop Mega",
                            "amount": "55.00",
                            "lines": [
                                {"productName": "Hakkede Tomater", "quantity": 2, "amount": 30}
                            ],
                        }
                    },
                ),
                ProviderOrderRecord(
                    external_id="coop-refund-1",
                    revision_id="rev-1",
                    payload={
                        "summary": {
                            "summaryId": "coop-refund-1",
                            "receiptId": "rcpt-ref-1",
                            "purchaseDate": "2026-11-19T12:00:00Z",
                            "storeName": "Coop Mega",
                            "amount": "-30.00",
                            "lines": [
                                {"productName": "Refundert Vare", "quantity": -1, "amount": -30}
                            ],
                        }
                    },
                ),
            ],
            next_cursor="cur-1",
            has_more=False,
        ),
        expected_cursor=None,
    )

    try:
        evidence = features.evidence("tomat")
        assert any("Hakkede Tomater" in snippet.text for snippet in evidence)
        assert not any("Refundert Vare" in snippet.text for snippet in evidence)

        # Discovery context must NOT treat bought-before history as explicit likes/dislikes
        disc_ctx = features.discovery_context("tomat")
        assert disc_ctx.likes == []
        assert disc_ctx.dislikes == []

        captured_prompts: list[str] = []

        class CaptureAgent:
            async def run(self, prompt, **kwargs):
                captured_prompts.append(prompt)
                res = MagicMock()
                res.output = PlanDraft(
                    meals=[
                        {
                            "date": "2026-11-23",
                            "slot": "dinner",
                            "recipe_id": str(recipe.id),
                            "servings": 4,
                        }
                    ]
                )
                return res

        monkeypatch.setattr("home_kitchen.features.build_planner", lambda _s: CaptureAgent())
        draft = await features.recommend_week(date(2026, 11, 23), days=7)
        assert any(eid.startswith("purchase_history:coop:") for eid in draft.evidence_ids)
        assert captured_prompts
        assert "Hakkede Tomater" in captured_prompts[0]
        assert "Refundert Vare" not in captured_prompts[0]
    finally:
        engine.dispose()
