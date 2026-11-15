"""Background matching stays bounded, isolated and useful while work is pending."""

import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from home_kitchen.basket.jobs import SAFE_ERROR, BasketMatchJobs
from home_kitchen.basket.matching import BasketMatchResult, NeedMatchResult
from home_kitchen.inventory import PurchasePlan, PurchasePlanLine

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
