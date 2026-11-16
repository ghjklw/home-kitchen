"""Saved and purchased Oda recipe reads use only the documented read tools."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from home_kitchen.oda import OdaError, OdaService
from home_kitchen.oda.repository import metadata
from home_kitchen.settings import Settings


def recipe_tool(name, *, read_only=True, bad_schema=False, with_intent=False):
    properties = {
        "page": {"type": "integer", "minimum": 1, "maximum": 50},
        "size": {"type": "integer", "minimum": 1, "maximum": 20},
    }
    if with_intent:
        properties["intent"] = {"type": "string", "maxLength": 300}
    item_properties = {
        "id": {"type": "integer"},
        "title": {"type": "string"},
        "url": {"type": ["string", "null"]},
        "providerName": {"type": ["string", "null"]},
        "imageUrl": {"type": ["string", "null"]},
        "deliveriesCount": {"type": "integer"},
    }
    if bad_schema:
        properties["page"]["maximum"] = 100
    return SimpleNamespace(
        name=name,
        annotations=SimpleNamespace(readOnlyHint=read_only),
        inputSchema={
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": [],
        },
        outputSchema={
            "type": "object",
            "required": ["hasMore", "recipes"],
            "properties": {
                "hasMore": {"type": "boolean"},
                "recipes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "title"],
                        "properties": item_properties,
                    },
                },
            },
        },
    )


@pytest.fixture
def oda():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    metadata.create_all(engine)
    service = OdaService(
        engine,
        Settings(
            local_dev=True,
            public_url="https://kitchen.example",
            oda_encryption_key=SecretStr(Fernet.generate_key().decode()),
        ),
    )
    service.access_token = lambda actor_id: "test-token"
    yield service, engine
    service.close()
    engine.dispose()


def client_mock(tool_name, payload, *, read_only=True, bad_schema=False, with_intent=False):
    call_tool = AsyncMock(return_value=SimpleNamespace(structured_content=payload))
    client = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=[
                recipe_tool(
                    tool_name,
                    read_only=read_only,
                    bad_schema=bad_schema,
                    with_intent=with_intent,
                )
            ]
        ),
        call_tool=call_tool,
    )
    context = AsyncMock()
    context.__aenter__.return_value = client
    return context, client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "tool_name"),
    [
        ("list_liked_recipes", "get_liked_recipes"),
        ("list_purchased_recipes", "get_purchased_recipes"),
    ],
)
async def test_recipe_collections_return_validated_provider_data(oda, method, tool_name):
    service, _ = oda
    payload = {
        "hasMore": True,
        "recipes": [
            {
                "id": 731,
                "title": "Tomato soup",
                "url": None,
                "providerName": "Oda",
                "imageUrl": None,
                "deliveriesCount": 2,
            }
        ],
    }
    context, client = client_mock(tool_name, payload)
    with patch("home_kitchen.oda.service.Client", return_value=context):
        result = await getattr(service, method)("alice", page=3, size=7)

    assert result == payload
    client.call_tool.assert_awaited_once_with(
        tool_name, {"page": 3, "size": 7}, raise_on_error=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["list_liked_recipes", "list_purchased_recipes"])
async def test_recipe_collection_rejects_out_of_range_pagination_before_network(oda, method):
    service, _ = oda
    with patch("home_kitchen.oda.service.Client") as client:
        with pytest.raises(OdaError) as error:
            await getattr(service, method)("alice", page=51)
    assert error.value.status_code == 400
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("read_only", "bad_schema"), [(False, False), (True, True)])
async def test_recipe_collection_fails_closed_on_changed_tool_metadata(oda, read_only, bad_schema):
    service, _ = oda
    context, client = client_mock(
        "get_liked_recipes",
        {"hasMore": False, "recipes": []},
        read_only=read_only,
        bad_schema=bad_schema,
    )
    with patch("home_kitchen.oda.service.Client", return_value=context):
        with pytest.raises(OdaError, match="read-only|schema changed"):
            await service.list_liked_recipes("alice")
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipe_collection_rejects_malformed_response(oda):
    service, _ = oda
    context, client = client_mock(
        "get_purchased_recipes",
        {"hasMore": False, "recipes": [{"id": "731", "title": "Tomato soup"}]},
    )
    with patch("home_kitchen.oda.service.Client", return_value=context):
        with pytest.raises(OdaError, match="collection response changed"):
            await service.list_purchased_recipes("alice")
    client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_recipe_collection_uses_exact_actor_and_rejects_unknown_read_tool(oda):
    service, _ = oda
    access_token = Mock(return_value="test-token")
    service.access_token = access_token
    context, _ = client_mock("get_liked_recipes", {"hasMore": False, "recipes": []})
    with patch("home_kitchen.oda.service.Client", return_value=context):
        await service.list_liked_recipes("alice")
    access_token.assert_called_once_with("alice")
    with pytest.raises(OdaError, match="not supported"):
        await service._read("alice", "get_recipe_ingredients", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "tool_name"),
    [
        ("list_liked_recipes", "get_liked_recipes"),
        ("list_purchased_recipes", "get_purchased_recipes"),
    ],
)
async def test_recipe_collection_accepts_intent_property(oda, method, tool_name):
    service, _ = oda
    payload = {"hasMore": False, "recipes": []}
    context, client = client_mock(tool_name, payload, with_intent=True)
    with patch("home_kitchen.oda.service.Client", return_value=context):
        result = await getattr(service, method)("alice")
    assert result == payload
    client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_recipe_collection_refreshes_token_on_unauthorized(oda):
    import httpx

    service, _ = oda
    tokens = ["expired-token", "refreshed-token"]

    def fake_access_token(actor, force_refresh=False):
        return tokens.pop() if force_refresh else tokens[0]

    service.access_token = fake_access_token

    req = httpx.Request("POST", "https://oda.com/mcp")
    resp_401 = httpx.Response(401, request=req)
    error_401 = httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp_401)

    payload = {"hasMore": False, "recipes": []}
    call_mock = AsyncMock(side_effect=[error_401, SimpleNamespace(structured_content=payload)])

    tool = recipe_tool("get_liked_recipes", with_intent=True)
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=[tool]),
        call_tool=call_mock,
    )
    context = AsyncMock()
    context.__aenter__.return_value = client

    with patch("home_kitchen.oda.service.Client", return_value=context):
        result = await service.list_liked_recipes("alice")

    assert result == payload
    assert call_mock.await_count == 2
