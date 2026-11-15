"""Cart reviews use provider reads; only a one-use explicit confirmation writes."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import StaticPool

from home_kitchen.oda import OdaError
from home_kitchen.oda.cart import (
    HOUSEHOLD_CART_LOCK,
    CartProposalRequest,
    OdaCartService,
    metadata,
    reviews,
)
from home_kitchen.oda.repository import OdaRepository
from home_kitchen.oda.repository import metadata as oauth_metadata


@asynccontextmanager
async def protocol_test_gate():
    # Protocol tests exercise one confirmation at a time. PostgreSQL test below
    # exercises the real cross-process database lock without this seam.
    yield


@pytest.fixture
def cart():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    oauth_metadata.create_all(engine)
    metadata.create_all(engine)
    repository = OdaRepository(engine, Fernet(Fernet.generate_key()))
    repository.begin_connect("alice")
    product = {
        "id": 123,
        "name": "Milk",
        "description": "Whole milk, 1 L",
        "price": "29.90",
        "unitPrice": "29.90",
        "unitName": "per L",
        "url": "https://oda.com/no/products/123/",
        "availability": {"isAvailable": True, "description": "In stock", "code": "available"},
    }
    baseline = {"groups": [], "url": "https://oda.com/no/cart/", "totalGrossAmount": "0.00"}
    oda = SimpleNamespace(
        _repository=lambda: repository,
        access_token=lambda actor: "private-token",
        search_products=AsyncMock(
            return_value={"result": [{"query": "milk", "products": [product], "hasMore": False}]}
        ),
        get_cart=AsyncMock(return_value=baseline),
    )
    yield (
        OdaCartService(
            oda, history_repository=SimpleNamespace(record=Mock()), gate_factory=protocol_test_gate
        ),
        oda,
        engine,
        baseline,
    )
    engine.dispose()


def request(product_id=123):
    return CartProposalRequest.model_validate(
        {"queries": ["Milk"], "additions": [{"product_id": product_id, "quantity": 2}]}
    )


def tools():
    return [
        SimpleNamespace(
            name="get_cart",
            annotations=SimpleNamespace(readOnlyHint=True),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"intent": {"type": "string"}},
            },
        ),
        SimpleNamespace(
            name="manipulate_cart",
            annotations=SimpleNamespace(readOnlyHint=False, destructiveHint=True),
            inputSchema={
                "type": "object",
                "additionalProperties": False,
                "required": ["operations"],
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["quantity"],
                            "properties": {
                                "quantity": {"type": "integer"},
                                "productId": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                                "recipeId": {},
                                "productListId": {},
                                "orderNumber": {},
                                "fromRecipePortions": {},
                            },
                        },
                    }
                },
            },
        ),
    ]


def client_mock(baseline, *, ambiguous=False, inspect_attempt=None):
    async def call(name, arguments, **kwargs):
        if name == "get_cart":
            return SimpleNamespace(structured_content=baseline)
        assert name == "manipulate_cart"
        assert arguments == {"operations": [{"productId": 123, "quantity": 2}]}
        if inspect_attempt:
            inspect_attempt()
        if ambiguous:
            raise RuntimeError("private-provider-error")
        return SimpleNamespace(
            structured_content={
                **baseline,
                "groups": [
                    {
                        "items": [
                            {
                                "product": {
                                    "id": 123,
                                    "name": "Milk",
                                    "description": "Whole milk, 1 L",
                                    "price": "29.90",
                                    "unitPrice": "29.90",
                                    "unitName": "per L",
                                },
                                "quantity": 2,
                                "totalGrossAmount": "59.80",
                            }
                        ]
                    }
                ],
            }
        )

    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=tools()), call_tool=AsyncMock(side_effect=call)
    )
    context = AsyncMock()
    context.__aenter__.return_value = client
    return context, client


@pytest.mark.asyncio
async def test_proposal_reads_only_and_uses_fresh_exact_product_references(cart):
    service, oda, engine, _ = cart
    with patch("home_kitchen.oda.cart.Client") as transport:
        proposal = await service.propose("alice", request())
        transport.assert_not_called()
    oda.search_products.assert_awaited_once_with("alice", ["Milk"], size=20)
    assert proposal["status"] == "pending"
    assert proposal["additions"][0]["product"]["description"] == "Whole milk, 1 L"
    assert proposal["additions"][0]["after_quantity"] == "2"
    assert "unverified" in proposal["warnings"][0]
    with engine.connect() as connection:
        assert "Whole milk" not in str(connection.execute(select(reviews)).all())
    with pytest.raises(OdaError, match="fresh Oda search"):
        await service.propose("alice", request(999))


@pytest.mark.asyncio
async def test_actor_expiry_and_explicit_review_are_required(cart):
    service, _, engine, _ = cart
    proposal = await service.propose("alice", request())
    review_id = proposal["proposal_id"]
    with pytest.raises(OdaError, match="not found"):
        await service.confirm("bob", review_id, reviewed=True)
    with pytest.raises(OdaError, match="Review the exact"):
        await service.confirm("alice", review_id, reviewed=False)
    with engine.begin() as connection:
        connection.execute(
            reviews.update().values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(OdaError, match="expired"):
        await service.confirm("alice", review_id, reviewed=True)


@pytest.mark.asyncio
async def test_confirm_writes_exact_delta_once_after_durable_attempt(cart):
    service, _, _, baseline = cart
    proposal = await service.propose("alice", request())
    review_id = proposal["proposal_id"]

    def attempted():
        assert service.get("alice", review_id)["status"] == "attempted"

    context, client = client_mock(baseline, inspect_attempt=attempted)
    with patch("home_kitchen.oda.cart.Client", return_value=context):
        assert (await service.confirm("alice", review_id, reviewed=True))["status"] == "success"
        assert (await service.confirm("alice", review_id, reviewed=True))["status"] == "success"
    assert [call.args[0] for call in client.call_tool.await_args_list] == [
        "get_cart",
        "manipulate_cart",
    ]
    events = service.history_repository.record.call_args_list
    assert [call.args[0].structured["status"] for call in events] == [
        "proposed",
        "attempted",
        "success",
    ]
    for event in events:
        assert event.args[0].kind == "domain_event"
        assert event.kwargs["actor_id"] == "alice" and event.kwargs["connection"] is not None
        assert event.args[0].structured["additions"] == [{"product_id": 123, "package_count": 2}]
        assert review_id not in str(event.args[0].structured)
        assert "Milk" not in str(event.args[0].structured)


@pytest.mark.asyncio
async def test_stale_cart_never_writes(cart):
    service, _, _, baseline = cart
    proposal = await service.propose("alice", request())
    context, client = client_mock({**baseline, "totalGrossAmount": "10.00"})
    with patch("home_kitchen.oda.cart.Client", return_value=context):
        assert (await service.confirm("alice", proposal["proposal_id"], reviewed=True))[
            "status"
        ] == "stale"
    assert [call.args[0] for call in client.call_tool.await_args_list] == ["get_cart"]


@pytest.mark.asyncio
async def test_ambiguous_write_persists_uncertain_and_replay_never_retries(cart):
    service, _, _, baseline = cart
    proposal = await service.propose("alice", request())
    context, client = client_mock(baseline, ambiguous=True)
    with patch("home_kitchen.oda.cart.Client", return_value=context):
        result = await service.confirm("alice", proposal["proposal_id"], reviewed=True)
        assert result["status"] == "uncertain"
        assert "private-provider-error" not in str(result)
        assert (await service.confirm("alice", proposal["proposal_id"], reviewed=True))[
            "status"
        ] == "uncertain"
    assert client.call_tool.await_count == 2
    assert (await service.propose("alice", request()))["status"] == "pending"


@pytest.mark.asyncio
async def test_unresolved_attempt_blocks_another_review_confirmation(cart):
    service, _, engine, _ = cart
    first = await service.propose("alice", request())
    with engine.begin() as connection:
        connection.execute(reviews.update().values(status="attempted"))
    second = await service.propose("alice", request())
    with pytest.raises(OdaError, match="unresolved"):
        await service.confirm("alice", second["proposal_id"], reviewed=True)
    assert service.get("alice", first["proposal_id"])["status"] == "attempted"


@pytest.mark.asyncio
async def test_changed_write_annotation_blocks_all_tool_calls(cart):
    service, _, _, baseline = cart
    proposal = await service.propose("alice", request())
    context, client = client_mock(baseline)
    declared = tools()
    declared[1].annotations.readOnlyHint = True
    client.list_tools.return_value = declared
    with patch("home_kitchen.oda.cart.Client", return_value=context):
        assert (await service.confirm("alice", proposal["proposal_id"], reviewed=True))[
            "status"
        ] == "error"
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_provider_failure_is_uncertain_not_success(cart):
    service, _, _, baseline = cart
    proposal = await service.propose("alice", request())
    context, client = client_mock(baseline)
    original = client.call_tool.side_effect

    async def response(name, arguments, **kwargs):
        if name == "manipulate_cart":
            return SimpleNamespace(structured_content={"success": False, "error": "private-detail"})
        return await original(name, arguments, **kwargs)

    client.call_tool.side_effect = response
    with patch("home_kitchen.oda.cart.Client", return_value=context):
        result = await service.confirm("alice", proposal["proposal_id"], reviewed=True)
        assert result["status"] == "uncertain"
        assert "private-detail" not in str(result)


@pytest.mark.skipif(
    not os.environ.get("KITCHEN_TEST_DATABASE_URL"), reason="Requires disposable PostgreSQL"
)
@pytest.mark.asyncio
async def test_postgresql_household_gate_serializes_alice_and_bob(cart, test_database_url):
    """Two replica-like service instances cannot both write from one empty-cart review."""
    _, source, _, baseline = cart
    engine = create_engine(test_database_url)
    repository = OdaRepository(engine, Fernet(Fernet.generate_key()))
    repository.begin_connect("alice")
    repository.begin_connect("bob")
    product = source.search_products.return_value["result"][0]["products"][0]
    quantity = 0
    alice_read = asyncio.Event()
    allow_alice = asyncio.Event()
    bob_read = asyncio.Event()
    writes = []

    def current_cart():
        if quantity == 0:
            return dict(baseline)
        return {
            **baseline,
            "totalGrossAmount": "59.80",
            "groups": [
                {
                    "items": [
                        {
                            "product": product,
                            "quantity": quantity,
                            "totalGrossAmount": "59.80",
                        }
                    ]
                }
            ],
        }

    oda = SimpleNamespace(
        _repository=lambda: repository,
        access_token=lambda actor: actor,
        search_products=source.search_products,
        get_cart=AsyncMock(side_effect=lambda actor: current_cart()),
    )
    alice = OdaCartService(oda)
    bob = OdaCartService(oda)
    first = await alice.propose("alice", request())
    second = await bob.propose("bob", request())

    def remote(resource, *, auth, timeout):
        async def call(name, arguments, **kwargs):
            nonlocal quantity
            if name == "get_cart":
                if auth == "alice":
                    alice_read.set()
                    await allow_alice.wait()
                else:
                    bob_read.set()
                return SimpleNamespace(structured_content=current_cart())
            assert name == "manipulate_cart"
            assert arguments == {"operations": [{"productId": 123, "quantity": 2}]}
            writes.append(auth)
            quantity += 2
            return SimpleNamespace(structured_content=current_cart())

        context = AsyncMock()
        context.__aenter__.return_value = SimpleNamespace(
            list_tools=AsyncMock(return_value=tools()),
            call_tool=AsyncMock(side_effect=call),
        )
        return context

    def waiter_visible():
        key = HOUSEHOLD_CART_LOCK & ((1 << 64) - 1)
        with engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                        "AND CAST(classid AS bigint) = :high AND CAST(objid AS bigint) = :low AND objsubid = 1"
                    ),
                    {"high": key >> 32, "low": key & ((1 << 32) - 1)},
                ).scalar_one()
                > 0
            )

    tasks = []
    try:
        with patch("home_kitchen.oda.cart.Client", side_effect=remote):
            tasks.append(
                asyncio.create_task(alice.confirm("alice", first["proposal_id"], reviewed=True))
            )
            await asyncio.wait_for(alice_read.wait(), 5)
            tasks.append(
                asyncio.create_task(bob.confirm("bob", second["proposal_id"], reviewed=True))
            )
            async with asyncio.timeout(5):
                while not await asyncio.to_thread(waiter_visible):
                    await asyncio.sleep(0.01)
            # PostgreSQL itself reports Bob waiting on Alice's session lock, and
            # the async event loop remains responsive while acquisition waits.
            assert not bob_read.is_set()
            assert writes == []
            allow_alice.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert [result["status"] for result in results] == ["success", "stale"]
            assert quantity == 2 and writes == ["alice"]
    finally:
        allow_alice.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.dispose()
