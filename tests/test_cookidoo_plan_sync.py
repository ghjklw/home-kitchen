"""Offline tests for Cookidoo My Week client and plan sync preview/confirm (API + MCP)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy import insert, select

from home_kitchen.basket.repository import BasketService
from home_kitchen.context import HistoryContext, history_context
from home_kitchen.cookidoo_recipes import CookidooCollectionProvider, CookidooError
from home_kitchen.feature_mcp import install_feature_tools
from home_kitchen.mcp_server import build_mcp
from home_kitchen.models import RecipeSource
from home_kitchen.recipes.collections import source_links
from home_kitchen.settings import Household
from home_kitchen.store import meals, recipes
from tests.test_cookidoo_recipes import (
    _offline_app,
    _save_cookidoo_connection,
    cipher,  # noqa: F401
    connector_store,  # noqa: F401
    sqlite_engine,  # noqa: F401
)


@pytest.mark.asyncio
async def test_cookidoo_client_my_week_read_and_write_reads_before_add(connector_store):  # noqa: F811
    hid = uuid4()
    _save_cookidoo_connection(connector_store, hid, access_token="tok-mw", country="no")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, request.url.path, body))
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
                                            "entryId": "mw-1",
                                            "recipeId": "r100",
                                            "title": "Fiskesuppe",
                                            "servings": 4,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/planner/en/api/my-week":
            return httpx.Response(
                201,
                json={
                    "entryId": "mw-2",
                    "recipeId": body["recipeId"],
                    "date": body["date"],
                    "title": "Tomatsuppe",
                    "servings": body.get("servings", 4),
                },
            )
        if request.method == "PUT" and request.url.path == "/planner/en/api/my-week/mw-1":
            return httpx.Response(
                200,
                json={
                    "entryId": "mw-1",
                    "recipeId": body["recipeId"],
                    "date": body["date"],
                    "title": "Oppdatert fiskesuppe",
                    "servings": body.get("servings", 4),
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
    assert entries[0].date == date(2026, 11, 23)

    calls.clear()
    added = await provider.add_to_my_week("r200", date(2026, 11, 23), servings=2)
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert added.external_entry_id == "mw-2"
    assert added.cookidoo_recipe_id == "r200"
    assert added.source_url == "https://cookidoo.international/recipes/recipe/nb-NO/r200"

    # Explicit edit with known entry_id issues PUT
    updated = await provider.add_to_my_week("r100", date(2026, 11, 23), servings=6, entry_id="mw-1")
    assert updated.external_entry_id == "mw-1"
    assert updated.servings == 6
    assert calls[-1][0] == "PUT"

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
        if request.url.path == "/planner/en/api/my-week":
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
        if request.method == "GET" and request.url.path == "/planner/en/api/my-week":
            return httpx.Response(
                200,
                json={
                    "days": [
                        {
                            "date": "2026-11-23",
                            "recipes": [
                                {
                                    "entryId": "mw-existing-1",
                                    "recipeId": "r-other",
                                    "title": "Eksisterende My Week-rett",
                                    "servings": 4,
                                }
                            ],
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/planner/en/api/my-week":
            return httpx.Response(
                201,
                json={
                    "entryId": f"mw-new-{len(requests_log)}",
                    "recipeId": body["recipeId"],
                    "date": body["date"],
                    "servings": body["servings"],
                },
            )
        if request.method == "PUT" and request.url.path == "/planner/en/api/my-week/mw-existing-1":
            return httpx.Response(
                200,
                json={
                    "entryId": "mw-existing-1",
                    "recipeId": body["recipeId"],
                    "date": body["date"],
                    "servings": body["servings"],
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
        # Only GET happened during preview; no POST/PUT yet
        assert [m for m, _, _ in requests_log] == ["GET"]
        assert len(preview["additions"]) == 1
        assert preview["additions"][0]["cookidoo_recipe_id"] == "r-wok"
        assert preview["additions"][0]["same_date_existing_count"] == 1
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
        assert [m for m, _, _ in requests_log] == ["GET", "GET", "POST", "PUT"]


@pytest.mark.asyncio
async def test_mcp_cookidoo_plan_sync_tools_and_actor_isolation(
    sqlite_engine,  # noqa: F811
    cipher,  # noqa: F811
):
    hid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/planner/en/api/my-week":
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
