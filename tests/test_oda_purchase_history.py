"""Synthetic source captures use the official v2.3.0 read-tool contracts."""

import copy
import json
import runpy
from base64 import urlsafe_b64decode
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import create_engine, func, select, text

from home_kitchen.oda import OdaError, OdaService
from home_kitchen.oda.purchase_history import OdaPurchaseHistoryAdapter, OrderStatus
from home_kitchen.purchase_sync import (
    CheckpointConflict,
    IncompleteScanError,
    checkpoint,
    metadata,
    persist_page,
    provider_order_sources,
    sync_account,
)
from home_kitchen.settings import Settings

pytestmark = pytest.mark.mocked_provider

FIXTURES = Path(__file__).parent / "fixtures" / "oda"


def tool(name):
    fixture = json.loads((FIXTURES / f"{name}_v2_3_0.json").read_text())
    return SimpleNamespace(
        name=name,
        annotations=SimpleNamespace(readOnlyHint=True),
        inputSchema=fixture["input_schema"],
        outputSchema=fixture["output_schema"],
    )


@pytest.fixture
def order():
    return {
        "orderNumber": "00001234-NO",
        "deliveryDate": "2026-01-02",
        "currency": "NOK",
        "grossAmount": 23.5,
        "products": [
            {
                "product": {
                    "id": 123,
                    "name": "Milk",
                    "description": "1 litre",
                    "price": "23.50",
                    "unitPrice": "23.50",
                    "unitName": "l",
                },
                "quantity": 1,
                "totalGrossAmount": "23.50",
            }
        ],
    }


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    # Reject any household write, including writes which would later be rolled back.
    with engine.begin() as connection:
        for table in ("kitchen_history", "kitchen_inventory"):
            connection.execute(text(f"CREATE TABLE {table} (id TEXT)"))
            connection.execute(
                text(
                    f"CREATE TRIGGER reject_{table} BEFORE INSERT ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'source capture attempted household write'); END"
                )
            )
    yield engine
    engine.dispose()


def fake_service(pages, status="delivered"):
    async def list_orders(account_id, page, size):
        assert account_id == "alice" and size == 20
        return copy.deepcopy(pages[page])

    return SimpleNamespace(
        list_orders=AsyncMock(side_effect=list_orders),
        track_order=AsyncMock(
            side_effect=lambda actor, number: {
                "orderNumber": number,
                "status": status,
            }
        ),
    )


@pytest.mark.parametrize("status", (*get_args(OrderStatus), "provider_future_status"))
def test_exact_status_snapshot_never_creates_purchase_evidence(engine, order, status):
    service = fake_service({1: {"hasMore": False, "orders": [order]}}, status)
    adapter = OdaPurchaseHistoryAdapter(service, "alice")
    assert sync_account(engine, "oda", "alice", adapter) == 1
    with engine.connect() as connection:
        row = connection.execute(select(provider_order_sources)).mappings().one()
        assert row["external_id"] == "00001234-NO"
        assert row["payload"] == {
            "order": order,
            "tracking": {
                "orderNumber": order["orderNumber"],
                "status": status,
            },
        }
        assert row["source_updated_at"] is None
        assert row["observed_at"] is not None
        for table in ("kitchen_history", "kitchen_inventory"):
            assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


def test_full_scans_restart_at_one_hash_replay_and_corrections(engine, order):
    pages = {1: {"hasMore": True, "orders": [order]}, 2: {"hasMore": False, "orders": []}}
    service = fake_service(pages)
    adapter = OdaPurchaseHistoryAdapter(service, "alice")
    for _ in range(2):
        assert sync_account(engine, "oda", "alice", adapter) == 1
    assert [call.kwargs["page"] for call in service.list_orders.await_args_list] == [1, 2, 1, 2]
    with engine.connect() as connection:
        first = connection.execute(select(provider_order_sources)).mappings().one()
        assert first["revision_id"].startswith("oda-snapshot-v1:")
    service.track_order.side_effect = lambda actor, number: {
        "orderNumber": number,
        "status": "cancelled",
    }
    assert sync_account(engine, "oda", "alice", adapter) == 1
    pages[1]["orders"][0]["products"][0]["quantity"] = 2
    assert sync_account(engine, "oda", "alice", adapter) == 1
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(provider_order_sources)).scalar()
            == 3
        )


