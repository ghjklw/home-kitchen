from datetime import date
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from home_kitchen.app import create_app
from home_kitchen.basket.jobs import BasketMatchJob, _Job
from home_kitchen.basket.matching import BasketCandidate, NeedMatchResult
from home_kitchen.settings import Settings


def test_batch_api_resolves_only_job_candidates_and_rejects_unknown_packages(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    actor = "local-development"
    start, end = date(2026, 11, 16), date(2026, 11, 22)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post("/api/items", json={"name": "Pasta", "quantity": "2", "unit": "item"})
        need = client.get("/api/basket", params={"start": start, "end": end}).json()["needs"][0][
            "need"
        ]
        need_id = need["id"]
        features = app.state.features
        features.basket_jobs.scope_provider = lambda current_actor: 1
        scope = 1

        def seed(candidate):
            job_id = str(uuid4())
            match = NeedMatchResult(
                need_id=need_id,
                name="Pasta",
                quantity=Decimal(2),
                unit="item",
                page=1,
                has_more=False,
                candidates=[candidate],
            )
            now = features.basket_jobs.clock()
            value = BasketMatchJob(
                job_id=job_id,
                status="complete",
                start=start,
                end=end,
                needs=[match],
            )
            features.basket_jobs._jobs[job_id] = _Job(actor, scope, (), now, value, finished=now)
            return job_id

        unknown = BasketCandidate(
            id="provider:unknown",
            provider="provider",
            product_id="unknown",
            name="Pasta",
            description="",
            match_query="Pasta",
            match_reason="Search result",
        )
        refused = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": seed(unknown),
                "selections": [
                    {
                        "need_id": need_id,
                        "candidate_id": unknown.id,
                        "packages": 2,
                        "confirm_identity": True,
                    }
                ],
            },
        )
        assert refused.status_code == 409
        assert (
            client.get("/api/basket", params={"start": start, "end": end}).json()["needs"][0][
                "selections"
            ]
            == []
        )

        safe = BasketCandidate(
            id="provider:safe",
            provider="provider",
            product_id="safe",
            name="Pasta",
            description="1 item",
            product_url="https://shop.example.test/products/safe",
            price="12.50",
            pack_description="1 item",
            pack_quantity=Decimal(1),
            pack_unit="item",
            pack_metadata_source="product_page",
            pack_metadata_status="unverified",
            match_query="Pasta",
            match_reason="Search result",
        )
        job_id = seed(safe)
        old_selection = client.put(
            "/api/basket/selections",
            json={
                "start": str(start),
                "end": str(end),
                "selection": {
                    "need_id": need_id,
                    "product": {
                        "provider": "provider",
                        "product_id": "old-choice",
                        "name": "Old choice",
                        "pack_quantity": "1",
                        "pack_unit": "item",
                        "observed_at": "2026-11-16T12:00:00Z",
                    },
                    "packages": 2,
                },
            },
        )
        assert old_selection.status_code == 200
        response = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [
                    {
                        "need_id": need_id,
                        "candidate_id": safe.id,
                        "packages": 2,
                        "product": {"product_id": "client-forged", "pack_quantity": 100},
                    }
                ],
            },
        )
        assert response.status_code == 422
        unconfirmed = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [{"need_id": need_id, "candidate_id": safe.id, "packages": 2}],
            },
        )
        assert unconfirmed.status_code == 409
        assert "identity is unverified" in unconfirmed.json()["detail"].casefold()
        basket_after_rejection = client.get(
            "/api/basket", params={"start": start, "end": end}
        ).json()
        assert (
            basket_after_rejection["needs"][0]["selections"][0]["product"]["product_id"]
            == "old-choice"
        )
        with features.kitchen.store.engine.connect() as connection:
            rejected_event_count = connection.execute(
                text(
                    "SELECT count(*) FROM kitchen_history "
                    "WHERE structured->>'action' = 'basket_product_selected'"
                )
            ).scalar_one()
        assert rejected_event_count == 1
        accepted = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [
                    {
                        "need_id": need_id,
                        "candidate_id": safe.id,
                        "packages": 2,
                        "confirm_identity": True,
                    }
                ],
            },
        )
        assert accepted.status_code == 200, accepted.json()
        selection = accepted.json()["needs"][0]["selections"][0]
        assert len(accepted.json()["needs"][0]["selections"]) == 1
        assert selection["product"]["product_id"] == "safe"
        assert selection["product"]["pack_quantity"] == "1"
        assert selection["product"]["evidence_status"] == "unverified"
        with features.kitchen.store.engine.connect() as connection:
            event_count = connection.execute(
                text(
                    "SELECT count(*) FROM kitchen_history "
                    "WHERE structured->>'action' = 'basket_product_selected'"
                )
            ).scalar_one()
        retried = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [
                    {
                        "need_id": need_id,
                        "candidate_id": safe.id,
                        "packages": 2,
                        "confirm_identity": True,
                    }
                ],
            },
        )
        assert retried.status_code == 200
        with features.kitchen.store.engine.connect() as connection:
            retried_event_count = connection.execute(
                text(
                    "SELECT count(*) FROM kitchen_history "
                    "WHERE structured->>'action' = 'basket_product_selected'"
                )
            ).scalar_one()
        assert retried_event_count == event_count == 2


