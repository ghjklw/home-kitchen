"""Offline tests for Cookidoo My Week client and plan sync preview/confirm (API + MCP)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import insert, select
from test_cookidoo_recipes import (
    _offline_app,
    _save_cookidoo_connection,
    cipher,  # noqa: F401
    connector_store,  # noqa: F401
    sqlite_engine,  # noqa: F401
)

from home_kitchen.basket.repository import BasketService
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.cookidoo_recipes import CookidooCollectionProvider, CookidooError
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import Meal, RecipeSource
from home_kitchen.recipes.collections import source_links
from home_kitchen.settings import Household
from home_kitchen.store import meals, recipes

pytestmark = pytest.mark.mocked_provider


@pytest.mark.asyncio
async def test_cookidoo_client_my_week_read_and_write_reads_before_add(connector_store):  # noqa: F811
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid, access_token="tok-mw", country="no")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "myDays": [
                        {
                            "id": "2026-11-23",
                            "dayKey": "2026-11-23",
                            "recipes": [
                                {
                                    "entryId": "mw-1",
                                    "id": "r100",
                                    "title": "Fiskesuppe",
                                    "servings": 4,
                                    "assets": {
                                        "images": {
                                            "square": "https://assets.tmecosys.com/image/upload/{transformation}/img/r100"
                                        }
                                    },
                                }
                            ],
                            "customerRecipeIds": [],
                        }
                    ]
                },
            )
        if request.method == "PUT" and request.url.path == "/planning/en/api/my-day":
            recipe_id = body["recipeIds"][0]
            return httpx.Response(
                200,
                json={
                    "message": "Recipe planned!",
                    "content": {
                        "id": body["dayKey"],
                        "dayKey": body["dayKey"],
                        "recipes": [
                            {
                                "entryId": "mw-2" if recipe_id == "r200" else "mw-1",
                                "id": recipe_id,
                                "title": "Tomatsuppe"
                                if recipe_id == "r200"
                                else "Oppdatert fiskesuppe",
                                "servings": 2 if recipe_id == "r200" else 6,
                            }
                        ],
                        "customerRecipeIds": [],
                    },
                    "code": None,
                },
            )
        return httpx.Response(404)

    provider = CookidooCollectionProvider(
        connector_store, hid, transport=httpx.MockTransport(handler)
    )
    entries = await provider.get_my_week_plan(date(2026, 11, 23), date(2026, 11, 29))
    assert len(entries) == 1
    assert entries[0].external_entry_id == "mw-1"
    assert entries[0].cookidoo_recipe_id == "r100"
    assert entries[0].source_url == "https://cookidoo.international/recipes/recipe/nb-NO/r100"
    assert entries[0].image_url == (
        "https://assets.tmecosys.com/image/upload/t_web_rdp_recipe_584x480_1_5x/img/r100"
    )
    assert entries[0].date == date(2026, 11, 23)

    calls.clear()
    added = await provider.add_to_my_week("r200", date(2026, 11, 23), servings=2)
    assert calls[0][0] == "GET"
    assert calls[1][0] == "PUT"
    assert calls[1][1] == "/planning/en/api/my-day"
    assert calls[1][2] == {"dayKey": "2026-11-23", "recipeIds": ["r200"]}
    assert added.external_entry_id == "mw-2"
    assert added.cookidoo_recipe_id == "r200"
    assert added.source_url == "https://cookidoo.international/recipes/recipe/nb-NO/r200"

    # Explicit edit with known entry_id issues PUT /planning/en/api/my-day
    updated = await provider.add_to_my_week("r100", date(2026, 11, 23), servings=6, entry_id="mw-1")
    assert updated.external_entry_id == "mw-1"
    assert updated.servings == 6
    assert calls[-1][0] == "PUT"
    assert calls[-1][1] == "/planning/en/api/my-day"

    # Missing entry_id fails closed with 409 instead of overwriting
    with pytest.raises(CookidooError) as exc_missing:
        await provider.add_to_my_week("r100", date(2026, 11, 23), entry_id="mw-does-not-exist")
    assert exc_missing.value.status_code == 409


def test_rest_api_cookidoo_plan_sync_import_shared_dates_repeats_edits_and_tm6(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()
    existing_recipe_id = uuid4()
    existing_meal_id = uuid4()
    edit_target_meal_id = uuid4()

    with sqlite_engine.begin() as conn:
        conn.execute(
            insert(recipes).values(
                id=existing_recipe_id,
                title="Kjøkkensuppe",
                servings=4,
                ingredients=[
                    {"name": "potet", "quantity": "400", "unit": "g", "category": "Other"}
                ],
                instructions=["Kok potet"],
                source="manual",
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        # Same date 2026-11-23 already has an existing Kitchen meal
        conn.execute(
            insert(meals).values(
                id=existing_meal_id,
                date=date(2026, 11, 23),
                label="Lunsj",
                recipe_id=existing_recipe_id,
                servings=2,
            )
        )
        # Separate meal on 2026-11-24 that will be explicitly targeted by an edit
        conn.execute(
            insert(meals).values(
                id=edit_target_meal_id,
                date=date(2026, 11, 24),
                label="Middag",
                recipe_id=existing_recipe_id,
                servings=2,
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "days": [
                        {
                            "date": "2026-11-23",
                            "recipes": [
                                # Two deliberate entries of the SAME recipe on the SAME date (which also has existing_meal_id)
                                {
                                    "entryId": "mw-a",
                                    "recipeId": "r-tm6",
                                    "title": "TM6 Risotto",
                                    "servings": 4,
                                    "label": "Middag",
                                },
                                {
                                    "entryId": "mw-b",
                                    "recipeId": "r-tm6",
                                    "title": "TM6 Risotto",
                                    "servings": 2,
                                    "label": "Kvelds",
                                },
                            ],
                        },
                        {
                            "date": "2026-11-24",
                            "recipes": [
                                {
                                    "entryId": "mw-c",
                                    "recipeId": "r-tm6",
                                    "title": "TM6 Risotto",
                                    "servings": 6,
                                }
                            ],
                        },
                    ]
                },
            )
        if request.url.path == "/recipes/recipe/en/r-tm6":
            return httpx.Response(
                200,
                json={
                    "id": "r-tm6",
                    "title": "TM6 Risotto",
                    "servingSize": {"quantity": {"value": 4}},
                    "recipeIngredientGroups": [
                        {"recipeIngredients": ["300 g risottoris", "8 dl kraft"]}
                    ],
                    "recipeStepGroups": [
                        {
                            "recipeSteps": [
                                {
                                    "formattedText": "Kok <nobr>15 min/100°C/revers/hastighet 1</nobr>",
                                    "timeSeconds": 900,
                                    "temperature": "100°C",
                                    "speed": "1",
                                    "reverse": True,
                                    "ingredientIndices": [0, 1],
                                }
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(404)

    app, features, history, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    with TestClient(app) as client:
        # 1. Preview import from Cookidoo My Week -> Kitchen with explicit edit on mw-c
        preview_resp = client.post(
            "/api/cookidoo/plan-sync/preview",
            json={
                "direction": "cookidoo_to_kitchen",
                "start": "2026-11-23",
                "end": "2026-11-25",
                "edits": [{"source_key": "mw-c", "target_meal_id": str(edit_target_meal_id)}],
            },
        )
        assert preview_resp.status_code == 200, preview_resp.text
        preview = preview_resp.json()
        assert preview["status"] == "ready"
        # mw-a and mw-b are separate additions despite sharing date with existing_meal_id AND repeating recipe!
        assert len(preview["additions"]) == 2
        assert [a["external_entry_id"] for a in preview["additions"]] == ["mw-a", "mw-b"]
        assert preview["additions"][0]["same_date_existing_count"] == 1
        assert preview["additions"][0]["same_date_existing_titles"] == ["Kjøkkensuppe"]
        assert len(preview["edits"]) == 1
        assert preview["edits"][0]["external_entry_id"] == "mw-c"
        assert preview["edits"][0]["kitchen_meal_id"] == str(edit_target_meal_id)
        assert preview["edits"][0]["before"]["title"] == "Kjøkkensuppe"
        assert preview["edits"][0]["after"]["title"] == "TM6 Risotto"

        # 2. Confirm without reviewed=True is rejected
        denied = client.post(
            f"/api/cookidoo/plan-sync/{preview['preview_id']}/confirm",
            json={"reviewed": False},
        )
        assert denied.status_code == 422

        # 3. Confirm all previewed additions + explicit edit
        confirmed_resp = client.post(
            f"/api/cookidoo/plan-sync/{preview['preview_id']}/confirm",
            json={
                "reviewed": True,
                "selected_ids": [
                    preview["additions"][0]["item_id"],
                    preview["additions"][1]["item_id"],
                    preview["edits"][0]["item_id"],
                ],
            },
        )
        assert confirmed_resp.status_code == 200, confirmed_resp.text
        confirmed = confirmed_resp.json()
        assert confirmed["status"] == "applied"
        assert confirmed["applied_count"] == 3

        # Retry confirm is idempotent
        retry_resp = client.post(
            f"/api/cookidoo/plan-sync/{preview['preview_id']}/confirm",
            json={"reviewed": True},
        )
        assert retry_resp.status_code == 200
        assert retry_resp.json()["applied_count"] == 3

    with sqlite_engine.connect() as conn:
        meal_rows = conn.execute(select(meals)).mappings().all()
        # Original meal on 2026-11-23 was NOT overwritten + 2 repeated additions + 1 edited meal = 4 total
        assert len(meal_rows) == 4
        unchanged_lunch = next(r for r in meal_rows if r["id"] == existing_meal_id)
        assert unchanged_lunch["recipe_id"] == existing_recipe_id
        assert unchanged_lunch["servings"] == 2

        edited_meal = next(r for r in meal_rows if r["id"] == edit_target_meal_id)
        assert edited_meal["servings"] == 6
        assert edited_meal["version"] == 2

        nov23_risottos = [
            r for r in meal_rows if r["date"] == date(2026, 11, 23) and r["id"] != existing_meal_id
        ]
        assert len(nov23_risottos) == 2
        assert {r["servings"] for r in nov23_risottos} == {2, 4}

        # Imported Cookidoo recipe retained source, source_url, source_links, and TM6 guided_steps
        rec_rows = conn.execute(select(recipes)).mappings().all()
        tm6_rec = next(r for r in rec_rows if r["title"] == "TM6 Risotto")
        assert tm6_rec["source"] == RecipeSource.COOKIDOO
        assert tm6_rec["source_url"] == "https://cookidoo.international/recipes/recipe/nb-NO/r-tm6"
        guided = (
            json.loads(tm6_rec["guided_steps"])
            if isinstance(tm6_rec["guided_steps"], str)
            else tm6_rec["guided_steps"]
        )
        assert len(guided) == 1
        assert guided[0]["time_seconds"] == 900
        assert guided[0]["temperature"] == "100°C"
        assert guided[0]["speed"] == "1"
        assert guided[0]["reverse"] is True
        link = conn.execute(select(source_links)).mappings().one()
        assert link["external_recipe_id"] == "r-tm6"
        assert link["recipe_id"] == tm6_rec["id"]


def test_rest_api_cookidoo_plan_sync_export_reads_my_week_and_skips_ai_drafts(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()
    cookidoo_recipe_id = uuid4()
    ai_recipe_id = uuid4()
    meal_cookidoo_1 = uuid4()
    meal_cookidoo_2 = uuid4()
    meal_ai = uuid4()

    with sqlite_engine.begin() as conn:
        conn.execute(
            insert(recipes).values(
                id=cookidoo_recipe_id,
                title="Cookidoo Wok",
                servings=4,
                ingredients=[
                    {"name": "nudler", "quantity": "200", "unit": "g", "category": "Other"}
                ],
                instructions=["Stek i 5 min"],
                source="cookidoo",
                source_url="https://cookidoo.international/recipes/recipe/nb-NO/r-wok",
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(recipes).values(
                id=ai_recipe_id,
                title="KI-tilpasset Wok",
                servings=4,
                ingredients=[{"name": "tofu", "quantity": "200", "unit": "g", "category": "Other"}],
                instructions=["Stek tofu"],
                source="ai",
                source_url="https://cookidoo.international/recipes/recipe/nb-NO/r-wok",
                source_ingredients=[],
                provenance={
                    "kind": "ai_adaptation",
                    "original_source": "cookidoo",
                    "source_url": "https://cookidoo.international/recipes/recipe/nb-NO/r-wok",
                },
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(meals).values(
                id=meal_cookidoo_1,
                date=date(2026, 11, 23),
                label="Middag",
                recipe_id=cookidoo_recipe_id,
                servings=4,
            )
        )
        conn.execute(
            insert(meals).values(
                id=meal_cookidoo_2,
                date=date(2026, 11, 23),
                label="Kvelds",
                recipe_id=cookidoo_recipe_id,
                servings=2,
            )
        )
        conn.execute(
            insert(meals).values(
                id=meal_ai,
                date=date(2026, 11, 24),
                label="Middag",
                recipe_id=ai_recipe_id,
                servings=4,
            )
        )

    requests_log: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        requests_log.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "myDays": [
                        {
                            "dayKey": "2026-11-23",
                            "recipes": [
                                {
                                    "entryId": "mw-existing-1",
                                    "id": "r-other",
                                    "title": "Eksisterende My Week-rett",
                                    "servings": 4,
                                }
                            ],
                        }
                    ]
                },
            )
        if (
            request.method == "DELETE"
            and request.url.path == "/planning/en/api/my-day/2026-11-23/recipes/r-other"
        ):
            return httpx.Response(
                200,
                json={"message": "Removed", "content": None, "code": None},
            )
        if request.method == "PUT" and request.url.path == "/planning/en/api/my-day":
            recipe_id = body["recipeIds"][0]
            return httpx.Response(
                200,
                json={
                    "message": "Planned",
                    "content": {
                        "id": body["dayKey"],
                        "dayKey": body["dayKey"],
                        "recipes": [
                            {
                                "entryId": f"mw-new-{len(requests_log)}",
                                "id": recipe_id,
                                "title": "Cookidoo Wok",
                            }
                        ],
                    },
                },
            )
        return httpx.Response(404)

    app, features, history, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    with TestClient(app) as client:
        preview_resp = client.post(
            "/api/cookidoo/plan-sync/preview",
            json={
                "direction": "kitchen_to_cookidoo",
                "start": "2026-11-23",
                "end": "2026-11-25",
                "edits": [
                    {
                        "source_key": str(meal_cookidoo_2),
                        "target_external_entry_id": "mw-existing-1",
                    }
                ],
            },
        )
        assert preview_resp.status_code == 200, preview_resp.text
        preview = preview_resp.json()
        # Only GET happened during preview; no PUT/DELETE yet
        assert [m for m, _, _ in requests_log] == ["GET"]
        assert len(preview["additions"]) == 1
        assert preview["additions"][0]["cookidoo_recipe_id"] == "r-wok"
        assert preview["additions"][0]["same_date_existing_count"] == 0
        assert len(preview["edits"]) == 1
        assert preview["edits"][0]["external_entry_id"] == "mw-existing-1"
        assert len(preview["skipped"]) == 1
        assert preview["skipped"][0]["title"] == "KI-tilpasset Wok"

        confirm_resp = client.post(
            f"/api/cookidoo/plan-sync/{preview['preview_id']}/confirm",
            json={"reviewed": True},
        )
        assert confirm_resp.status_code == 200, confirm_resp.text
        confirmed = confirm_resp.json()
        assert confirmed["status"] == "applied"
        assert confirmed["applied_count"] == 2
        assert [m for m, _, _ in requests_log] == ["GET", "GET", "PUT", "DELETE", "PUT"]


@pytest.mark.asyncio
async def test_cookidoo_auto_import_is_idempotent_and_tracks_only_unchanged_synced_meals(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
    caplog,
):
    import logging

    caplog.set_level(logging.INFO, logger="home_kitchen.cookidoo_plan_sync")
    hid = uuid4()
    recipe_id = uuid4()
    source_url = "https://cookidoo.international/recipes/recipe/nb-NO/r-known"
    with sqlite_engine.begin() as conn:
        conn.execute(
            insert(recipes).values(
                id=recipe_id,
                title="Known soup",
                servings=4,
                ingredients=[{"name": "potato", "quantity": "400", "unit": "g"}],
                instructions=["Boil"],
                source="cookidoo",
                source_url=source_url,
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(source_links).values(
                provider="cookidoo",
                external_recipe_id="r-known",
                recipe_id=recipe_id,
                source_url=source_url,
                metadata={},
                created_at=datetime.now(UTC),
            )
        )

    source_entry = {"date": "2026-11-29", "servings": 2}
    provider_writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            provider_writes.append(request.method)
            return httpx.Response(200, json={"message": "ok"})
        if request.method == "GET" and request.url.path == "/planning/en/api/my-week/2026-11-23":
            recipes_for_day = []
            if source_entry is not None:
                recipes_for_day = [
                    {
                        "entryId": "mw-auto-1",
                        "id": "r-known",
                        "title": "Known soup",
                        "servings": source_entry["servings"],
                    }
                ]
            recipes_for_day.append(
                {
                    "entryId": "mw-needs-review",
                    "id": "r-unknown",
                    "title": "Unknown recipe",
                    "servings": 4,
                }
            )
            return httpx.Response(
                200,
                json={
                    "myDays": [
                        {
                            "dayKey": source_entry["date"] if source_entry else "2026-11-23",
                            "recipes": recipes_for_day,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    app, _features, _history, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    url = "/api/cookidoo/plan-sync/auto-import?start=2026-11-23&end=2026-11-29"
    with TestClient(app) as client:
        first = client.post(url)
        assert first.status_code == 200, first.text
        assert first.json()["added"] == 1, (first.json(), caplog.text)
        assert first.json()["skipped"] == 1
        with sqlite_engine.connect() as conn:
            imported = conn.execute(select(meals)).mappings().one()
            assert str(imported["date"]) == "2026-11-23"

        # A Sunday Cookidoo entry and a Monday-anchored Kitchen meal are the same
        # week-level plan item; reconciling back to Cookidoo must not add a duplicate.
        outbound = await _features.cookidoo_plan_sync.sync_kitchen_to_cookidoo_best_effort(
            "malo", date(2026, 11, 23), date(2026, 11, 29)
        )
        assert outbound["added"] == 0
        assert provider_writes == []

        repeat = client.post(url)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json()["added"] == 0

        source_entry.update(date="2026-11-25", servings=6)
        update = client.post(url)
        assert update.status_code == 200, update.text
        assert update.json()["updated"] == 1

        source_entry = None
        removed = client.post(url)
        assert removed.status_code == 200, removed.text
        assert removed.json()["removed"] == 1

    with sqlite_engine.connect() as conn:
        assert conn.execute(select(meals)).mappings().all() == []


def test_cookidoo_auto_import_rechecks_history_after_an_overlapping_sync_commits(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
    monkeypatch,
):
    hid = uuid4()
    recipe_id = uuid4()
    source_url = "https://cookidoo.international/recipes/recipe/nb-NO/r-race"
    with sqlite_engine.begin() as conn:
        conn.execute(
            insert(recipes).values(
                id=recipe_id,
                title="Known race recipe",
                servings=4,
                ingredients=[],
                instructions=["Cook"],
                source="cookidoo",
                source_url=source_url,
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(source_links).values(
                provider="cookidoo",
                external_recipe_id="r-race",
                recipe_id=recipe_id,
                source_url=source_url,
                metadata={},
                created_at=datetime.now(UTC),
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "myDays": [
                        {
                            "dayKey": "2026-11-29",
                            "recipes": [
                                {
                                    "entryId": "mw-race-1",
                                    "id": "r-race",
                                    "title": "Known race recipe",
                                    "servings": 2,
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    app, features, _history, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    sync_uow = features.cookidoo_plan_sync._uow
    original_session = sync_uow.session
    race_inserted = False

    @contextmanager
    def competing_sync_then_session():
        nonlocal race_inserted
        if not race_inserted:
            with original_session() as competing:
                meal = Meal(
                    id=uuid4(),
                    date=date(2026, 11, 23),
                    recipe_id=recipe_id,
                    servings=2,
                )
                competing.add_meal(meal)
                competing.record_sync_history(
                    actor_id="malo",
                    direction="cookidoo_to_kitchen",
                    action="addition",
                    meal_id=meal.id,
                    recipe_id=recipe_id,
                    cookidoo_recipe_id="r-race",
                    source_url=source_url,
                    scheduled_date=meal.date,
                    message="Synkroniserte måltid fra Cookidoo Min uke",
                    external_entry_id="mw-race-1",
                    include_external_entry_id=True,
                    sync_state={
                        "date": "2026-11-23",
                        "recipe_id": str(recipe_id),
                        "servings": 2,
                        "label": None,
                    },
                )
            race_inserted = True
        with original_session() as active:
            yield active

    monkeypatch.setattr(sync_uow, "session", competing_sync_then_session)
    with TestClient(app) as client:
        response = client.post(
            "/api/cookidoo/plan-sync/auto-import?start=2026-11-23&end=2026-11-29"
        )
        assert response.status_code == 200, response.text
        assert response.json()["added"] == 0

    with sqlite_engine.connect() as conn:
        imported = conn.execute(select(meals)).mappings().all()
        assert len(imported) == 1


@pytest.mark.asyncio
async def test_mcp_cookidoo_plan_sync_tools_and_actor_isolation(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "days": [
                        {
                            "date": "2026-11-25",
                            "recipes": [
                                {
                                    "entryId": "mw-mcp-1",
                                    "recipeId": "r-mcp-plan",
                                    "title": "MCP Suppe",
                                    "servings": 4,
                                }
                            ],
                        }
                    ]
                },
            )
        if request.url.path == "/recipes/recipe/en/r-mcp-plan":
            return httpx.Response(
                200,
                json={
                    "id": "r-mcp-plan",
                    "title": "MCP Suppe",
                    "ingredients": ["400 g tomat"],
                    "instructions": ["Kok suppe"],
                },
            )
        return httpx.Response(404)

    _, features, _, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    mcp = build_mcp(
        SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine)), lambda: Household()
    )
    install_feature_tools(
        mcp,
        features,
        BasketService(
            SimpleNamespace(store=SimpleNamespace(engine=sqlite_engine)), features.history
        ),
    )

    async with Client(mcp) as _:
        tok = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
        try:
            mw_res = await mcp.call_tool(
                "get_cookidoo_my_week",
                {"start": "2026-11-23", "end": "2026-11-29"},
            )
            assert len(mw_res.structured_content["result"]) == 1

            prev_res = await mcp.call_tool(
                "preview_cookidoo_plan_sync",
                {
                    "direction": "cookidoo_to_kitchen",
                    "start": "2026-11-23",
                    "end": "2026-11-29",
                },
            )
            preview_id = prev_res.structured_content["preview_id"]
            assert len(prev_res.structured_content["additions"]) == 1

            with pytest.raises(ToolError):
                await mcp.call_tool(
                    "confirm_cookidoo_plan_sync",
                    {"preview_id": preview_id, "reviewed": False},
                )

            conf_res = await mcp.call_tool(
                "confirm_cookidoo_plan_sync",
                {"preview_id": preview_id, "reviewed": True},
            )
            assert conf_res.structured_content["status"] == "applied"
            assert conf_res.structured_content["applied_count"] == 1
        finally:
            history_context.reset(tok)

        tok = history_context.set(HistoryContext(actor_id="bob", channel="mcp"))
        try:
            with pytest.raises(ToolError):
                await mcp.call_tool("get_cookidoo_plan_sync_preview", {"preview_id": preview_id})
        finally:
            history_context.reset(tok)


@pytest.mark.asyncio
async def test_my_week_resolves_custom_recipes_and_changed_day_write_responses(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planning/en/api/my-week/2026-11-23" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "myDays": [
                        {
                            "id": "2026-11-23",
                            "dayKey": "2026-11-23",
                            "recipes": [
                                {
                                    "entryId": "mw-cat-1",
                                    "id": "r-cat-1",
                                    "title": "Katalogsuppe",
                                    "ingredients": ["400 g tomat"],
                                    "instructions": [
                                        "Kok opp <nobr>5 min/100°C/hastighet 2</nobr>"
                                    ],
                                }
                            ],
                            "customerRecipeIds": ["cust-id-1"],
                        }
                    ]
                },
            )
        if request.url.path == "/planning/en/api/my-day" and request.method == "PUT":
            body = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "message": "Recipe planned!",
                    "content": {
                        "id": body["dayKey"],
                        "dayKey": body["dayKey"],
                        "recipes": [
                            {
                                "entryId": "mw-day-ret-99",
                                "id": body["recipeIds"][0],
                                "title": "Skrevet rett",
                            }
                        ],
                    },
                    "code": None,
                },
            )
        if request.url.path == "/created-recipes/en":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "recipeId": "cust-id-1",
                            "recipeContent": {
                                "name": "Egen grøt",
                                "recipeIngredient": ["2 dl havregryn", "5 dl melk"],
                                "recipeInstructions": ["Kok <nobr>8 min/90°C/hastighet 1</nobr>"],
                                "recipeYield": 2,
                            },
                        }
                    ]
                },
            )
        return httpx.Response(404)

    app, features, _, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)
    provider = features.cookidoo_plan_sync.provider

    # Unresolved custom entry preserves /created-recipes/ distinction
    unresolved = await provider.get_my_week_plan(
        date(2026, 11, 23), date(2026, 11, 23), resolve_custom_recipes=False
    )
    assert len(unresolved) == 2
    assert unresolved[1].is_custom is True
    assert unresolved[1].cookidoo_recipe_id == "cust-id-1"
    assert "/created-recipes/" in unresolved[1].source_url
    assert unresolved[1].ingredients == []

    # Resolved custom entry hydrates full recipe details while keeping /created-recipes/ link
    resolved = await provider.get_my_week_plan(
        date(2026, 11, 23), date(2026, 11, 23), resolve_custom_recipes=True
    )
    assert resolved[1].title == "Egen grøt"
    assert resolved[1].ingredients == ["2 dl havregryn", "5 dl melk"]
    assert "/created-recipes/" in resolved[1].source_url

    # Calendar write returning changed day extracts real entryId instead of day id
    written = await provider.add_to_my_week("r-cat-1", date(2026, 11, 23), servings=4)
    assert written.external_entry_id == "mw-day-ret-99"

    # Full plan import uses inline resolved custom & catalogue entries without hitting /recipes/recipe/en/*
    with TestClient(app) as client:
        prev = client.post(
            "/api/cookidoo/plan-sync/preview",
            json={
                "direction": "cookidoo_to_kitchen",
                "start": "2026-11-23",
                "end": "2026-11-23",
            },
        )
        assert prev.status_code == 200, prev.text
        preview_data = prev.json()
        assert len(preview_data["additions"]) == 2
        conf = client.post(
            f"/api/cookidoo/plan-sync/{preview_data['preview_id']}/confirm",
            json={"reviewed": True},
        )
        assert conf.status_code == 200, conf.text
        assert conf.json()["applied_count"] == 2

    with sqlite_engine.connect() as conn:
        rec_rows = conn.execute(select(recipes)).mappings().all()
        by_title = {r["title"]: r for r in rec_rows}
        assert "Egen grøt" in by_title
        assert "/created-recipes/" in by_title["Egen grøt"]["source_url"]
        assert by_title["Egen grøt"]["ingredients"][0]["name"] == "havregryn"
        assert len(by_title["Egen grøt"]["guided_steps"]) == 1
        assert "Katalogsuppe" in by_title
        assert len(by_title["Katalogsuppe"]["guided_steps"]) == 1


def test_cookidoo_plan_sync_rejects_duplicate_edit_targets_and_unknown_source_keys(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()
    rec_id = uuid4()
    target_meal_id = uuid4()

    with sqlite_engine.begin() as conn:
        conn.execute(
            insert(recipes).values(
                id=rec_id,
                title="Suppe",
                servings=4,
                ingredients=[{"name": "vann", "quantity": "5", "unit": "dl", "category": "Other"}],
                instructions=["Kok"],
                source="manual",
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(meals).values(
                id=target_meal_id,
                date=date(2026, 11, 23),
                label="Middag",
                recipe_id=rec_id,
                servings=4,
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "days": [
                        {
                            "date": "2026-11-23",
                            "recipes": [
                                {"entryId": "mw-1", "recipeId": "r1", "title": "Rett 1"},
                                {"entryId": "mw-2", "recipeId": "r2", "title": "Rett 2"},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    app, _, _, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    with TestClient(app) as client:
        dup_target = client.post(
            "/api/cookidoo/plan-sync/preview",
            json={
                "direction": "cookidoo_to_kitchen",
                "start": "2026-11-23",
                "end": "2026-11-23",
                "edits": [
                    {"source_key": "mw-1", "target_meal_id": str(target_meal_id)},
                    {"source_key": "mw-2", "target_meal_id": str(target_meal_id)},
                ],
            },
        )
        assert dup_target.status_code == 422

        unknown_source = client.post(
            "/api/cookidoo/plan-sync/preview",
            json={
                "direction": "cookidoo_to_kitchen",
                "start": "2026-11-23",
                "end": "2026-11-23",
                "edits": [
                    {"source_key": "mw-does-not-exist", "target_meal_id": str(target_meal_id)},
                ],
            },
        )
        assert unknown_source.status_code == 422


@pytest.mark.asyncio
async def test_kitchen_to_cookidoo_uses_put_planning_my_day_and_custom_recipe_source(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()
    custom_recipe_id = uuid4()
    custom_meal_id = uuid4()
    catalogue_recipe_id = uuid4()
    catalogue_meal_id = uuid4()

    with sqlite_engine.begin() as conn:
        conn.execute(
            insert(recipes).values(
                id=catalogue_recipe_id,
                title="Vafler",
                servings=4,
                ingredients=[{"name": "mel", "quantity": "200", "unit": "g", "category": "Other"}],
                instructions=["Stek vafler"],
                source="cookidoo",
                source_url="https://cookidoo.international/recipes/recipe/nb-NO/r214846",
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(recipes).values(
                id=custom_recipe_id,
                title="Egen grøt",
                servings=2,
                ingredients=[
                    {"name": "havregryn", "quantity": "2", "unit": "dl", "category": "Other"}
                ],
                instructions=["Kok grøt"],
                source="cookidoo",
                source_url="https://cookidoo.international/created-recipes/en/01K2CTJ9Y1BABRG5MXK44CFZS4",
                source_ingredients=[],
                source_language="nob",
                guided_steps=[],
            )
        )
        conn.execute(
            insert(meals).values(
                id=catalogue_meal_id,
                date=date(2026, 11, 23),
                label="Middag",
                recipe_id=catalogue_recipe_id,
                servings=4,
            )
        )
        conn.execute(
            insert(meals).values(
                id=custom_meal_id,
                date=date(2026, 11, 30),
                label="Kvelds",
                recipe_id=custom_recipe_id,
                servings=2,
            )
        )

    requests_log: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        requests_log.append((request.method, request.url.path, body))
        if request.url.path.startswith("/planner/"):
            return httpx.Response(405)
        if request.method == "GET" and request.url.path == "/planning/en/api/my-week/2026-11-23":
            return httpx.Response(
                200,
                json={
                    "myDays": [
                        {
                            "id": "2026-11-23",
                            "dayKey": "2026-11-23",
                            "recipes": [
                                {
                                    "id": "r999",
                                    "title": "Eksisterende rett",
                                    "totalTime": "1500.0",
                                    "assets": {
                                        "images": {
                                            "square": "https://assets.tmecosys.com/image/upload/{transformation}/img/r999"
                                        }
                                    },
                                }
                            ],
                            "customerRecipeIds": [],
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path == "/planning/en/api/my-week/2026-11-30":
            return httpx.Response(200, json={"myDays": []})
        if request.method == "PUT" and request.url.path == "/planning/en/api/my-day":
            assert body is not None
            if body.get("recipeSource") == "CUSTOMER":
                return httpx.Response(
                    201,
                    json={
                        "message": "Recipe planned!",
                        "content": {
                            "id": body["dayKey"],
                            "dayKey": body["dayKey"],
                            "recipes": [],
                            "customerRecipeIds": body["recipeIds"],
                        },
                        "code": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "message": "Recipe planned!",
                    "content": {
                        "id": body["dayKey"],
                        "dayKey": body["dayKey"],
                        "recipes": [
                            {
                                "id": body["recipeIds"][0],
                                "title": "Vafler",
                                "totalTime": "1500.0",
                                "assets": {
                                    "images": {
                                        "square": "https://assets.tmecosys.com/image/upload/{transformation}/img/r214846"
                                    }
                                },
                            }
                        ],
                        "customerRecipeIds": [],
                    },
                    "code": None,
                },
            )
        return httpx.Response(404)

    app, _, _, store = _offline_app(
        sqlite_engine,
        cipher,
        household_id=hid,
        cookidoo_transport=httpx.MockTransport(handler),
    )
    _save_cookidoo_connection(store, hid)

    with TestClient(app) as client:
        prev_resp = client.post(
            "/api/cookidoo/plan-sync/preview",
            json={
                "direction": "kitchen_to_cookidoo",
                "start": "2026-11-23",
                "end": "2026-12-01",
            },
        )
        assert prev_resp.status_code == 200, prev_resp.text
        preview = prev_resp.json()
        assert len(preview["additions"]) == 2
        assert {item["date"] for item in preview["additions"]} == {
            "2026-11-29",
            "2026-12-06",
        }
        assert len(preview["existing_my_week_entries"]) == 1
        assert preview["existing_my_week_entries"][0]["cookidoo_recipe_id"] == "r999"

        conf_resp = client.post(
            f"/api/cookidoo/plan-sync/{preview['preview_id']}/confirm",
            json={"reviewed": True},
        )
        assert conf_resp.status_code == 200, conf_resp.text
        confirmed = conf_resp.json()
        assert confirmed["status"] == "applied"
        assert confirmed["applied_count"] == 2

    put_calls = [
        (method, path, body)
        for method, path, body in requests_log
        if method == "PUT" and path == "/planning/en/api/my-day"
    ]
    assert put_calls == [
        ("PUT", "/planning/en/api/my-day", {"dayKey": "2026-11-29", "recipeIds": ["r214846"]}),
        (
            "PUT",
            "/planning/en/api/my-day",
            {
                "dayKey": "2026-12-06",
                "recipeIds": ["01K2CTJ9Y1BABRG5MXK44CFZS4"],
                "recipeSource": "CUSTOMER",
            },
        ),
    ]