def test_empty_history_unique_terminal_cursor_protects_cas(engine):
    adapter = OdaPurchaseHistoryAdapter(
        fake_service({1: {"hasMore": False, "orders": []}}), "alice"
    )
    stale = adapter.fetch_page("alice", None)
    assert sync_account(engine, "oda", "alice", adapter) == 0
    previous = checkpoint(engine, "oda", "alice")
    assert sync_account(engine, "oda", "alice", adapter) == 0
    assert checkpoint(engine, "oda", "alice") != previous
    with pytest.raises(CheckpointConflict):
        persist_page(engine, "oda", "alice", stale, expected_cursor=previous)


def test_cursor_declares_full_rescan_mode_and_new_generation_after_completion():
    adapter = OdaPurchaseHistoryAdapter(
        fake_service({1: {"hasMore": False, "orders": []}}), "alice"
    )
    first = adapter.fetch_page("alice", None)
    second = adapter.fetch_page("alice", first.next_cursor)
    first_state = json.loads(urlsafe_b64decode(first.next_cursor.split(".", 1)[1]))
    second_state = json.loads(urlsafe_b64decode(second.next_cursor.split(".", 1)[1]))
    assert first_state | {"scan_id": second_state["scan_id"]} == second_state
    assert first_state["scan_id"] != second_state["scan_id"]
    assert first_state["version"] == 1
    assert first_state["mode"] == "full_rescan"
    assert first_state["next_page"] == 1
    assert first_state["state"] == "complete"


@pytest.mark.parametrize(
    "cursor", ["", "junk", "oda-scan-v2.e30=", "oda-scan-v1.e30=", "oda-scan-v1.!"]
)
def test_bad_cursor_fails_before_read(cursor):
    service = fake_service({})
    with pytest.raises(ValueError, match="cursor"):
        OdaPurchaseHistoryAdapter(service, "alice").fetch_page("alice", cursor)
    service.list_orders.assert_not_awaited()


def test_account_scoping_rejects_actor_and_cursor_mismatch():
    service = fake_service({1: {"hasMore": True, "orders": []}})
    adapter = OdaPurchaseHistoryAdapter(service, "alice")
    with pytest.raises(ValueError, match="account mismatch"):
        adapter.fetch_page("bob", None)
    cursor = adapter.fetch_page("alice", None).next_cursor
    with pytest.raises(ValueError, match="cursor account mismatch"):
        OdaPurchaseHistoryAdapter(service, "bob").fetch_page("bob", cursor)


def test_page_50_truncation_captures_final_records_and_next_poll_restarts(engine, order):
    pages = {page: {"hasMore": True, "orders": []} for page in range(1, 51)}
    pages[50]["orders"] = [order]
    service = fake_service(pages)
    adapter = OdaPurchaseHistoryAdapter(service, "alice")
    with pytest.raises(IncompleteScanError, match="truncated.*incomplete"):
        sync_account(engine, "oda", "alice", adapter)
    saved = checkpoint(engine, "oda", "alice")
    assert saved is not None
    with engine.connect() as connection:
        captured = connection.execute(select(provider_order_sources)).mappings().one()
        assert captured["external_id"] == order["orderNumber"]
        assert captured["payload"]["order"] == order
    # A second capped scan starts at one, saves a unique terminal cursor, and
    # reports incomplete again without duplicating the page-50 capture.
    with pytest.raises(IncompleteScanError, match="next poll restarts at page 1"):
        sync_account(engine, "oda", "alice", adapter)
    assert checkpoint(engine, "oda", "alice") != saved
    assert [call.kwargs["page"] for call in service.list_orders.await_args_list] == (
        list(range(1, 51)) * 2
    )
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(provider_order_sources)).scalar()
            == 1
        )
    # If history later fits the cap, the next scan can complete normally.
    pages[1] = {"hasMore": False, "orders": [order]}
    assert sync_account(engine, "oda", "alice", adapter) == 1
    assert service.list_orders.await_args_list[-1].kwargs["page"] == 1


def test_truncated_page_is_explicitly_incomplete_before_persistence(order):
    service = fake_service({page: {"hasMore": True, "orders": [order]} for page in range(1, 51)})
    adapter = OdaPurchaseHistoryAdapter(service, "alice")
    cursor = None
    for _ in range(50):
        page = adapter.fetch_page("alice", cursor)
        cursor = page.next_cursor
    assert page.has_more is False
    assert page.incomplete_reason is not None
    assert "incomplete" in page.incomplete_reason
    assert len(page.records) == 1
    assert adapter.fetch_page("alice", cursor).has_more is True
    assert service.list_orders.await_args_list[-1].kwargs["page"] == 1


