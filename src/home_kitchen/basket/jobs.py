"""Bounded, actor-scoped background jobs for read-only product matching."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from home_kitchen.basket.matching import (
    MAX_NEEDS,
    MAX_RESULTS_PER_NEED,
    AvailabilityCheckOutcome,
    AvailabilityTarget,
    BasketCandidate,
    BasketMatchingService,
    BasketMatchResult,
    NeedMatchResult,
)
from home_kitchen.basket.ports import PurchasePlanPort
from home_kitchen.shopping import PurchasePlan, PurchasePlanLine

SAFE_ERROR = "Product matching could not finish. Please try again."
MATCH_JOB_VERSION = 2


class BasketMatchJob(BasketMatchResult):
    job_id: str
    status: Literal["queued", "running", "complete", "failed"] = "queued"
    detail: str | None = None
    observed_at: datetime | None = None
    needs: list[NeedMatchResult] = Field(default_factory=list)


class BackgroundMatchingHealth(BaseModel):
    """Freshness and failure summary for background Oda product matching."""

    status: Literal["idle", "running", "healthy", "stale", "failed"] = "idle"
    active_jobs: int = 0
    last_completed_at: datetime | None = None
    last_failed_at: datetime | None = None
    last_error: str | None = None
    stale: bool = False
    freshness_note: str | None = None


class SelectionAvailabilityItem(BaseModel):
    need_id: UUID
    need_name: str
    provider: str = "oda"
    product_id: str
    product_name: str
    packages: int = 1
    available: bool | None = None
    previous_available: bool | None = None
    availability_status: Literal["available", "unavailable", "unknown"]
    availability_description: str | None = None
    previous_price: str | None = None
    current_price: str | None = None
    price_changed: bool = False
    search_query: str | None = None


class BasketAvailabilityRefreshResult(BaseModel):
    checked_at: datetime
    all_available: bool
    items: list[SelectionAvailabilityItem] = Field(default_factory=list)
    unavailable_items: list[SelectionAvailabilityItem] = Field(default_factory=list)
    changed_items: list[SelectionAvailabilityItem] = Field(default_factory=list)


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
    need_revisions: dict[UUID, int] = field(default_factory=dict)
    need_queries: dict[UUID, str] = field(default_factory=dict)
    need_tasks: dict[UUID, asyncio.Task] = field(default_factory=dict)
    main_failed: bool = False


class BasketMatchJobs:
    """Own matching tasks and return isolated snapshots suitable for polling."""

    def __init__(
        self,
        matching: BasketMatchingService,
        inventory: PurchasePlanPort,
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
        for task in job.need_tasks.values():
            if not task.done():
                task.cancel()
        job.need_tasks.clear()

    def _prune(self, actor: str, scope: int | None) -> None:
        now = self.clock()
        for job_id, job in list(self._jobs.items()):
            if now - job.created >= self.ttl_seconds or (
                job.actor == actor and (scope is None or job.scope != scope)
            ):
                self._discard(job_id)

    @staticmethod
    def _clean_query_overrides(
        query_overrides: dict[UUID, str] | None,
        valid_ids: set[UUID],
    ) -> dict[UUID, str]:
        if not query_overrides:
            return {}
        cleaned: dict[UUID, str] = {}
        for need_id, raw_query in query_overrides.items():
            if need_id not in valid_ids:
                raise ValueError("Search query override references an unknown purchase need.")
            text = " ".join(raw_query.split())
            if not text or len(text) > 200:
                raise ValueError("Search query must contain 1 to 200 characters")
            cleaned[need_id] = text
        return cleaned

    def _supports_query_overrides(self) -> bool:
        try:
            params = inspect.signature(self.matching.find_products).parameters
        except (TypeError, ValueError):
            return False
        return "query_overrides" in params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
        )

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
        query_overrides: dict[UUID, str] | None = None,
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
        cleaned_overrides = self._clean_query_overrides(
            query_overrides, {line.id for line in plan.purchase_lines}
        )
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
            MATCH_JOB_VERSION,
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
            tuple(sorted((str(k), v) for k, v in cleaned_overrides.items())),
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
                    search_query=cleaned_overrides.get(line.id, line.name),
                    revision=0,
                )
                for line in lines[:MAX_NEEDS]
            ],
            skipped_need_count=max(0, len(lines) - MAX_NEEDS),
        )
        job = _Job(
            actor,
            scope,
            fingerprint,
            now,
            value,
            need_queries=dict(cleaned_overrides),
        )
        self._jobs[value.job_id] = job
        task = asyncio.create_task(
            self._run(job, plan, page, size, force_refresh, cleaned_overrides)
        )
        job.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return value.model_copy(deep=True)

    async def retry_need(
        self,
        actor: str,
        job_id: str,
        need_id: UUID,
        *,
        query: str | None = None,
        page: int = 1,
        size: int = 5,
        force_refresh: bool = True,
    ) -> BasketMatchJob:
        """Refine or retry matching for one need inside an existing job with revision guard."""
        self._actor(actor)
        if self._closed:
            raise ValueError("Product matching is shutting down")
        if not 1 <= page <= 50 or not 1 <= size <= MAX_RESULTS_PER_NEED:
            raise ValueError("page or result size is outside the supported range")
        cleaned_query: str | None = None
        if query is not None:
            cleaned_query = " ".join(query.split())
            if not cleaned_query or len(cleaned_query) > 200:
                raise ValueError("Search query must contain 1 to 200 characters")
        job = self._jobs.get(job_id)
        if job is None or job.actor != actor:
            raise KeyError(job_id)
        scope = await run_in_threadpool(self.scope_provider, actor)
        self._prune(actor, scope)
        if self._jobs.get(job_id) is not job or job.scope != scope or scope is None:
            raise KeyError(job_id)
        need_index = next(
            (idx for idx, item in enumerate(job.value.needs) if item.need_id == need_id),
            None,
        )
        if need_index is None:
            raise KeyError(str(need_id))
        plan = await run_in_threadpool(self.inventory.plan, job.value.start, job.value.end)
        if self._closed:
            raise ValueError("Product matching is shutting down")
        if self._jobs.get(job_id) is not job:
            raise KeyError(job_id)
        line = next((item for item in plan.purchase_lines if item.id == need_id), None)
        if line is None:
            raise ValueError("Purchase needs changed. Refresh the shopping list and try again.")

        previous_task = job.need_tasks.get(need_id)
        if previous_task is not None and not previous_task.done():
            previous_task.cancel()

        revision = job.need_revisions.get(need_id, 0) + 1
        job.need_revisions[need_id] = revision
        existing = job.value.needs[need_index]
        effective_query = (
            cleaned_query
            if cleaned_query is not None
            else (job.need_queries.get(need_id) or existing.search_query or line.name)
        )
        job.need_queries[need_id] = effective_query
        job.value.needs[need_index] = existing.model_copy(
            update={
                "name": line.name,
                "quantity": line.to_buy_quantity,
                "unit": line.unit,
                "page": page,
                "has_more": False,
                "candidates": [],
                "status": "searching",
                "detail": None,
                "search_query": effective_query,
                "revision": revision,
                "approximate": line.approximate,
                "provenance": line.provenance,
                "source_url": line.source_url,
            }
        )
        job.value.status = "running"
        job.value.detail = None
        job.finished = None
        job.main_failed = False

        task = asyncio.create_task(
            self._run_need(
                job,
                line,
                plan.start,
                plan.end,
                effective_query,
                page,
                size,
                force_refresh,
                revision,
            )
        )
        job.need_tasks[need_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.value.model_copy(deep=True)

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
        identity_confirmations: set[tuple[UUID, str]] | frozenset[tuple[UUID, str]] = frozenset(),
    ) -> dict[tuple[UUID, str], tuple[NeedMatchResult, BasketCandidate, datetime]]:
        """Resolve only current, actor-owned candidates from a completed search snapshot."""
        reference_set = set(references)
        if not set(identity_confirmations) <= reference_set:
            raise ValueError("Identity confirmation must name a selected candidate for its need.")
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
            if candidate.available is False:
                raise ValueError(
                    "A selected product is currently unavailable at Oda. Choose an alternative or refresh matches."
                )
            if (
                candidate.identity_status != "compatible"
                and (need_id, candidate_id) not in identity_confirmations
            ):
                raise ValueError(
                    "Product identity is unverified. Confirm this exact candidate for this need."
                )
            resolved[(need_id, candidate_id)] = (
                need.model_copy(deep=True),
                candidate.model_copy(deep=True),
                need.observed_at or job.observed_at,
            )
        return resolved

    @staticmethod
    def _to_availability_result(
        outcomes: list[AvailabilityCheckOutcome], checked_at: datetime
    ) -> BasketAvailabilityRefreshResult:
        items = [
            SelectionAvailabilityItem(
                need_id=outcome.target.need_id,
                need_name=outcome.target.need_name,
                provider=outcome.target.provider,
                product_id=outcome.target.product_id,
                product_name=outcome.fresh_product.name
                if outcome.fresh_product
                else outcome.target.product_name,
                packages=outcome.target.packages,
                available=outcome.available,
                previous_available=outcome.target.previous_available,
                availability_status=outcome.availability_status,
                availability_description=outcome.availability_description,
                previous_price=outcome.target.previous_price,
                current_price=outcome.current_price,
                price_changed=outcome.price_changed,
                search_query=outcome.checked_query or outcome.target.search_query,
            )
            for outcome in outcomes
        ]
        unavailable_items = [item for item in items if item.availability_status != "available"]
        changed_items = [
            item
            for item in items
            if item.price_changed
            or (item.previous_available is not None and item.available != item.previous_available)
        ]
        return BasketAvailabilityRefreshResult(
            checked_at=checked_at,
            all_available=not unavailable_items,
            items=items,
            unavailable_items=unavailable_items,
            changed_items=changed_items,
        )

    async def refresh_candidate_availability(
        self,
        actor: str,
        job_id: str,
        start: date,
        end: date,
        references: list[tuple[UUID, str]],
    ) -> BasketAvailabilityRefreshResult:
        """Refresh live provider availability for selected job candidates before commit/review."""
        self._actor(actor)
        job = self._jobs.get(job_id)
        if job is None or job.actor != actor:
            raise KeyError(job_id)
        scope = await run_in_threadpool(self.scope_provider, actor)
        self._prune(actor, scope)
        if self._jobs.get(job_id) is not job or job.scope != scope or scope is None:
            raise ValueError(
                "Product matches expired or provider context changed. Refresh matches."
            )
        if (job.value.start, job.value.end) != (start, end):
            raise ValueError("Product matches belong to a different shopping period.")
        if job.value.status != "complete" or job.finished is None:
            raise ValueError("Wait for product matching to finish before checking availability.")

        by_need = {need.need_id: need for need in job.value.needs}
        targets: list[AvailabilityTarget] = []
        for need_id, candidate_id in references:
            need = by_need.get(need_id)
            candidate = (
                next((item for item in need.candidates if item.id == candidate_id), None)
                if need is not None
                else None
            )
            if need is None or candidate is None:
                raise ValueError(
                    "A selected product match is stale or unavailable. Refresh matches."
                )
            targets.append(
                AvailabilityTarget(
                    need_id=need_id,
                    need_name=need.name,
                    provider=candidate.provider,
                    product_id=candidate.product_id,
                    product_name=candidate.name,
                    previous_available=candidate.available,
                    previous_price=candidate.price,
                    search_query=candidate.match_query or need.search_query or need.name,
                )
            )
        outcomes = await self.matching.check_products_availability(actor, targets)
        now_utc = datetime.now(UTC)
        if self._jobs.get(job_id) is job:
            outcome_by_key = {
                (
                    outcome.target.need_id,
                    outcome.target.provider,
                    outcome.target.product_id,
                ): outcome
                for outcome in outcomes
            }
            for need in job.value.needs:
                updated_need = False
                for idx, candidate in enumerate(need.candidates):
                    outcome = outcome_by_key.get(
                        (need.need_id, candidate.provider, candidate.product_id)
                    )
                    if outcome is None or outcome.fresh_product is None:
                        continue
                    fresh = outcome.fresh_product
                    need.candidates[idx] = candidate.model_copy(
                        update={
                            "available": fresh.available,
                            "price": fresh.price,
                            "unit_price": fresh.unit_price,
                            "unit_name": fresh.unit_name,
                        }
                    )
                    updated_need = True
                if updated_need:
                    need.observed_at = now_utc
            job.observed_at = now_utc
            job.value.observed_at = now_utc
        return self._to_availability_result(outcomes, now_utc)

    async def refresh_targets_availability(
        self,
        actor: str,
        targets: list[AvailabilityTarget],
    ) -> BasketAvailabilityRefreshResult:
        """Refresh live provider availability for arbitrary selected basket targets."""
        self._actor(actor)
        outcomes = await self.matching.check_products_availability(actor, targets)
        return self._to_availability_result(outcomes, datetime.now(UTC))

    def _finalize_job_if_idle(self, job: _Job, *, finished_task: asyncio.Task | None) -> None:
        main_running = (
            job.task is not None and job.task is not finished_task and not job.task.done()
        )
        need_running = any(
            task is not finished_task and not task.done() for task in job.need_tasks.values()
        )
        if main_running or need_running:
            return
        if not job.main_failed:
            job.observed_at = datetime.now(UTC)
            job.value.observed_at = job.observed_at
            job.value.status = "complete"
        job.finished = self.clock()

    async def _run(
        self,
        job: _Job,
        plan: PurchasePlan,
        page: int,
        size: int,
        refresh: bool,
        query_overrides: dict[UUID, str] | None = None,
    ):
        bounded_lines = plan.purchase_lines[:MAX_NEEDS]
        initial_revisions = {line.id: job.need_revisions.get(line.id, 0) for line in bounded_lines}

        async def current() -> bool:
            scope = await run_in_threadpool(self.scope_provider, job.actor)
            return scope is not None and scope == job.scope

        async def progress(need: NeedMatchResult) -> None:
            expected_rev = initial_revisions.get(need.need_id, 0)
            if job.need_revisions.get(need.need_id, 0) != expected_rev:
                return
            if await current():
                if job.need_revisions.get(need.need_id, 0) != expected_rev:
                    return
                for index, existing in enumerate(job.value.needs):
                    if existing.need_id == need.need_id:
                        job.value.needs[index] = need.model_copy(
                            update={"revision": expected_rev},
                            deep=True,
                        )
                        break

        try:
            job.value.status = "running"
            if not await current():
                raise RuntimeError("Provider context changed")
            kwargs: dict = {
                "page": page,
                "size": size,
                "force_refresh": refresh,
                "progress": progress,
                "plan": plan,
            }
            if query_overrides and self._supports_query_overrides():
                kwargs["query_overrides"] = query_overrides
            result = await self.matching.find_products(
                job.actor,
                plan.start,
                plan.end,
                **kwargs,
            )
            if not await current():
                raise RuntimeError("Provider context changed")
            now_utc = datetime.now(UTC)
            by_need_id = {need.need_id: need for need in result.needs}
            for index, existing in enumerate(job.value.needs):
                expected_rev = initial_revisions.get(existing.need_id, 0)
                if job.need_revisions.get(existing.need_id, 0) != expected_rev:
                    continue
                updated = by_need_id.get(existing.need_id)
                if updated is not None:
                    job.value.needs[index] = updated.model_copy(
                        update={
                            "revision": expected_rev,
                            "observed_at": updated.observed_at or now_utc,
                        },
                        deep=True,
                    )
            job.value.skipped_need_count = result.skipped_need_count
        except asyncio.CancelledError:
            raise
        except Exception:
            job.main_failed = True
            job.value.status = "failed"
            job.value.detail = SAFE_ERROR
            for need in job.value.needs:
                expected_rev = initial_revisions.get(need.need_id, 0)
                if (
                    need.status in {"pending", "searching"}
                    and job.need_revisions.get(need.need_id, 0) == expected_rev
                ):
                    need.status = "search_failed"
                    need.detail = SAFE_ERROR
        finally:
            self._finalize_job_if_idle(job, finished_task=asyncio.current_task())

    async def _run_need(
        self,
        job: _Job,
        line: PurchasePlanLine,
        start: date,
        end: date,
        effective_query: str,
        page: int,
        size: int,
        refresh: bool,
        revision: int,
    ) -> None:
        async def current() -> bool:
            scope = await run_in_threadpool(self.scope_provider, job.actor)
            return scope is not None and scope == job.scope

        async def progress(need: NeedMatchResult) -> None:
            if job.need_revisions.get(line.id) != revision:
                return
            if not await current():
                return
            if job.need_revisions.get(line.id) != revision:
                return
            for index, existing in enumerate(job.value.needs):
                if existing.need_id == line.id:
                    job.value.needs[index] = need.model_copy(
                        update={
                            "revision": revision,
                            "search_query": need.search_query or effective_query,
                        },
                        deep=True,
                    )
                    break

        sub_plan = PurchasePlan(start=start, end=end, suggestions=[], lines=[line])
        current_task = asyncio.current_task()
        try:
            if job.need_revisions.get(line.id) != revision:
                return
            if not await current():
                raise RuntimeError("Provider context changed")
            kwargs: dict = {
                "page": page,
                "size": size,
                "force_refresh": refresh,
                "progress": progress,
                "plan": sub_plan,
            }
            if self._supports_query_overrides():
                kwargs["query_overrides"] = {line.id: effective_query}
            result = await self.matching.find_products(
                job.actor,
                start,
                end,
                **kwargs,
            )
            if job.need_revisions.get(line.id) != revision:
                return
            if not await current():
                raise RuntimeError("Provider context changed")
            now_utc = datetime.now(UTC)
            updated = next((item for item in result.needs if item.need_id == line.id), None)
            if updated is not None:
                for index, existing in enumerate(job.value.needs):
                    if existing.need_id == line.id:
                        job.value.needs[index] = updated.model_copy(
                            update={
                                "revision": revision,
                                "search_query": updated.search_query or effective_query,
                                "observed_at": updated.observed_at or now_utc,
                            },
                            deep=True,
                        )
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            if job.need_revisions.get(line.id) == revision:
                for index, existing in enumerate(job.value.needs):
                    if existing.need_id == line.id:
                        job.value.needs[index] = existing.model_copy(
                            update={
                                "status": "search_failed",
                                "detail": SAFE_ERROR,
                                "revision": revision,
                                "search_query": effective_query,
                            }
                        )
                        break
        finally:
            if job.need_tasks.get(line.id) is current_task:
                job.need_tasks.pop(line.id, None)
            self._finalize_job_if_idle(job, finished_task=current_task)

    def health_snapshot(self, actor: str) -> BackgroundMatchingHealth:
        """Return actor-scoped freshness and failure visibility for background matching."""
        self._actor(actor)
        now_mono = self.clock()
        actor_jobs = [job for job in self._jobs.values() if job.actor == actor]
        active = sum(1 for job in actor_jobs if job.value.status in {"queued", "running"})
        completed = [job for job in actor_jobs if job.value.status == "complete"]
        failed = [job for job in actor_jobs if job.value.status == "failed"]

        latest_complete = max(completed, key=lambda j: j.created) if completed else None
        latest_failed = max(failed, key=lambda j: j.created) if failed else None

        stale = False
        if latest_complete is not None and latest_complete.finished is not None:
            stale = (now_mono - latest_complete.finished) > self.ttl_seconds
        if (
            latest_failed is not None
            and latest_complete is not None
            and latest_failed.created > latest_complete.created
        ):
            stale = True

        if active > 0:
            status: Literal["idle", "running", "healthy", "stale", "failed"] = "running"
            note = "Bakgrunnssøk etter Oda-produkter pågår."
        elif latest_failed is not None and (
            latest_complete is None or latest_failed.created > latest_complete.created
        ):
            status = "failed"
            note = (
                "Siste bakgrunnssøk feilet; tidligere produkttreff er ikke bekreftet som gjeldende. "
                "Oppdater produktsøk for ferske treff."
            )
        elif latest_complete is not None and stale:
            status = "stale"
            note = (
                "Tidligere produkttreff er utdaterte og gjenspeiler ikke nødvendigvis "
                "gjeldende tilgjengelighet hos Oda. Oppdater søket før gjennomgang."
            )
        elif latest_complete is not None:
            status = "healthy"
            note = None
        else:
            status = "idle"
            note = None

        return BackgroundMatchingHealth(
            status=status,
            active_jobs=active,
            last_completed_at=latest_complete.observed_at if latest_complete else None,
            last_failed_at=latest_failed.observed_at if latest_failed else None,
            last_error=(
                latest_failed.value.detail or SAFE_ERROR
                if latest_failed is not None
                and (latest_complete is None or latest_failed.created > latest_complete.created)
                else None
            ),
            stale=stale,
            freshness_note=note,
        )

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
