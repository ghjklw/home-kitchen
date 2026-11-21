"""Pure unit tests and AST import-boundary checks for Step 6 adapter consolidation."""

from __future__ import annotations

import ast
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from home_kitchen.bootstrap import (
    create_in_memory_cookidoo_plan_sync_service,
    create_in_memory_purchase_sync_service,
)
from home_kitchen.connector_ports import ConnectorStatus
from home_kitchen.connector_store import MemoryConnectorStore
from home_kitchen.cookidoo_plan_sync import (
    CookidooPlanSyncConfirmRequest,
    CookidooPlanSyncPreviewRequest,
    SyncEditTarget,
)
from home_kitchen.crypto import resolve_connector_cipher, resolve_oda_cipher
from home_kitchen.integrations import (
    AIIntegration,
    CookidooIntegration,
    CoopIntegration,
    HomeAssistantIntegration,
    IntegrationRegistry,
    KassalIntegration,
    OdaIntegration,
    Rema1000Integration,
    TrumfIntegration,
)
from home_kitchen.models import Ingredient, Meal, Recipe, RecipeSource
from home_kitchen.oda.cart import OdaCartService
from home_kitchen.oda.service import OdaService
from home_kitchen.purchase_sync import (
    CheckpointConflict,
    ProviderOrderRecord,
    ProviderPage,
    RevisionConflict,
    sync_account,
)
from home_kitchen.recipes.bootstrap import create_in_memory_cookidoo_recipe_service
from home_kitchen.recipes.collections import CollectionEntry
from home_kitchen.settings import Settings


@pytest.mark.parametrize(
    ("relative_path", "forbidden_prefixes"),
    [
        (
            "src/home_kitchen/connector_ports.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.connector_store",
                "home_kitchen.oda",
            ),
        ),
        (
            "src/home_kitchen/integrations.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.connector_store",
                "home_kitchen.oda",
                "home_kitchen.settings",
            ),
        ),
        (
            "src/home_kitchen/connector_auth.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.connector_store",
            ),
        ),
        (
            "src/home_kitchen/cookidoo_recipes.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.connector_store",
                "home_kitchen.recipes.adapters.postgres",
            ),
        ),
        (
            "src/home_kitchen/cookidoo_plan_sync.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.connector_store",
                "home_kitchen.recipes.adapters.postgres",
            ),
        ),
        (
            "src/home_kitchen/purchase_sync.py",
            (
                "sqlalchemy",
                "home_kitchen.service",
                "home_kitchen.store",
                "home_kitchen.history",
                "home_kitchen.connector_store",
                "home_kitchen.oda",
                "home_kitchen.kassal",
            ),
        ),
        (
            "src/home_kitchen/coop_history.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/trumf_history.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/rema1000_history.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/kassal.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/home_assistant.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/imports.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/recommendations.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/oda/models.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/oda/media.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/oda/purchase_history.py",
            ("sqlalchemy", "home_kitchen.service", "home_kitchen.store", "home_kitchen.history"),
        ),
        (
            "src/home_kitchen/connector_store.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/oda/repository.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/oda/service.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/oda/cart.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/purchase_sync_adapters.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
        (
            "src/home_kitchen/cookidoo_adapters.py",
            ("home_kitchen.service",),
        ),
        (
            "src/home_kitchen/history.py",
            ("home_kitchen.service", "home_kitchen.store"),
        ),
    ],
)
def test_step6_modules_have_no_forbidden_imports(
    relative_path: str, forbidden_prefixes: tuple[str, ...]
) -> None:
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for mod in imported:
        assert not any(
            mod == prefix or mod.startswith(f"{prefix}.") for prefix in forbidden_prefixes
        ), f"{relative_path} imports forbidden module {mod}"


def test_uniform_cipher_resolution_helpers() -> None:
    k1 = Fernet.generate_key().decode("ascii")
    k2 = Fernet.generate_key().decode("ascii")

    unconfigured = SimpleNamespace(
        connector_encryption_key=None,
        oda_encryption_key=None,
        local_dev=False,
    )
    assert resolve_connector_cipher(unconfigured) is None
    assert resolve_oda_cipher(unconfigured) is None

    dev_settings = Settings(local_dev=True)
    dev_cipher = resolve_connector_cipher(dev_settings)
    assert dev_cipher is not None
    assert resolve_oda_cipher(dev_settings) is None

    rotated = SimpleNamespace(
        local_dev=False,
        connector_encryption_key=SecretStr(f"{k1},{k2}"),
        oda_encryption_key=SecretStr(k2),
    )
    conn_cipher = resolve_connector_cipher(rotated)
    oda_cipher = resolve_oda_cipher(rotated)
    assert conn_cipher is not None
    assert oda_cipher is not None
    token_from_old = Fernet(k2.encode("ascii")).encrypt(b"secret")
    assert conn_cipher.decrypt(token_from_old) == b"secret"
    assert oda_cipher.decrypt(token_from_old) == b"secret"


