from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from home_kitchen.app import create_app
from home_kitchen.settings import Settings


def make_client(path):
    settings = Settings(local_dev=True, database_url=path)
    return TestClient(create_app(settings), client=("127.0.0.1", 5000))


def recipe_payload():
    return {
        "title": "Tomato pasta",
        "servings": 2,
        "ingredients": [{"name": "Pasta", "quantity": 200, "unit": "g"}],
        "instructions": ["Boil the pasta"],
    }


def test_empty_household_recipe_meal_shopping_and_restart(test_database_url):
    database = test_database_url
    with make_client(database) as client:
        assert client.get("/api/recipes").json() == []
        assert client.get("/api/household").json() == {
            "name": "",
            "adults": 1,
            "children_ages": [],
            "dietary_preferences": [],
            "default_servings": 2,
            "language": "en",
        }
        assert client.get("/api/items").json() == []
        household = client.put(
            "/api/household",
            json={"name": "North house", "adults": 2, "children_ages": [8]},
        )
        assert household.status_code == 200
        assert household.json()["name"] == "North house"

        created = client.post("/api/recipes", json=recipe_payload())
        assert created.status_code == 201
        recipe_id = created.json()["id"]
        meal_date = date.today() + timedelta(days=2)
        meal = client.post(
            "/api/meals",
            json={"date": meal_date.isoformat(), "recipe_id": recipe_id, "servings": 4},
        )
        assert meal.status_code == 201
        assert meal.json()["recipe_id"] == recipe_id

        shopping = client.get(
            "/api/shopping", params={"start": meal_date.isoformat(), "end": meal_date.isoformat()}
        )
        assert shopping.status_code == 200
        line = next(row for row in shopping.json()["lines"] if row["name"] == "Pasta")
        assert Decimal(line["quantity"]) == Decimal("400")
        assert line["unit"] == "g"

    with make_client(database) as restarted:
        assert restarted.get("/api/recipes").json()[0]["id"] == recipe_id
        assert restarted.get("/api/household").json()["name"] == "North house"
        assert (
            restarted.get(
                "/api/meals", params={"start": meal_date.isoformat(), "end": meal_date.isoformat()}
            ).json()[0]["recipe_id"]
            == recipe_id
        )
        line = next(
            row
            for row in restarted.get(
                "/api/shopping",
                params={"start": meal_date.isoformat(), "end": meal_date.isoformat()},
            ).json()["lines"]
            if row["name"] == "Pasta"
        )
        assert Decimal(line["quantity"]) == Decimal("400")


def test_invalid_input_dates_and_unknown_resources(test_database_url):
    with make_client(test_database_url) as client:
        assert (
            client.post("/api/recipes", json={**recipe_payload(), "servings": 0}).status_code == 422
        )
        assert (
            client.post(
                "/api/recipes",
                json={**recipe_payload(), "ingredients": [{"name": " ", "quantity": 1}]},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/meals", params={"start": "not-a-date", "end": "2026-01-01"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/meals", params={"start": "2026-02-02", "end": "2026-02-01"}
            ).status_code
            == 422
        )
        unknown = "00000000-0000-0000-0000-000000000001"
        assert client.get(f"/api/recipes/{unknown}").status_code == 404
        assert client.delete(f"/api/items/{unknown}").status_code == 404
        assert (
            client.post(
                "/api/meals",
                json={"date": "2026-01-01", "recipe_id": unknown, "servings": 2},
            ).status_code
            == 404
        )


def test_ai_unconfigured_is_unavailable_and_does_not_save(test_database_url):
    with make_client(test_database_url) as client:
        response = client.post("/api/ai/recipe", json={"prompt": "A simple soup"})
        assert response.status_code == 503
        assert client.get("/api/recipes").json() == []


def test_regular_item_api_accepts_interval_and_records_purchase(test_database_url):
    with make_client(test_database_url) as client:
        created = client.post(
            "/api/regular-items",
            json={"name": "Milk", "quantity": 2, "unit": "L", "interval_days": 5},
        )
        assert created.status_code == 201
        assert created.json()["interval_days"] == 5
        assert created.json()["unit"] == "l"
        item_id = created.json()["id"]
        purchase = client.post(
            f"/api/regular-items/{item_id}/purchase", json={"purchased_on": "2026-01-05"}
        )
        assert purchase.status_code == 200
        assert purchase.json()["last_purchased"] == "2026-01-05"


def test_planning_occupied_slot_preserves_existing_meal(test_database_url):
    with make_client(test_database_url) as client:
        recipe = client.post("/api/recipes", json=recipe_payload()).json()
        data = {"date": "2027-01-11", "recipe_id": recipe["id"], "servings": 2}
        original = client.post("/api/meals", json=data)
        conflict = client.post("/api/meals", json={**data, "servings": 4})
        assert conflict.status_code == 409
        assert "already planned" in conflict.json()["detail"]
        meals = client.get("/api/meals?start=2027-01-11&end=2027-01-11").json()
        assert meals == [original.json()]
        assert client.post("/api/meals", json={**data, "slot": "lunch"}).status_code == 201
