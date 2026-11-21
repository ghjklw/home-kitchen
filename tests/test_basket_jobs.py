"""Background matching stays bounded, isolated and useful while work is pending."""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from home_kitchen.basket.jobs import SAFE_ERROR, BasketMatchJobs
from home_kitchen.basket.matching import BasketCandidate, BasketMatchResult, NeedMatchResult
from home_kitchen.inventory import PurchasePlan, PurchasePlanLine

pytestmark = pytest.mark.mocked_provider

START = date(2026, 11, 15)
END = date(2026, 11, 21)


class Inventory:
    quantity = Decimal(2)

    def plan(self, start, end):
        return PurchasePlan(
            start=start,
            end=end,
            suggestions=[],
            lines=[
                PurchasePlanLine(
                    id=UUID(int=index),
                    name=f"Need {index}",
                    unit="item",
                    required_quantity=self.quantity,
                    available_quantity=0,
                    covered_quantity=0,
                    to_buy_quantity=self.quantity,
                    origins=[],
                    approximate=True,
                    provenance="Matprat estimate; source text: 1 dl sukker",
                    source_url="https://www.matprat.no/artikler/mengde-mal-og-vekt/mal-og-vekt/",
                )
                for index in (1, 2)
            ],
        )


class Matching:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.partial = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.fail = False

    async def find_products(self, actor, start, end, *, page, size, force_refresh, progress, plan):
        self.calls.append((actor, plan, force_refresh))
        self.started.set()
        needs = [
            NeedMatchResult(
                need_id=line.id,
                name=line.name,
                quantity=line.to_buy_quantity,
                unit=line.unit,
                page=page,
                has_more=False,
                candidates=[],
                status="matched",
                approximate=line.approximate,
                provenance=line.provenance,
                source_url=line.source_url,
            )
            for line in plan.purchase_lines
        ]
        await progress(needs[0])
        self.partial.set()
        try:
            await self.release.wait()
            if self.fail:
                raise RuntimeError("secret provider token")
            return BasketMatchResult(start=start, end=end, needs=needs)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.fixture
async def service():
    inventory = Inventory()
    matching = Matching()
    contexts = {"alice": 1, "bob": 1}
    jobs = BasketMatchJobs(matching, inventory, contexts.get)
    yield jobs, inventory, matching, contexts
    await jobs.close()


async def settled(jobs, actor, job_id):
    async def poll():
        while True:
            value = await jobs.get(actor, job_id)
            if value.status in {"complete", "failed"}:
                return value
            await asyncio.sleep(0)

    return await asyncio.wait_for(poll(), 2)


async def test_start_returns_before_provider_finishes_and_partial_results_survive(service):
    jobs, _, matching, _ = service
    initial = await asyncio.wait_for(jobs.start("alice", START, END), 1)
    assert initial.status == "queued"
    assert [need.status for need in initial.needs] == ["pending", "pending"]
    assert initial.needs[0].approximate is True
    assert "1 dl sukker" in initial.needs[0].provenance
    assert str(initial.needs[0].source_url).startswith("https://www.matprat.no/")
    await asyncio.wait_for(matching.partial.wait(), 1)
    partial = await jobs.get("alice", initial.job_id)
    assert partial.status == "running"
    assert [need.status for need in partial.needs] == ["matched", "pending"]
    # Returned snapshots must not let a poller mutate the owned job.
    partial.needs.clear()
    assert len((await jobs.get("alice", initial.job_id)).needs) == 2
    matching.release.set()
    final = await settled(jobs, "alice", initial.job_id)
    assert final.status == "complete"
    assert [need.status for need in final.needs] == ["matched", "matched"]
    assert final.needs[0].approximate is True


async def test_identical_requests_reuse_running_and_recent_completed_job(service):
    jobs, _, matching, _ = service
    first = await jobs.start("alice", START, END)
    second = await jobs.start("alice", START, END, force_refresh=True)
    assert first.job_id == second.job_id
    matching.release.set()
    await settled(jobs, "alice", first.job_id)
    assert (await jobs.start("alice", START, END)).job_id == first.job_id
    refreshed = await jobs.start("alice", START, END, force_refresh=True)
    assert refreshed.job_id != first.job_id