@pytest.mark.parametrize("unknown_time", [False, True])
def test_source_time_downgrade_explicitly_rejects_null_captures(engine, order, unknown_time):
    migration = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "migrations/versions/0021_nullable_provider_order_source_time.py"
        )
    )
    if unknown_time:
        sync_account(
            engine,
            "oda",
            "alice",
            OdaPurchaseHistoryAdapter(
                fake_service({1: {"hasMore": False, "orders": [order]}}), "alice"
            ),
        )
    with (
        engine.connect() as connection,
        patch.object(migration["op"], "get_bind", return_value=connection),
        patch.object(migration["op"], "alter_column") as alter,
    ):
        if unknown_time:
            with pytest.raises(RuntimeError, match="unknown source update timestamps"):
                migration["downgrade"]()
            alter.assert_not_called()
        else:
            migration["downgrade"]()
            alter.assert_called_once()
            assert alter.call_args.kwargs["nullable"] is False


def test_untracked_summary_does_not_infer_status(order):
    service = fake_service({1: {"hasMore": False, "orders": [order]}})
    record = (
        OdaPurchaseHistoryAdapter(service, "alice", fetch_tracking=False)
        .fetch_page("alice", None)
        .records[0]
    )
    assert record.payload == {"order": order}
    service.track_order.assert_not_awaited()


@pytest.fixture
def service(engine):
    service = OdaService(engine, Settings(local_dev=True))
    service.access_token = Mock(return_value="fixture-token")
    yield service
    service.close()


def mock_client(name, payload):
    client = SimpleNamespace(
        list_tools=AsyncMock(return_value=[tool(name)]),
        call_tool=AsyncMock(return_value=SimpleNamespace(structured_content=payload)),
    )
    context = AsyncMock()
    context.__aenter__.return_value = client
    return context, client


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_orders", "order_tracking"])
async def test_service_calls_only_validated_read_tools(service, order, name):
    payload = (
        {"hasMore": False, "orders": [order]}
        if name == "get_orders"
        else {
            "orderNumber": order["orderNumber"],
            "status": "delivered",
        }
    )
    context, client = mock_client(name, payload)
    with patch("home_kitchen.oda.service.Client", return_value=context):
        if name == "get_orders":
            result = await service.list_orders("alice", 2, 20)
            args = {"page": 2, "size": 20}
        else:
            result = await service.track_order("alice", order["orderNumber"])
            args = {"order_number": order["orderNumber"]}
    assert result == payload
    service.access_token.assert_called_once_with("alice")
    client.call_tool.assert_awaited_once_with(name, args, raise_on_error=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_orders", "order_tracking"])
@pytest.mark.parametrize("change", ["readonly", "input", "output"])
async def test_changed_history_tool_contract_fails_closed(service, name, change):
    context, client = mock_client(name, {})
    advertised = client.list_tools.return_value[0]
    if change == "readonly":
        advertised.annotations.readOnlyHint = False
    elif change == "input":
        advertised.inputSchema["required"] = ["intent"]
    else:
        advertised.outputSchema["properties"] = {}
    with patch("home_kitchen.oda.service.Client", return_value=context):
        with pytest.raises(OdaError, match="read-only|schema changed"):
            await service._read("alice", name, {})
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("page,size", [(0, 20), (51, 20), (1, 21), (True, 20)])
async def test_invalid_pagination_fails_before_read(service, page, size):
    service._read = AsyncMock()
    with pytest.raises(OdaError):
        await service.list_orders("alice", page, size)
    service._read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"hasMore": "false", "orders": []},
        {"hasMore": False, "orders": [{"orderNumber": "x"}]},
        {"hasMore": False},
    ],
)
async def test_invalid_order_response_fails_closed(service, payload):
    service._read = AsyncMock(return_value=payload)
    with pytest.raises(OdaError, match="response changed"):
        await service.list_orders("alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,number", [(123, "x"), ("delivered", "wrong")])
async def test_tracking_invalid_status_type_and_mismatched_identity_fail_closed(
    service, status, number
):
    service._read = AsyncMock(return_value={"orderNumber": number, "status": status})
    with pytest.raises(OdaError, match="response changed"):
        await service.track_order("alice", "x")


@pytest.mark.asyncio
async def test_tracking_preserves_unknown_status_from_expanded_provider_schema(service):
    payload = {"orderNumber": "000123", "status": "provider_future_status"}
    context, client = mock_client("order_tracking", payload)
    client.list_tools.return_value[0].outputSchema["properties"]["status"]["enum"].append(
        "provider_future_status"
    )
    with patch("home_kitchen.oda.service.Client", return_value=context):
        assert await service.track_order("alice", "000123") == payload
