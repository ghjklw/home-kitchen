"""Bounded, actor-scoped background jobs for read-only product matching."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field
from starlette.concurrency import run_in_threadpool

from home_kitchen.basket.matching import (
    MAX_NEEDS,
    MAX_RESULTS_PER_NEED,
    BasketCandidate,
    BasketMatchingService,
    BasketMatchResult,
    NeedMatchResult,
)
from home_kitchen.inventory import InventoryService, PurchasePlan

SAFE_ERROR = "Product matching could not finish. Please try again."


class BasketMatchJob(BasketMatchResult):
    job_id: str
    status: Literal["queued", "running", "complete", "failed"] = "queued"
    detail: str | None = None
    needs: list[NeedMatchResult] = Field(default_factory=list)


@dataclass
class _Job:
    actor: str
    scope: int
    fingerprint: tuple
    created: float
    value: BasketMatchJob
    finished: float | None = None
    task: asyncio.Task | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class BasketMatchJobs:
    """Own matching tasks and return isolated snapshots suitable for polling."""

    def __init__(
        self,
        matching: BasketMatchingService,
        inventory: InventoryService,
        scope_provider: Callable[[str], int | None],
        *,
        max_jobs: int = 128,
        max_active: int = 8,
        max_per_actor: int = 2,
        ttl_seconds: float = 600,
        reuse_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        if min(max_jobs, max_active, max_per_actor) < 1:
            raise ValueError("Job limits must be positive")
        self.matching = matching
        self.inventory = inventory
        self.scope_provider = scope_provider
        self.max_jobs = max_jobs
        self.max_active = max_active
        self.max_per_actor = max_per_actor
        self.ttl_seconds = ttl_seconds
        self.reuse_seconds = reuse_seconds
        self.clock = clock
        self._jobs: dict[str, _Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @staticmethod
    def _actor(actor: str) -> None:
        if not actor or not actor.strip():
            raise ValueError("An authenticated actor is required")

    def _discard(self, job_id: str) -> None:
        job = self._jobs.pop(job_id)
        if job.task is not None and not job.task.done():
            job.task.cancel()

    def _prune(self, actor: str, scope: int | None) -> None:
        now = self.clock()
        for job_id, job in list(self._jobs.items()):
            if now - job.created >= self.ttl_seconds or (
                job.actor == actor and (scope is None or job.scope != scope)
            ):
                self._discard(job_id)

    async def start(
        self,
        actor: str,
        start: date,
        end: date,
        *,
        page: int = 1,
        size: int = 5,
        force_refresh: bool = False,
        need_ids: list[UUID] | None = None,
    ) -> BasketMatchJob:
        self._actor(actor)
        if self._closed:
            raise ValueError("Product matching is shutting down")
        if end < start:
            raise ValueError("end date must be on or after start date")
        if not 1 <= page <= 50 or not 1 <= size <= MAX_RESULTS_PER_NEED:
            raise ValueError("page or result size is outside the supported range")
        if need_ids is not None and (
            not 1 <= len(need_ids) <= MAX_NEEDS or len(set(need_ids)) != len(need_ids)
        ):
            raise ValueError(f"Select between 1 and {MAX_NEEDS} unique purchase needs")
        plan = await run_in_threadpool(self.inventory.plan, start, end)
        if need_ids is not None:
            requested = set(need_ids)
            lines = [line for line in plan.purchase_lines if line.id in requested]
            if {line.id for line in lines} != requested:
                raise ValueError("Purchase needs changed. Refresh the shopping list and try again.")
            plan = plan.model_copy(update={"lines": lines})
        scope = await run_in_threadpool(self.scope_provider, actor)
        if self._closed:
            raise ValueError("Product matching is shutting down")
        self._prune(actor, scope)
        if scope is None:
            raise ValueError("Connect your product provider before starting product matching")
        lines = plan.purchase_lines
        fingerprint = (
            actor,
            scope,
            start,
            end,
            page,
            size,
            tuple(
                (
                    line.id,
                    line.name,
                    line.to_buy_quantity,
                    line.unit,
                    line.approximate,
                    line.provenance,
                    line.source_url,
                )
                for line in lines
            ),
        )
        now = self.clock()
        for job in self._jobs.values():
            if job.fingerprint == fingerprint and (
                job.finished is None
                or (
                    not force_refresh
                    and job.value.status == "complete"
                    and now - job.finished <= self.reuse_seconds
                )
            ):
                return job.value.model_copy(deep=True)
        active = [job for job in self._jobs.values() if job.finished is None]
        if (
            len(active) >= self.max_active
            or sum(j.actor == actor for j in active) >= self.max_per_actor
        ):
            raise ValueError("Product matching is busy. Wait for a running search and try again.")
        while len(self._jobs) >= self.max_jobs:
            oldest = next(
                (key for key, job in self._jobs.items() if job.finished is not None), None
            )
            if oldest is None:
                raise ValueError(
                    "Product matching is busy. Wait for a running search and try again."
                )
            self._discard(oldest)
        value = BasketMatchJob(
            job_id=str(uuid4()),
            start=start,
            end=end,
            needs=[
                NeedMatchResult(
                    need_id=line.id,
                    name=line.name,
                    quantity=line.to_buy_quantity,
                    unit=line.unit,
                    page=page,
                    has_more=False,
                    candidates=[],
                    status="pending",
                    approximate=line.approximate,
                    provenance=line.provenance,
                    source_url=line.source_url,
                )
                for line in lines[:MAX_NEEDS]
            ],
            skipped_need_count=max(0, len(lines) - MAX_NEEDS),
        )
        job = _Job(actor, scope, fingerprint, now, value)
        self._jobs[value.job_id] = job
        task = asyncio.create_task(self._run(job, plan, page, size, force_refresh))
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return value.model_copy(deep=True)

    async def get(self, actor: str, job_id: str) -> BasketMatchJob:
        self._actor(actor)
        # Check ownership before touching provider context, including for unknown IDs.
        job = self._jobs.get(job_id)
        if job is None or job.actor != actor:
            raise KeyError(job_id)
        scope = await run_in_threadpool(self.scope_provider, actor)
        self._prune(actor, scope)
        if self._jobs.get(job_id) is not job:
            raise KeyError(job_id)
        return job.value.model_copy(deep=True)

    async def resolve_batch_candidates(
        self,
        actor: str,
        job_id: str,
        start: date,
        end: date,
        references: list[tuple[UUID, str]],
    ) -> dict[tuple[UUID, str], tuple[NeedMatchResult, BasketCandidate, datetime]]:
        """Resolve only current, actor-owned candidates from a completed search snapshot."""
        self._actor(actor)
        job = self._jobs.get(job_id)
        if job is None or job.actor != actor:
            raise KeyError(job_id)
        scope = await run_in_threadpool(self.scope_provider, actor)
        self._prune(actor, scope)
        if self._jobs.get(job_id) is not job:
            raise ValueError(
                "Product matches expired or provider context changed. Refresh matches."
            )
        if job.scope != scope or scope is None:
            raise ValueError(
                "Product matches expired or provider context changed. Refresh matches."
            )
        if (job.value.start, job.value.end) != (start, end):
            raise ValueError("Product matches belong to a different shopping period.")
        if job.value.status != "complete" or job.finished is None:
            raise ValueError("Wait for product matching to finish before saving selections.")
        if self.clock() - job.finished >= self.ttl_seconds:
            raise ValueError("Product matches expired. Refresh matches.")
        by_need = {need.need_id: need for need in job.value.needs}
        resolved: dict[tuple[UUID, str], tuple[NeedMatchResult, BasketCandidate, datetime]] = {}
        for need_id, candidate_id in references:
            need = by_need.get(need_id)
            candidate = (
                next((item for item in need.candidates if item.id == candidate_id), None)
                if need is not None
                else None
            )
            if need is None or candidate is None or need.status != "matched":
                raise ValueError(
                    "A selected product match is stale or unavailable. Refresh matches."
                )
            resolved[(need_id, candidate_id)] = (
                need.model_copy(deep=True),
                candidate.model_copy(deep=True),
                job.observed_at,
            )
        return resolved

    async def _run(self, job: _Job, plan: PurchasePlan, page: int, size: int, refresh: bool):
        async def current() -> bool:
            scope = await run_in_threadpool(self.scope_provider, job.actor)
            return scope is not None and scope == job.scope

        async def progress(need: NeedMatchResult) -> None:
            if await current():
                for index, existing in enumerate(job.value.needs):
                    if existing.need_id == need.need_id:
                        job.value.needs[index] = need.model_copy(deep=True)
                        break

        try:
            job.value.status = "running"
            if not await current():
                raise RuntimeError("Provider context changed")
            result = await self.matching.find_products(
                job.actor,
                plan.start,
                plan.end,
                page=page,
                size=size,
                force_refresh=refresh,
                progress=progress,
                plan=plan,
            )
            if not await current():
                raise RuntimeError("Provider context changed")
            job.value.needs = [need.model_copy(deep=True) for need in result.needs]
            job.value.skipped_need_count = result.skipped_need_count
            job.value.status = "complete"
        except asyncio.CancelledError:
            raise
        except Exception:
            job.value.status = "failed"
            job.value.detail = SAFE_ERROR
            for need in job.value.needs:
                if need.status in {"pending", "searching"}:
                    need.status = "search_failed"
                    need.detail = SAFE_ERROR
        finally:
            job.finished = self.clock()
            job.observed_at = datetime.now(UTC)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