def test_memory_connector_store_and_integration_registry() -> None:
    key = Fernet.generate_key().decode("ascii")
    store = MemoryConnectorStore(cipher=key)
    hid = uuid4()

    status = store.save_connection(
        hid,
        "cookidoo",
        "oauth_password",
        {"access_token": "tok-123", "country": "no"},
        actor_id="malo",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )
    assert isinstance(status, ConnectorStatus)
    assert store.get_credentials(hid, "cookidoo") == {
        "access_token": "tok-123",
        "country": "no",
    }

    store.save_connection(
        hid,
        "trumf",
        "session_cookie",
        {"session_cookie": "trumf-cookie"},
        actor_id="malo",
        metadata={"has_session_cookie": True},
    )

    class _StubOda:
        def status(self, actor_id: str) -> dict[str, Any]:
            return {"connected": actor_id == "malo", "configured": True}

    settings = Settings(local_dev=True)
    registry = IntegrationRegistry(
        [
            OdaIntegration(_StubOda()),
            AIIntegration(settings),
            Rema1000Integration(store, hid),
            CoopIntegration(store, hid),
            TrumfIntegration(store, hid),
            CookidooIntegration(store, hid),
            KassalIntegration(store, hid, settings=settings),
            HomeAssistantIntegration(store, hid, settings=settings),
        ]
    )

    infos = {item.id: item for item in registry.describe("malo")}
    assert infos["oda"].status == "connected"
    assert infos["cookidoo"].status == "connected"
    assert infos["cookidoo"].token_status == "expiring_soon"
    assert infos["trumf"].refresh_available is True
    assert infos["rema1000"].status == "not_configured"


def test_in_memory_cookidoo_recipe_service_atomic_import_dedup_and_lock() -> None:
    key = Fernet.generate_key().decode("ascii")
    store = MemoryConnectorStore(cipher=key)
    hid = uuid4()
    store.save_connection(
        hid,
        "cookidoo",
        "oauth_password",
        {"access_token": "tok-abc-123", "country": "no"},
        actor_id="malo",
    )

    class _StubIdentityResolver:
        def resolve_recipe_identities(self, recipe: Recipe) -> Recipe:
            return recipe.model_copy(update={"title": f"{recipe.title} [resolved]"})

    service, uow, _ = create_in_memory_cookidoo_recipe_service(
        store=store,
        household_id=hid,
        identity_resolver=_StubIdentityResolver(),
    )

    gen = service.provider.generation("malo")
    service.provider.lock_generation(None, "malo", gen)

    entry = CollectionEntry(
        id="r-mem-1",
        provider="cookidoo",
        title="Fiskesuppe",
        source_url="https://cookidoo.international/recipes/recipe/nb-NO/r-mem-1",
        servings=4,
        ingredients=["400 g torsk", "2 dl fløte"],
        instructions=["Kok opp"],
    )

    with pytest.raises(ValueError, match="Gjennomgå"):
        service.import_recipe_atomic("malo", entry, reviewed=False)

    recipe_id, existed = service.import_recipe_atomic("malo", entry, reviewed=True)
    assert existed is False
    assert uow.recipes[recipe_id].title == "Fiskesuppe [resolved]"
    assert uow.recipes[recipe_id].source == RecipeSource.COOKIDOO
    assert ("cookidoo", "r-mem-1") in uow.source_links
    assert len(uow.history_entries) == 1

    # Duplicate import retains original recipe without overwriting
    second_id, existed_second = service.import_recipe_atomic("malo", entry, reviewed=True)
    assert existed_second is True
    assert second_id == recipe_id
    assert len(uow.history_entries) == 1

    # Changing credentials invalidates prior generation token
    store.save_connection(
        hid,
        "cookidoo",
        "oauth_password",
        {"access_token": "tok-rotated-999", "country": "no"},
        actor_id="malo",
    )
    with pytest.raises(ValueError, match="endret"):
        service.provider.lock_generation(None, "malo", gen)