async def test_changed_quantity_is_new_job_with_fresh_snapshot(service):
    jobs, inventory, matching, _ = service
    old = await jobs.start("alice", START, END)
    inventory.quantity = Decimal(9)
    new = await jobs.start("alice", START, END)
    assert old.job_id != new.job_id
    assert new.needs[0].quantity == 9
    assert (await jobs.get("alice", old.job_id)).needs[0].quantity == 2
    matching.release.set()
    assert (await settled(jobs, "alice", new.job_id)).needs[0].quantity == 9
    assert (await settled(jobs, "alice", old.job_id)).needs[0].quantity == 2


async def test_actor_is_required_and_jobs_are_private(service):
    jobs, _, _, _ = service
    with pytest.raises(ValueError, match="actor"):
        await jobs.start("", START, END)
    first = await jobs.start("alice", START, END)
    with pytest.raises(KeyError):
        await jobs.get("bob", first.job_id)
    assert (await jobs.start("bob", START, END)).job_id != first.job_id


async def test_disconnect_or_reconnect_invalidates_results_and_running_work(service):
    jobs, _, matching, contexts = service
    first = await jobs.start("alice", START, END)
    await matching.partial.wait()
    contexts["alice"] = 2
    with pytest.raises(KeyError):
        await jobs.get("alice", first.job_id)
    second = await jobs.start("alice", START, END)
    assert second.job_id != first.job_id
    contexts["alice"] = None
    with pytest.raises(KeyError):
        await jobs.get("alice", second.job_id)
    with pytest.raises(ValueError, match="Connect"):
        await jobs.start("alice", START, END)


async def test_failed_job_preserves_success_and_hides_exception(service):
    jobs, _, matching, _ = service
    matching.fail = True
    first = await jobs.start("alice", START, END)
    await matching.partial.wait()
    matching.release.set()
    final = await settled(jobs, "alice", first.job_id)
    assert final.status == "failed"
    assert final.detail == SAFE_ERROR
    assert [need.status for need in final.needs] == ["matched", "search_failed"]
    assert "secret" not in final.model_dump_json()


async def test_shutdown_cancels_and_awaits_owned_tasks(service):
    jobs, _, matching, _ = service
    await jobs.start("alice", START, END)
    await matching.partial.wait()
    await jobs.close()
    assert matching.cancelled.is_set()
    assert not jobs._tasks
    with pytest.raises(ValueError, match="shutting down"):
        await jobs.start("alice", START, END)


async def test_active_and_per_actor_capacity_are_bounded(service):
    jobs, inventory, matching, _ = service
    jobs.max_active = 3
    await jobs.start("alice", START, END)
    inventory.quantity = Decimal(3)
    await jobs.start("alice", START, END)
    inventory.quantity = Decimal(4)
    with pytest.raises(ValueError, match="busy"):
        await jobs.start("alice", START, END)
    await jobs.start("bob", START, END)
    inventory.quantity = Decimal(5)
    with pytest.raises(ValueError, match="busy"):
        await jobs.start("bob", START, END)
    assert len(jobs._jobs) == 3
    matching.release.set()


async def test_ttl_reuse_window_and_storage_eviction_are_bounded(service):
    jobs, inventory, matching, _ = service
    now = [0.0]
    jobs.clock = lambda: now[0]
    jobs.max_jobs = 1
    first = await jobs.start("alice", START, END)
    inventory.quantity = Decimal(3)
    with pytest.raises(ValueError, match="busy"):
        await jobs.start("alice", START, END)
    matching.release.set()
    await settled(jobs, "alice", first.job_id)
    now[0] = 61
    inventory.quantity = Decimal(2)
    second = await jobs.start("alice", START, END)
    assert second.job_id != first.job_id
    assert len(jobs._jobs) == 1
    with pytest.raises(KeyError):
        await jobs.get("alice", first.job_id)
    now[0] = 662
    with pytest.raises(KeyError):
        await jobs.get("alice", second.job_id)


async def test_selected_need_retry_only_matches_fresh_selected_line(service):
    jobs, inventory, matching, _ = service
    inventory.quantity = Decimal(7)
    job = await jobs.start("alice", START, END, need_ids=[UUID(int=2)], page=2, force_refresh=True)
    assert [(need.need_id, need.quantity, need.page) for need in job.needs] == [
        (UUID(int=2), Decimal(7), 2)
    ]
    await matching.partial.wait()
    assert len(matching.calls) == 1
    actor, snapshot, refresh = matching.calls[0]
    assert actor == "alice"
    assert refresh is True
    assert [line.id for line in snapshot.lines] == [UUID(int=2)]
    matching.release.set()
    final = await settled(jobs, "alice", job.job_id)
    assert [need.need_id for need in final.needs] == [UUID(int=2)]
    assert final.skipped_need_count == 0


