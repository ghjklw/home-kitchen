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
                "selections": [{"need_id": need_id, "candidate_id": unknown.id, "packages": 2}],
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
        accepted = client.put(
            "/api/basket/selections/batch",
            json={
                "start": str(start),
                "end": str(end),
                "job_id": job_id,
                "selections": [{"need_id": need_id, "candidate_id": safe.id, "packages": 2}],
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
                "selections": [{"need_id": need_id, "candidate_id": safe.id, "packages": 2}],
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
