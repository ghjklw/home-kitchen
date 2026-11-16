from datetime import date
from uuid import uuid4

import pytest
from test_api import make_client

from home_kitchen.context import HistoryContext, history_context


def test_explicit_purchase_closes_only_selected_capture_and_preserves_stock(test_database_url):
    with make_client(test_database_url) as client:
        milk = client.post("/api/items", json={"name": "Milk", "quantity": "2", "unit": "l"}).json()
        coffee = client.post("/api/items", json={"name": "Coffee"}).json()
        period = {"start": "2026-12-01", "end": "2026-12-07"}
        review_response = client.get("/api/store-trip/completion-review", params=period)
        assert review_response.status_code == 200
        assert review_response.headers["cache-control"] == "no-store"
        review = review_response.json()
        line = next(line for line in review["lines"] if line["name"] == "Milk")
        request = {
            **period,
            "review_token": review["review_token"],
            "reviewed": True,
            "idempotency_key": str(uuid4()),
            "purchased_on": str(date.today()),
            "purchases": [
                {
                    "need_id": line["id"],
                    "quantity": "2",
                    "unit": "l",
                    "close_capture_ids": [milk["id"]],
                }
            ],
        }
        saved = client.post("/api/store-trip/completions", json=request)
        assert saved.status_code == 201
        assert saved.headers["cache-control"] == "no-store"
        replay = client.post("/api/store-trip/completions", json=request)
        assert replay.json() == saved.json()
        items = {item["id"]: item for item in client.get("/api/items").json()}
        assert items[milk["id"]]["completed"] is True
        assert items[coffee["id"]]["completed"] is False
        assert client.get("/api/inventory").json() == []
        assert len(client.get("/api/store-trip/completions").json()) == 1
        purchases = [
            entry for entry in client.get("/api/interactions").json() if entry["kind"] == "purchase"
        ]
        assert len(purchases) == 1


@pytest.mark.asyncio
async def test_mcp_completion_reviews_and_batches_explicit_purchases_idempotently(
    test_database_url,
):
    with make_client(test_database_url) as client:
        milk = client.post("/api/items", json={"name": "Milk", "quantity": "2", "unit": "l"}).json()
        coffee = client.post(
            "/api/items", json={"name": "Coffee", "quantity": "1", "unit": "item"}
        ).json()
        today = date.today().isoformat()
        period = {"start": today, "end": today}
        mcp = client.app.state.mcp
        token = history_context.set(HistoryContext(actor_id="mcp-test-actor", channel="mcp"))
        try:
            review_result = await mcp.call_tool("review_store_trip_completion", period)
            review = review_result.structured_content
            assert review["start"] == today
            assert review["end"] == today
            by_name = {line["name"]: line for line in review["lines"]}
            assert {"Milk", "Coffee"} <= by_name.keys()

            request = {
                **period,
                "review_token": review["review_token"],
                "reviewed": True,
                "idempotency_key": str(uuid4()),
                "purchased_on": today,
                "purchases": [
                    {
                        "need_id": by_name["Milk"]["id"],
                        "quantity": "2",
                        "unit": "l",
                        "close_capture_ids": [milk["id"]],
                    },
                    {
                        "need_id": by_name["Coffee"]["id"],
                        "quantity": "1",
                        "unit": "item",
                        "close_capture_ids": [coffee["id"]],
                    },
                ],
            }
            saved = await mcp.call_tool("complete_store_trip", {"data": request})
            completed = saved.structured_content
            assert completed["idempotency_key"] == request["idempotency_key"]
            assert len(completed["lines"]) == 2
            assert {line["need"]["name"] for line in completed["lines"]} == {"Milk", "Coffee"}

            replay = await mcp.call_tool("complete_store_trip", {"data": request})
            assert replay.structured_content == completed
            trips = await mcp.call_tool("list_completed_store_trips", {})
            assert len(trips.structured_content) == 1
        finally:
            history_context.reset(token)

        items = {item["id"]: item for item in client.get("/api/items").json()}
        assert items[milk["id"]]["completed"] is True
        assert items[coffee["id"]]["completed"] is True
        assert client.get("/api/inventory").json() == []
        purchases = [
            entry for entry in client.get("/api/interactions").json() if entry["kind"] == "purchase"
        ]
        assert len(purchases) == 2