async def test_unknown_or_no_longer_positive_selected_needs_require_refresh(service):
    jobs, inventory, matching, _ = service
    with pytest.raises(ValueError, match="Refresh"):
        await jobs.start("alice", START, END, need_ids=[UUID(int=999)])
    inventory.quantity = Decimal(0)
    with pytest.raises(ValueError, match="Refresh"):
        await jobs.start("alice", START, END, need_ids=[UUID(int=1)])
    assert matching.calls == []
    assert not jobs._jobs


@pytest.mark.parametrize(
    "need_ids", [[], [UUID(int=1), UUID(int=1)], [UUID(int=i) for i in range(101)]]
)
async def test_selected_need_ids_are_bounded_and_unique(service, need_ids):
    jobs, _, matching, _ = service
    with pytest.raises(ValueError, match="unique purchase needs"):
        await jobs.start("alice", START, END, need_ids=need_ids)
    assert matching.calls == []


async def test_selected_retry_reuses_only_same_current_subset(service):
    jobs, _, matching, _ = service
    first = await jobs.start("alice", START, END, need_ids=[UUID(int=1)])
    same = await jobs.start("alice", START, END, need_ids=[UUID(int=1)])
    second = await jobs.start("alice", START, END, need_ids=[UUID(int=2)])
    assert first.job_id == same.job_id
    assert second.job_id != first.job_id
    matching.release.set()
    await settled(jobs, "alice", first.job_id)
    await settled(jobs, "alice", second.job_id)
    assert len(matching.calls) == 2
    assert [line.name for _, snapshot, _ in matching.calls for line in snapshot.lines] == [
        "Need 1",
        "Need 2",
    ]


async def test_batch_candidate_resolution_is_actor_scope_period_and_snapshot_scoped(service):
    jobs, _, matching, contexts = service
    job = await jobs.start("alice", START, END)
    matching.release.set()
    final = await settled(jobs, "alice", job.job_id)
    candidate = BasketCandidate(
        id="store:sku-1",
        provider="store",
        product_id="sku-1",
        name="Product",
        description="1 item",
        pack_quantity=Decimal(1),
        pack_unit="item",
        pack_metadata_status="unverified",
        match_query="Need 1",
        match_reason="Search result",
        identity_status="compatible",
    )
    owned = jobs._jobs[final.job_id]
    owned.value.needs[0].candidates = [candidate]
    reference = [(final.needs[0].need_id, candidate.id)]
    resolved = await jobs.resolve_batch_candidates("alice", final.job_id, START, END, reference)
    assert resolved[(final.needs[0].need_id, candidate.id)][1].product_id == "sku-1"
    with pytest.raises(KeyError):
        await jobs.resolve_batch_candidates("bob", final.job_id, START, END, reference)
    with pytest.raises(ValueError, match="different shopping period"):
        await jobs.resolve_batch_candidates(
            "alice", final.job_id, date(2026, 11, 22), date(2026, 11, 28), reference
        )
    contexts["alice"] = 2
    with pytest.raises(ValueError, match="provider context changed"):
        await jobs.resolve_batch_candidates("alice", final.job_id, START, END, reference)


async def test_unverified_candidates_require_exact_candidate_specific_confirmation(service):
    jobs, _, matching, _ = service
    job = await jobs.start("alice", START, END)
    matching.release.set()
    final = await settled(jobs, "alice", job.job_id)
    manual_a = BasketCandidate(
        id="store:manual-a",
        provider="store",
        product_id="manual-a",
        name="First manual product",
        description="",
        match_query="Need 1",
        match_reason="Identity needs review",
    )
    # Missing identity metadata defaults to unverified.
    manual = BasketCandidate(
        id="store:manual",
        provider="store",
        product_id="manual",
        name="Manual product",
        description="",
        match_query="Need 2",
        match_reason="Identity needs review",
    )
    owned = jobs._jobs[final.job_id]
    owned.value.needs[0].candidates = [manual_a]
    owned.value.needs[1].candidates = [manual]
    references = [
        (final.needs[0].need_id, manual_a.id),
        (final.needs[1].need_id, manual.id),
    ]

    with pytest.raises(ValueError, match="unverified"):
        await jobs.resolve_batch_candidates("alice", final.job_id, START, END, references)

    wrong_pair = {(final.needs[0].need_id, manual.id)}
    with pytest.raises(ValueError, match="must name a selected candidate"):
        await jobs.resolve_batch_candidates(
            "alice",
            final.job_id,
            START,
            END,
            references,
            identity_confirmations=wrong_pair,
        )

    with pytest.raises(ValueError, match="unverified"):
        await jobs.resolve_batch_candidates(
            "alice",
            final.job_id,
            START,
            END,
            references,
            identity_confirmations={(final.needs[1].need_id, manual.id)},
        )

    resolved = await jobs.resolve_batch_candidates(
        "alice",
        final.job_id,
        START,
        END,
        references,
        identity_confirmations=set(references),
    )
    assert resolved[(final.needs[0].need_id, manual_a.id)][1].identity_status == "unverified"
    assert resolved[(final.needs[1].need_id, manual.id)][1].identity_status == "unverified"