def test_batch_api_counts_known_mass_sku_as_pack_but_rejects_tomato_item_guess(test_database_url):
    app = create_app(Settings(local_dev=True, database_url=test_database_url))
    actor = "local-development"
    start, end = date(2026, 11, 16), date(2026, 11, 22)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert (
            client.post(
                "/api/items", json={"name": "Coffee", "quantity": "1", "unit": "pack"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/items", json={"name": "Tomato", "quantity": "1.333", "unit": "item"}
            ).status_code
            == 201
        )
        needs = client.get("/api/basket", params={"start": start, "end": end}).json()["needs"]
        by_name = {entry["need"]["name"].casefold(): entry["need"] for entry in needs}
        coffee_need, tomato_need = by_name["coffee"], by_name["tomato"]
        features = app.state.features
        features.basket_jobs.scope_provider = lambda current_actor: 1
        now = features.basket_jobs.clock()

        coffee = BasketCandidate(
            id="oda:coffee-500g",
            provider="oda",
            product_id="coffee-500g",
            name="Coffee 500 g",
            description="500 g",
            product_url="https://oda.com/no/products/coffee-500g",
            price="99.00",
            pack_description="500 g",
            pack_quantity=Decimal("500"),
            pack_unit="g",
            pack_metadata_source="oda_description",
            pack_metadata_status="unverified",
            match_query="coffee",
            match_reason="Search result",
        )
        tomato = BasketCandidate(
            id="oda:tomato-500g",
            provider="oda",
            product_id="tomato-500g",
            name="Tomatoes 500 g",
            description="500 g",
            product_url="https://oda.com/no/products/tomato-500g",
            price="39.00",
            pack_description="500 g",
            pack_quantity=Decimal("500"),
            pack_unit="g",
            pack_metadata_source="oda_description",
            pack_metadata_status="unverified",
            match_query="tomato",
            match_reason="Search result",
        )
        matches = [
            NeedMatchResult(
                need_id=coffee_need["id"],
                name="Coffee",
                quantity=Decimal("1"),
                unit="pack",
                page=1,
                has_more=False,
                candidates=[coffee],
            ),
            NeedMatchResult(
                need_id=tomato_need["id"],
                name="Tomato",
                quantity=Decimal("1.333"),
                unit="item",
                page=1,
                has_more=False,
                candidates=[tomato],
                approximate=True,
                provenance="recipe-derived estimate",
                source_url="https://example.test/recipe/tomato",
            ),
        ]
        job_id = str(uuid4())
        job = BasketMatchJob(job_id=job_id, status="complete", start=start, end=end, needs=matches)
        features.basket_jobs._jobs[job_id] = _Job(actor, 1, (), now, job, finished=now)

        rejected = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [
                    {"need_id": coffee_need["id"], "candidate_id": coffee.id, "packages": 1},
                    {"need_id": tomato_need["id"], "candidate_id": tomato.id, "packages": 1},
                ],
            },
        )
        assert rejected.status_code == 409
        after_rejection = client.get("/api/basket", params={"start": start, "end": end}).json()
        assert all(not entry["selections"] for entry in after_rejection["needs"])

        accepted = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [
                    {
                        "need_id": coffee_need["id"],
                        "candidate_id": coffee.id,
                        "packages": 1,
                        "confirm_identity": True,
                    }
                ],
            },
        )
        assert accepted.status_code == 200, accepted.json()
        accepted_needs = {
            entry["need"]["name"].casefold(): entry for entry in accepted.json()["needs"]
        }
        saved_coffee, saved_tomato = accepted_needs["coffee"], accepted_needs["tomato"]
        assert saved_coffee["need"]["name"] == "Coffee"
        assert saved_coffee["allocated_quantity"] == "1"
        assert saved_coffee["selections"][0]["product"]["pack_quantity"] == "500"
        assert saved_coffee["selections"][0]["product"]["pack_unit"] == "g"
        assert saved_tomato["need"]["name"] == "Tomato"
        assert saved_tomato["selections"] == []
        with features.kitchen.store.engine.connect() as connection:
            event = connection.execute(
                text(
                    "SELECT structured FROM kitchen_history "
                    "WHERE structured->>'action' = 'basket_product_selected' "
                    "AND structured->>'product_id' = :product_id"
                ),
                {"product_id": "coffee-500g"},
            ).scalar_one()
        assert event["allocated_quantity"] == "1"
        assert event["allocated_unit"] == "pack"
        assert event["pack_quantity"] == "500"
        assert event["pack_unit"] == "g"