@pytest.mark.asyncio
async def test_in_memory_cookidoo_plan_sync_roundtrip_and_guards() -> None:
    store = MemoryConnectorStore()
    hid = uuid4()
    store.save_connection(
        hid,
        "cookidoo",
        "oauth_password",
        {"access_token": "tok-sync-1", "country": "no"},
        actor_id="malo",
    )

    posted_payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        if request.method == "GET" and request.url.path == "/planner/en/api/my-week":
            return httpx.Response(
                200,
                json={
                    "weeks": [
                        {
                            "days": [
                                {
                                    "id": "2026-11-23",
                                    "recipes": [
                                        {
                                            "entryId": "mw-10",
                                            "recipeId": "r-mw-1",
                                            "title": "Kremet kyllingsuppe",
                                            "servings": 4,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path == "/recipes/recipe/en/r-mw-1":
            return httpx.Response(
                200,
                json={
                    "id": "r-mw-1",
                    "title": "Kremet kyllingsuppe",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [{"recipeIngredients": ["500 g kylling"]}],
                    "recipeStepGroups": [{"recipeSteps": [{"formattedText": "Kok suppe"}]}],
                },
            )
        if request.method == "POST" and request.url.path == "/planner/en/api/my-week":
            assert body is not None
            posted_payloads.append(body)
            return httpx.Response(
                201,
                json={
                    "entryId": "mw-created-1",
                    "recipeId": body["recipeId"],
                    "date": body["date"],
                    "title": "Kremet kyllingsuppe",
                    "servings": body.get("servings", 4),
                },
            )
        return httpx.Response(404)

    service, plan_uow, collection_uow, _ = create_in_memory_cookidoo_plan_sync_service(
        store=store,
        household_id=hid,
        transport=httpx.MockTransport(handler),
    )

    # Seed an existing meal on 2026-11-24 to test edit preview & confirm
    existing_recipe = Recipe(
        id=uuid4(),
        title="Gammel suppe",
        servings=2,
        ingredients=[Ingredient(name="Potet", quantity=Decimal("200"), unit="g")],
        instructions=["Kok"],
        source=RecipeSource.MANUAL,
    )
    collection_uow.recipes[existing_recipe.id] = existing_recipe
    existing_meal = Meal(
        id=uuid4(),
        date=date(2026, 11, 24),
        recipe_id=existing_recipe.id,
        servings=2,
        label="Middag",
    )
    plan_uow.meals[existing_meal.id] = existing_meal

    preview = await service.preview(
        "malo",
        CookidooPlanSyncPreviewRequest(
            direction="cookidoo_to_kitchen",
            start=date(2026, 11, 23),
            end=date(2026, 11, 29),
            edits=[
                SyncEditTarget(
                    source_key="mw-10",
                    target_meal_id=existing_meal.id,
                )
            ],
        ),
    )
    assert len(preview.edits) == 1
    confirmed = await service.confirm(
        "malo",
        preview.preview_id,
        CookidooPlanSyncConfirmRequest(reviewed=True),
    )
    assert confirmed.status == "applied"
    assert confirmed.applied_count == 1
    assert plan_uow.meals[existing_meal.id].date == date(2026, 11, 23)
    assert plan_uow.meals[existing_meal.id].servings == 4
    assert ("cookidoo", "r-mw-1") in collection_uow.source_links
    assert len(plan_uow.history_entries) == 1

    # Export linked meal back to Cookidoo My Week
    export_preview = await service.preview(
        "malo",
        CookidooPlanSyncPreviewRequest(
            direction="kitchen_to_cookidoo",
            start=date(2026, 11, 23),
            end=date(2026, 11, 29),
        ),
    )
    assert len(export_preview.additions) == 1
    export_done = await service.confirm(
        "malo",
        export_preview.preview_id,
        CookidooPlanSyncConfirmRequest(reviewed=True),
    )
    assert export_done.applied_count == 1
    assert len(posted_payloads) == 1
    assert posted_payloads[0]["recipeId"] == "r-mw-1"


def test_in_memory_purchase_sync_service_and_conflicts() -> None:
    hid = uuid4()
    service, order_store, connector_store = create_in_memory_purchase_sync_service(
        household_id=hid,
    )
    observed = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)

    class _StubAdapter:
        account_id = "acc-1"

        def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
            assert account_id == "acc-1"
            if cursor is None:
                return ProviderPage(
                    records=[
                        ProviderOrderRecord(
                            external_id="ord-1",
                            revision_id="rev-1",
                            source_updated_at=observed,
                            payload={
                                "purchased_at": observed.isoformat(),
                                "lines": [{"name": "Melk", "quantity": 2, "unit": "l"}],
                            },
                        )
                    ],
                    has_more=False,
                    next_cursor="cursor-1",
                )
            return ProviderPage(records=[], has_more=False, next_cursor="cursor-1")

    connector_store.save_connection(
        hid, "trumf", "session_cookie", {"session_cookie": "abc"}, account_id="acc-1"
    )
    service.register_adapter("trumf", _StubAdapter(), account_id="acc-1")

    res = service.sync_provider("trumf", account_id="acc-1")
    assert res.status == "success"
    assert res.records_synced == 1
    assert res.checkpoint == "cursor-1"

    merged = service.get_merged_records("trumf")
    assert len(merged) == 1
    assert merged[0]["external_id"] == "ord-1"

    ctx = service.get_bought_before_context()
    assert len(ctx.bought_before) == 1
    assert ctx.bought_before[0].product_name == "Melk"

    status = service.get_provider_status("trumf", account_id="acc-1")
    assert status.coverage_window.total_records == 1

    # Conflict guards on MemoryPurchaseOrderStore
    with pytest.raises(CheckpointConflict):
        order_store.persist_page(
            "trumf",
            "acc-1",
            ProviderPage(records=[], has_more=False, next_cursor="cursor-2"),
            expected_cursor="stale-cursor",
        )

    with pytest.raises(RevisionConflict):
        order_store.persist_page(
            "trumf",
            "acc-1",
            ProviderPage(
                records=[
                    ProviderOrderRecord(
                        external_id="ord-1",
                        revision_id="rev-1",
                        source_updated_at=observed,
                        payload={"tampered": True},
                    )
                ],
                has_more=False,
                next_cursor="cursor-2",
            ),
            expected_cursor="cursor-1",
        )

    # sync_account with EAN triggers injected kassal_client_factory
    seen_keys: list[str] = []

    class _FakeKassal:
        def lookup_ean(self, ean: str) -> SimpleNamespace | None:
            return SimpleNamespace(
                ean=ean,
                name="Tine Helmelk",
                brand="Tine",
                vendor="Tine SA",
                weight=1000,
                weight_unit="g",
                category="Meieri",
                image=None,
            )

    def _kassal_factory(api_key: str) -> _FakeKassal:
        seen_keys.append(api_key)
        return _FakeKassal()

    connector_store.save_connection(hid, "kassal", "api_key", {"api_key": "kassal-test-key"})

    class _EanAdapter:
        def fetch_page(self, account_id: str, cursor: str | None) -> ProviderPage:
            return ProviderPage(
                records=[
                    ProviderOrderRecord(
                        external_id="ord-ean-1",
                        revision_id="rev-1",
                        source_updated_at=observed,
                        payload={"lines": [{"ean": "7038010000188", "name": "Melk"}]},
                    )
                ],
                has_more=False,
                next_cursor="ean-cursor-1",
            )

    synced_ean = sync_account(
        order_store,
        "coop",
        "acc-ean",
        _EanAdapter(),
        kassal_client_factory=_kassal_factory,
        connector_store=connector_store,
        household_id=hid,
    )
    assert synced_ean == 1
    assert seen_keys == ["kassal-test-key"]


def test_oda_service_and_cart_accept_injected_ports() -> None:
    class _StubOdaRepo:
        def has_connection(self, actor_id: str) -> bool:
            return actor_id == "malo"

        def connection_generation(self, actor_id: str) -> int | None:
            return 7 if actor_id == "malo" else None

    settings = Settings(local_dev=True)
    oda = OdaService(None, settings, repository=_StubOdaRepo())  # type: ignore[arg-type]
    assert oda.status("malo")["connected"] is False  # public_url not set -> not configured
    oda_cfg = OdaService(
        None,
        Settings(local_dev=True, public_url="http://localhost:8000"),  # type: ignore[arg-type]
        repository=_StubOdaRepo(),  # type: ignore[arg-type]
    )
    assert oda_cfg.status("malo")["connected"] is True

    recorded: list[tuple[Any, str | None]] = []

    class _StubHistory:
        def record(self, data: Any, actor_id: str | None = None, connection: Any = None) -> Any:
            recorded.append((data, actor_id))
            return data

    cart = OdaCartService(oda_cfg, history_repository=_StubHistory())
    cart._record(
        None,
        "malo",
        "prop-1",
        {"additions": [{"product": {"id": 42}, "quantity": 2}]},
        "proposed",
    )
    assert len(recorded) == 1
    assert recorded[0][1] == "malo"