async def test_unverified_selection_confirmation_is_not_a_generic_review_flag(service):
    from pydantic import ValidationError

    jobs, _, matching, _ = service
    job = await jobs.start("alice", START, END)
    matching.release.set()
    final = await settled(jobs, "alice", job.job_id)
    candidate = BasketCandidate(
        id="store:unknown",
        provider="store",
        product_id="unknown",
        name="Unknown product",
        description="",
        match_query="Need 1",
        match_reason="Identity unverified",
    )
    final_pair = (final.needs[0].need_id, candidate.id)
    jobs._jobs[final.job_id].value.needs[0].candidates = [candidate]
    with pytest.raises(ValidationError, match="reviewed"):
        BasketCandidate(
            id="store:reviewed",
            provider="store",
            product_id="reviewed",
            name="Reviewed",
            description="",
            match_query="Need 1",
            match_reason="Search result",
            reviewed=True,
        )
    with pytest.raises(ValueError, match="Confirm this exact candidate"):
        await jobs.resolve_batch_candidates("alice", final.job_id, START, END, [final_pair])


async def test_observation_time_is_exposed_only_after_successful_completion(service):
    jobs, _, matching, _ = service
    initial = await jobs.start("alice", START, END)
    assert initial.observed_at is None
    await matching.partial.wait()
    assert (await jobs.get("alice", initial.job_id)).observed_at is None
    old = datetime(2000, 1, 1, tzinfo=UTC)
    jobs._jobs[initial.job_id].observed_at = old
    before = datetime.now(UTC)
    matching.release.set()
    final = await settled(jobs, "alice", initial.job_id)
    assert final.observed_at is not None
    assert before <= final.observed_at <= datetime.now(UTC)
    assert final.observed_at != old
    assert final.observed_at == jobs._jobs[initial.job_id].observed_at
    assert final.model_dump(mode="json")["observed_at"].endswith("Z")
    reused = await jobs.start("alice", START, END)
    assert reused.job_id == final.job_id
    assert reused.observed_at == final.observed_at
    refreshed = await jobs.start("alice", START, END, force_refresh=True)
    assert refreshed.observed_at is None
    next_final = await settled(jobs, "alice", refreshed.job_id)
    assert next_final.observed_at >= final.observed_at


async def test_failed_job_does_not_publish_a_successful_observation_time(service):
    jobs, _, matching, _ = service
    matching.fail = True
    job = await jobs.start("alice", START, END)
    await matching.partial.wait()
    matching.release.set()
    final = await settled(jobs, "alice", job.job_id)
    assert final.status == "failed"
    assert final.needs[0].status == "matched"  # Partial success is not a completed job.
    assert final.observed_at is None
    assert final.model_dump(mode="json")["observed_at"] is None


