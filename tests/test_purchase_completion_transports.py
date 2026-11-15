from datetime import date
from uuid import uuid4

from test_api import make_client


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