async def test_job_api_serializes_observation_time_without_database_or_provider(service):
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from home_kitchen.feature_api import feature_router

    jobs, _, matching, _ = service
    app = FastAPI()

    @app.middleware("http")
    async def actor(request: Request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    app.include_router(feature_router(SimpleNamespace(basket_jobs=jobs), None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/basket/match-jobs", json={"start": str(START), "end": str(END)}
        )
        assert response.status_code == 202
        assert response.json()["observed_at"] is None
        job_id = response.json()["job_id"]
        matching.release.set()
        final = await settled(jobs, "alice", job_id)
        response = await client.get(f"/api/basket/match-jobs/{job_id}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["observed_at"] == final.model_dump(mode="json")["observed_at"]


async def test_background_matching_invalidates_prior_matches_on_force_refresh_and_version_invalidation(
    service,
):
    from home_kitchen.basket.jobs import MATCH_JOB_VERSION

    jobs, inventory, matching, _ = service
    first = await jobs.start("alice", START, END)
    matching.release.set()
    settled_first = await settled(jobs, "alice", first.job_id)
    assert settled_first.status == "complete"

    # force_refresh=True invalidates the cached job and forces a new background job
    refreshed = await jobs.start("alice", START, END, force_refresh=True)
    assert refreshed.job_id != first.job_id
    assert refreshed.status in {"queued", "running", "complete"}

    # Bumping MATCH_JOB_VERSION invalidates cached jobs even without force_refresh
    owned_job = jobs._jobs[first.job_id]
    original_fingerprint = owned_job.fingerprint
    # Emulate a job recorded under prior version
    old_version_fingerprint = (
        original_fingerprint[0],
        original_fingerprint[1],
        original_fingerprint[2],
        original_fingerprint[3],
        original_fingerprint[4],
        original_fingerprint[5],
        MATCH_JOB_VERSION - 1,
        original_fingerprint[7],
    )
    owned_job.fingerprint = old_version_fingerprint
    new_job = await jobs.start("alice", START, END)
    assert new_job.job_id != first.job_id


async def test_per_item_refine_and_retry_updates_only_target_need():
    class RefineMatching:
        def __init__(self):
            self.calls = []

        async def find_products(
            self,
            actor,
            start,
            end,
            *,
            page,
            size,
            force_refresh,
            progress,
            plan,
            query_overrides=None,
        ):
            self.calls.append((actor, [line.id for line in plan.purchase_lines], query_overrides))
            needs = []
            for line in plan.purchase_lines:
                query = (query_overrides or {}).get(line.id, line.name)
                candidate = BasketCandidate(
                    id=f"oda:{line.id.int}:{query}",
                    provider="oda",
                    product_id=f"{line.id.int}-{query}",
                    name=f"Result for {query}",
                    description="1 item",
                    pack_quantity=Decimal(1),
                    pack_unit="item",
                    pack_metadata_status="unverified",
                    match_query=query,
                    match_reason="Search result",
                    identity_status="compatible",
                    available=True,
                )
                need = NeedMatchResult(
                    need_id=line.id,
                    name=line.name,
                    quantity=line.to_buy_quantity,
                    unit=line.unit,
                    page=page,
                    has_more=False,
                    candidates=[candidate],
                    status="matched",
                    search_query=query,
                )
                await progress(need)
                needs.append(need)
            return BasketMatchResult(start=start, end=end, needs=needs)

    inventory = Inventory()
    matching = RefineMatching()
    jobs = BasketMatchJobs(matching, inventory, {"alice": 1}.get)
    try:
        initial = await jobs.start("alice", START, END)
        settled_initial = await settled(jobs, "alice", initial.job_id)
        assert settled_initial.status == "complete"
        assert settled_initial.needs[0].search_query == "Need 1"
        assert settled_initial.needs[1].search_query == "Need 2"

        retried = await jobs.retry_need(
            "alice",
            initial.job_id,
            UUID(int=2),
            query="  Tine   helmelk  ",
        )
        assert retried.job_id == initial.job_id
        assert retried.status == "running"
        assert retried.needs[0].status == "matched"
        assert retried.needs[0].candidates[0].name == "Result for Need 1"
        assert retried.needs[1].status == "searching"
        assert retried.needs[1].search_query == "Tine helmelk"
        assert retried.needs[1].revision == 1

        final = await settled(jobs, "alice", initial.job_id)
        assert final.status == "complete"
        assert final.needs[0].revision == 0
        assert final.needs[0].candidates[0].name == "Result for Need 1"
        assert final.needs[1].revision == 1
        assert final.needs[1].search_query == "Tine helmelk"
        assert final.needs[1].candidates[0].name == "Result for Tine helmelk"
    finally:
        await jobs.close()


async def test_stale_retry_response_cannot_overwrite_newer_revision():
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    first_started = asyncio.Event()
    second_started = asyncio.Event()

    class OutOfOrderMatching:
        async def find_products(
            self,
            actor,
            start,
            end,
            *,
            page,
            size,
            force_refresh,
            progress,
            plan,
            query_overrides=None,
        ):
            line = plan.purchase_lines[0]
            query = (query_overrides or {}).get(line.id, line.name)
            if query == "old-query":
                first_started.set()
                try:
                    await first_release.wait()
                except asyncio.CancelledError:
                    # Simulate a provider/adapter ignoring cancellation briefly or returning late
                    raise
            elif query == "new-query":
                second_started.set()
                await second_release.wait()
            needs = [
                NeedMatchResult(
                    need_id=item.id,
                    name=item.name,
                    quantity=item.to_buy_quantity,
                    unit=item.unit,
                    page=page,
                    has_more=False,
                    candidates=[
                        BasketCandidate(
                            id=f"oda:{query}",
                            provider="oda",
                            product_id=query,
                            name=f"Product {query}",
                            description="1 item",
                            match_query=query,
                            match_reason="Search result",
                            available=True,
                        )
                    ],
                    status="matched",
                    search_query=(query_overrides or {}).get(item.id, item.name),
                )
                for item in plan.purchase_lines
            ]
            return BasketMatchResult(start=start, end=end, needs=needs)

    inventory = Inventory()
    jobs = BasketMatchJobs(OutOfOrderMatching(), inventory, {"alice": 1}.get)
    try:
        initial = await jobs.start("alice", START, END)
        await settled(jobs, "alice", initial.job_id)

        # Start revision 1 (slow)
        await jobs.retry_need("alice", initial.job_id, UUID(int=1), query="old-query")
        await asyncio.wait_for(first_started.wait(), 1)

        # Start revision 2 (supersedes revision 1)
        await jobs.retry_need("alice", initial.job_id, UUID(int=1), query="new-query")
        await asyncio.wait_for(second_started.wait(), 1)
        second_release.set()
        after_second = await settled(jobs, "alice", initial.job_id)
        assert after_second.needs[0].revision == 2
        assert after_second.needs[0].search_query == "new-query"
        assert after_second.needs[0].candidates[0].product_id == "new-query"

        # Now release old-query; even if uncancelled, revision guard keeps new-query
        first_release.set()
        await asyncio.sleep(0)
        final = await jobs.get("alice", initial.job_id)
        assert final.needs[0].revision == 2
        assert final.needs[0].search_query == "new-query"
        assert final.needs[0].candidates[0].product_id == "new-query"
    finally:
        await jobs.close()


async def test_main_job_completion_cannot_overwrite_need_superseded_by_retry():
    need1_done = asyncio.Event()
    release_main = asyncio.Event()

    class MainAndRetryMatching:
        async def find_products(
            self,
            actor,
            start,
            end,
            *,
            page,
            size,
            force_refresh,
            progress,
            plan,
            query_overrides=None,
        ):
            if len(plan.purchase_lines) == 2:
                first_line = plan.purchase_lines[0]
                first_result = NeedMatchResult(
                    need_id=first_line.id,
                    name=first_line.name,
                    quantity=first_line.to_buy_quantity,
                    unit=first_line.unit,
                    page=page,
                    has_more=False,
                    candidates=[
                        BasketCandidate(
                            id="oda:initial-1",
                            provider="oda",
                            product_id="initial-1",
                            name="Initial Need 1",
                            description="1 item",
                            match_query=first_line.name,
                            match_reason="Initial",
                            available=True,
                        )
                    ],
                    status="matched",
                    search_query=first_line.name,
                )
                await progress(first_result)
                need1_done.set()
                await release_main.wait()
                second_line = plan.purchase_lines[1]
                second_result = NeedMatchResult(
                    need_id=second_line.id,
                    name=second_line.name,
                    quantity=second_line.to_buy_quantity,
                    unit=second_line.unit,
                    page=page,
                    has_more=False,
                    candidates=[],
                    status="no_results",
                    search_query=second_line.name,
                )
                return BasketMatchResult(start=start, end=end, needs=[first_result, second_result])
            target = plan.purchase_lines[0]
            query = (query_overrides or {}).get(target.id, target.name)
            return BasketMatchResult(
                start=start,
                end=end,
                needs=[
                    NeedMatchResult(
                        need_id=target.id,
                        name=target.name,
                        quantity=target.to_buy_quantity,
                        unit=target.unit,
                        page=page,
                        has_more=False,
                        candidates=[
                            BasketCandidate(
                                id="oda:refined-1",
                                provider="oda",
                                product_id="refined-1",
                                name="Refined Need 1",
                                description="1 item",
                                match_query=query,
                                match_reason="Refined",
                                available=True,
                            )
                        ],
                        status="matched",
                        search_query=query,
                    )
                ],
            )

    jobs = BasketMatchJobs(MainAndRetryMatching(), Inventory(), {"alice": 1}.get)
    try:
        job = await jobs.start("alice", START, END)
        await asyncio.wait_for(need1_done.wait(), 1)

        await jobs.retry_need("alice", job.job_id, UUID(int=1), query="Refined query")
        # Wait briefly so retry finishes while main is still blocked
        await asyncio.sleep(0.05)
        mid = await jobs.get("alice", job.job_id)
        assert mid.status == "running"
        assert mid.needs[0].revision == 1
        assert mid.needs[0].candidates[0].product_id == "refined-1"

        release_main.set()
        final = await settled(jobs, "alice", job.job_id)
        assert final.status == "complete"
        assert final.needs[0].revision == 1
        assert final.needs[0].search_query == "Refined query"
        assert final.needs[0].candidates[0].product_id == "refined-1"
        assert final.needs[1].status == "no_results"
    finally:
        await jobs.close()


async def test_slow_or_failing_item_does_not_block_other_items_in_job():
    from home_kitchen.basket.matching import (
        BasketMatchingService,
        ProviderProduct,
        ProviderSearchGroup,
    )

    need2_gate = asyncio.Event()

    class MixedProvider:
        max_queries = 1

        async def search_products(self, actor_id, queries, page, size):
            query = queries[0]
            if query == "Need 2":
                await need2_gate.wait()
                raise TimeoutError("Need 2 timed out")
            return [
                ProviderSearchGroup(
                    query=query,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="101",
                            name="Fast Need 1",
                            description="1 item",
                            price="19.90",
                            available=True,
                        )
                    ],
                )
            ]

    matching = BasketMatchingService(Inventory(), MixedProvider())
    jobs = BasketMatchJobs(matching, Inventory(), {"alice": 1}.get)
    try:
        started = await jobs.start("alice", START, END)

        async def wait_for_need1():
            while True:
                snap = await jobs.get("alice", started.job_id)
                if snap.needs[0].status == "matched":
                    return snap
                await asyncio.sleep(0.01)

        partial = await asyncio.wait_for(wait_for_need1(), 2)
        assert partial.status == "running"
        assert partial.needs[0].status == "matched"
        assert partial.needs[1].status == "pending"

        need2_gate.set()
        final = await settled(jobs, "alice", started.job_id)
        assert final.status == "complete"
        assert final.needs[0].status == "matched"
        assert final.needs[1].status == "search_failed"
    finally:
        await jobs.close()


async def test_refresh_candidate_availability_updates_snapshot_and_blocks_unavailable_commit():
    from home_kitchen.basket.matching import (
        BasketMatchingService,
        ProviderProduct,
        ProviderSearchGroup,
    )

    class AvailabilityProvider:
        def __init__(self):
            self.available_1 = True
            self.available_2 = True
            self.price_1 = "25.00"

        async def search_products(self, actor_id, queries, page, size):
            return [
                ProviderSearchGroup(
                    query=query,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="101",
                            name="Product 1",
                            description="1 item",
                            price=self.price_1,
                            available=self.available_1,
                        ),
                        ProviderProduct(
                            provider="oda",
                            product_id="202",
                            name="Product 2",
                            description="1 item",
                            price="30.00",
                            available=self.available_2,
                        ),
                    ],
                )
                for query in queries
            ]

    provider = AvailabilityProvider()
    matching = BasketMatchingService(Inventory(), provider)
    jobs = BasketMatchJobs(matching, Inventory(), {"alice": 1}.get)
    try:
        job = await jobs.start("alice", START, END)
        final = await settled(jobs, "alice", job.job_id)
        # Seed distinct candidate IDs per need
        owned = jobs._jobs[final.job_id]
        owned.value.needs[0].candidates = [
            BasketCandidate(
                id="oda:101",
                provider="oda",
                product_id="101",
                name="Product 1",
                description="1 item",
                price="25.00",
                pack_quantity=Decimal(1),
                pack_unit="item",
                pack_metadata_status="unverified",
                match_query="Need 1",
                match_reason="Search",
                identity_status="compatible",
                available=True,
            )
        ]
        owned.value.needs[1].candidates = [
            BasketCandidate(
                id="oda:202",
                provider="oda",
                product_id="202",
                name="Product 2",
                description="1 item",
                price="30.00",
                pack_quantity=Decimal(1),
                pack_unit="item",
                pack_metadata_status="unverified",
                match_query="Need 2",
                match_reason="Search",
                identity_status="compatible",
                available=True,
            )
        ]

        # Product 2 goes out of stock and Product 1 changes price
        provider.available_2 = False
        provider.price_1 = "29.90"
        refreshed = await jobs.refresh_candidate_availability(
            "alice",
            final.job_id,
            START,
            END,
            [(UUID(int=1), "oda:101"), (UUID(int=2), "oda:202")],
        )
        assert refreshed.all_available is False
        assert [item.product_id for item in refreshed.unavailable_items] == ["202"]
        assert [item.product_id for item in refreshed.changed_items] == ["101", "202"]
        assert refreshed.items[0].price_changed is True
        assert refreshed.items[0].current_price == "29.90"

        # Committing the out-of-stock item is blocked, while the valid item can still be resolved
        with pytest.raises(ValueError, match="unavailable"):
            await jobs.resolve_batch_candidates(
                "alice", final.job_id, START, END, [(UUID(int=2), "oda:202")]
            )
        valid_resolved = await jobs.resolve_batch_candidates(
            "alice", final.job_id, START, END, [(UUID(int=1), "oda:101")]
        )
        assert valid_resolved[(UUID(int=1), "oda:101")][1].price == "29.90"
    finally:
        await jobs.close()


async def test_retry_and_availability_refresh_api_endpoints():
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from httpx import ASGITransport, AsyncClient

    from home_kitchen.basket.matching import (
        BasketMatchingService,
        ProviderProduct,
        ProviderSearchGroup,
    )
    from home_kitchen.basket.models import (
        BasketNeedStatus,
        BasketReview,
        ProductSnapshot,
        PurchaseNeed,
        SelectedProduct,
    )
    from home_kitchen.feature_api import feature_router

    class LiveProvider:
        def __init__(self):
            self.available = False

        async def search_products(self, actor_id, queries, page, size):
            return [
                ProviderSearchGroup(
                    query=q,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="555",
                            name=f"Oda {q}",
                            description="1 item",
                            price="39.90",
                            available=self.available,
                        )
                    ],
                )
                for q in queries
            ]

    provider = LiveProvider()
    inventory = Inventory()
    matching = BasketMatchingService(inventory, provider)
    jobs = BasketMatchJobs(matching, inventory, {"alice": 1}.get)
    saved_review = BasketReview(
        needs=[
            BasketNeedStatus(
                need=PurchaseNeed(id=UUID(int=1), name="Need 1", quantity=Decimal(2), unit="item"),
                selections=[
                    SelectedProduct(
                        need_id=UUID(int=1),
                        product=ProductSnapshot(
                            provider="oda",
                            product_id="555",
                            name="Oda Need 1",
                            pack_quantity=Decimal(1),
                            pack_unit="item",
                            price=Decimal("35.00"),
                            available=True,
                            observed_at=datetime.now(UTC),
                        ),
                        packages=2,
                    )
                ],
                allocated_quantity=Decimal(2),
                remaining_quantity=Decimal(0),
                overbuy_quantity=Decimal(0),
                ready=True,
            )
        ],
        total_cost=Decimal("70.00"),
        price_complete=True,
        currency="NOK",
        ready_for_review=True,
        issues=[],
    )
    basket_stub = SimpleNamespace(review=lambda s, e: saved_review)
    app = FastAPI()

    @app.middleware("http")
    async def actor(request: Request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    app.include_router(feature_router(SimpleNamespace(basket_jobs=jobs), basket_stub))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            started = await client.post(
                "/api/basket/match-jobs", json={"start": str(START), "end": str(END)}
            )
            assert started.status_code == 202
            job_id = started.json()["job_id"]
            await settled(jobs, "alice", job_id)

            bad = await client.post(
                f"/api/basket/match-jobs/{job_id}/needs/{UUID(int=1)}/retry",
                json={"query": "   "},
            )
            assert bad.status_code == 422

            retried = await client.post(
                f"/api/basket/match-jobs/{job_id}/needs/{UUID(int=1)}/retry",
                json={"query": "Havremel"},
            )
            assert retried.status_code == 202
            assert retried.headers["cache-control"] == "no-store"
            assert retried.json()["needs"][0]["search_query"] == "Havremel"
            assert retried.json()["needs"][0]["revision"] == 1
            final = await settled(jobs, "alice", job_id)
            assert final.needs[0].candidates[0].match_query == "Havremel"

            refreshed = await client.post(
                "/api/basket/availability-refresh",
                json={"start": str(START), "end": str(END)},
            )
            assert refreshed.status_code == 200
            assert refreshed.headers["cache-control"] == "no-store"
            body = refreshed.json()
            assert body["all_available"] is False
            assert len(body["unavailable_items"]) == 1
            assert body["unavailable_items"][0]["product_id"] == "555"
            assert body["unavailable_items"][0]["availability_status"] == "unavailable"
            assert len(body["changed_items"]) == 1
    finally:
        await jobs.close()
