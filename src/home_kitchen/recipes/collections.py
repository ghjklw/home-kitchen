"""Bounded, actor-scoped collection previews and explicitly reviewed bulk imports."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Table, Text, Uuid, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert

from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.imports import ImportedDraft, import_recipe, validate_url
from home_kitchen.models import Ingredient, PurchaseEstimate, Recipe, RecipeSource, SourceIngredient
from home_kitchen.recipes.source_quantities import source_ingredient_parts
from home_kitchen.service import KitchenService
from home_kitchen.store import entity_values, metadata, recipes

_LOG = logging.getLogger(__name__)
CollectionKind = Literal["liked", "purchased"]
LABELS = {"liked": "Liked on Oda", "purchased": "Ingredients bought on Oda"}
source_links = Table(
    "recipe_source_links",
    metadata,
    Column("provider", Text, primary_key=True),
    Column("external_recipe_id", Text, primary_key=True),
    Column("recipe_id", Uuid, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False),
    Column("source_url", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class CollectionEntry(BaseModel):
    id: str
    provider: str
    title: str
    source_url: str | None = None
    image_url: str | None = None
    badges: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    status: Literal[
        "pending", "importing", "imported", "already_imported", "failed", "missing_url"
    ] = "pending"
    recipe_id: UUID | None = None
    error: str | None = None


class CollectionPage(BaseModel):
    entries: list[CollectionEntry]
    has_more: bool


class CollectionProvider(Protocol):
    id: str

    def generation(self, actor_id: str) -> object: ...

    async def page(
        self, actor_id: str, kind: CollectionKind, page: int, size: int
    ) -> CollectionPage: ...

    def validate_source(self, entry: CollectionEntry) -> None: ...

    def lock_generation(self, connection, actor_id: str, generation: object) -> None: ...


class OdaCollectionProvider:
    id = "oda"

    def __init__(self, oda):
        self.oda = oda

    def generation(self, actor_id: str) -> object:
        generation = self.oda.search_cache_context(actor_id)
        if generation is None or not self.oda.status(actor_id).get("connected"):
            raise ValueError("Connect Oda before reading recipe collections")
        return generation

    async def page(self, actor_id, kind, page, size):
        method = self.oda.list_liked_recipes if kind == "liked" else self.oda.list_purchased_recipes
        payload = await method(actor_id, page=page, size=size)
        return CollectionPage(
            has_more=payload["hasMore"],
            entries=[
                CollectionEntry(
                    id=str(item["id"]),
                    provider=self.id,
                    title=item["title"],
                    source_url=item.get("url"),
                    image_url=item.get("imageUrl"),
                    badges=[LABELS[kind]],
                    metadata={
                        key: item[key] for key in ("providerName", "deliveriesCount") if key in item
                    },
                )
                for item in payload["recipes"]
            ],
        )

    def lock_generation(self, connection, actor_id, generation):
        # Hold the lifecycle row until recipe/map/history commit. A disconnect or
        # reconnect cannot pass its generation bump between this check and save.
        from home_kitchen.oda.repository import connections, lifecycles

        accepted = connection.execute(
            lifecycles.update()
            .where(
                lifecycles.c.actor_id == actor_id,
                lifecycles.c.generation == generation,
            )
            .values(generation=generation)
            .returning(lifecycles.c.actor_id)
        ).scalar_one_or_none()
        connected = connection.execute(
            select(connections.c.actor_id).where(connections.c.actor_id == actor_id)
        ).scalar_one_or_none()
        if accepted is None or connected is None:
            raise ValueError("Oda connection changed; preview the collections again")

    def validate_source(self, entry):
        if entry.source_url is None:
            raise ValueError("This Oda recipe has no public source URL")
        validate_url(entry.source_url)
        parsed = urlparse(entry.source_url)
        match = re.fullmatch(r"/no/recipes/([1-9][0-9]*)-[a-z0-9]+(?:-[a-z0-9]+)*/?", parsed.path)
        if parsed.hostname not in {"oda.com", "www.oda.com"} or not match or match[1] != entry.id:
            raise ValueError("Oda source URL does not match the listed recipe identity")


class CollectionJob(BaseModel):
    job_id: UUID
    status: Literal["previewing", "ready", "importing", "completed", "cancelled", "failed"]
    kinds: list[CollectionKind]
    entries: list[CollectionEntry] = Field(default_factory=list)
    pages_read: int = 0
    processed: int = 0
    total: int = 0
    truncated: bool = False
    truncated_kinds: list[CollectionKind] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class _Job:
    def __init__(self, actor, generation, view):
        self.actor = actor
        self.generation = generation
        self.view = view
        self.created = time.monotonic()
        self.task: asyncio.Task | None = None
        self.cancelled = False


def source_ingredient(raw: str, index: int) -> Ingredient:
    """Preserve the cooking measure and attach only published estimates."""
    name, parsed = source_ingredient_parts(raw)
    estimate = (
        PurchaseEstimate.model_validate(parsed.purchase_estimate.__dict__)
        if parsed.purchase_estimate is not None
        else None
    )
    return Ingredient(
        name=name,
        quantity=parsed.quantity,
        unit=parsed.unit,
        source_indices=[index],
        purchase_estimate=estimate,
    )


class CollectionService:
    MAX_JOBS = 16
    TTL_SECONDS = 30 * 60
    MAX_PAGES = 50
    PAGE_SIZE = 20

    def __init__(
        self,
        kitchen: KitchenService,
        history: HistoryRepository,
        provider: CollectionProvider,
        extractor: Callable[[str], Awaitable[ImportedDraft]] = import_recipe,
    ):
        self.kitchen, self.history, self.provider, self.extractor = (
            kitchen,
            history,
            provider,
            extractor,
        )
        self._jobs: dict[UUID, _Job] = {}
        self._job_reservations = 0
        self._semaphore = asyncio.Semaphore(2)
        self._closed = False

    def _prune(self):
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if now - job.created >= self.TTL_SECONDS:
                job.cancelled = True
                if job.task and not job.task.done():
                    job.task.cancel()
                del self._jobs[key]

    def _job(self, actor_id, job_id) -> _Job:
        self._prune()
        try:
            job = self._jobs[UUID(str(job_id))]
        except (KeyError, ValueError, TypeError):
            raise KeyError(
                "Collection job unavailable or expired; preview the collections again. Saved recipes are retained."
            ) from None
        if job.actor != actor_id:
            raise KeyError("Collection job unavailable; preview your own collections again.")
        return job

    def _fence(self, job):
        if job.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() - job.created >= self.TTL_SECONDS:
            raise ValueError("Collection preview expired; preview again")
        if self.provider.generation(job.actor) != job.generation:
            raise ValueError("Oda connection changed; preview the collections again")

    async def start_preview(self, actor_id: str, kinds: list[CollectionKind]) -> CollectionJob:
        self._prune()
        if self._closed:
            raise ValueError("Collection service is closed")
        if not kinds or any(kind not in LABELS for kind in kinds):
            raise ValueError("Select liked and/or purchased collections")
        if len(self._jobs) + self._job_reservations >= self.MAX_JOBS:
            raise ValueError(
                "Too many collection jobs; cancel or wait for an existing job to expire"
            )
        view = CollectionJob(job_id=uuid4(), status="previewing", kinds=list(dict.fromkeys(kinds)))
        self._job_reservations += 1
        try:
            generation = await asyncio.to_thread(self.provider.generation, actor_id)
        finally:
            self._job_reservations -= 1
        self._prune()
        if self._closed:
            raise ValueError("Collection service is closed")
        if len(self._jobs) >= self.MAX_JOBS:
            raise ValueError(
                "Too many collection jobs; cancel or wait for an existing job to expire"
            )
        job = _Job(actor_id, generation, view)
        self._jobs[view.job_id] = job
        job.task = asyncio.create_task(self._preview(job))
        return view.model_copy(deep=True)

    async def get(self, actor_id: str, job_id: UUID | str) -> CollectionJob:
        job = self._job(actor_id, job_id)
        if not job.cancelled:
            await asyncio.to_thread(self._fence, job)
        return job.view.model_copy(deep=True)

    async def _preview(self, job):
        try:
            async with self._semaphore:
                seen = {}
                for kind in job.view.kinds:
                    try:
                        for number in range(1, self.MAX_PAGES + 1):
                            await asyncio.to_thread(self._fence, job)
                            page = await self.provider.page(job.actor, kind, number, self.PAGE_SIZE)
                            await asyncio.to_thread(self._fence, job)
                            job.view.pages_read += 1
                            for entry in page.entries[: self.PAGE_SIZE]:
                                if entry.provider != self.provider.id:
                                    raise ValueError("Collection provider identity changed")
                                if entry.id in seen:
                                    existing = seen[entry.id]
                                    existing.badges = list(
                                        dict.fromkeys([*existing.badges, *entry.badges])
                                    )
                                    existing.metadata.update(entry.metadata)
                                    if not existing.source_url and entry.source_url:
                                        existing.source_url = entry.source_url
                                        await asyncio.to_thread(self._inspect_entry, existing)
                                    continue
                                seen[entry.id] = entry
                                await asyncio.to_thread(self._inspect_entry, entry)
                                job.view.entries.append(entry)
                            if not page.has_more:
                                break
                            if number == self.MAX_PAGES:
                                job.view.truncated = True
                                job.view.truncated_kinds.append(kind)
                    except Exception as error:
                        _LOG.warning("Could not finish collection %s: %s", kind, error)
                        await asyncio.to_thread(self._fence, job)
                        if (
                            getattr(error, "status_code", None) == 409
                            or "expired" in str(error).lower()
                            or "connect" in str(error).lower()
                        ):
                            job.view.errors.append(
                                getattr(error, "detail", str(error))
                                or f"Could not finish {LABELS[kind]}; preview again to retry."
                            )
                            break
                        job.view.errors.append(
                            f"Could not finish {LABELS[kind]}; preview again to retry."
                        )
                job.view.total = len(job.view.entries)
                job.view.status = "ready" if job.view.entries or not job.view.errors else "failed"
        except asyncio.CancelledError:
            job.view.status = "cancelled"
        except Exception:
            job.view.status = "failed"
            job.view.errors.append(
                "Collection connection changed or became unavailable; preview again."
            )

    def _inspect_entry(self, entry):
        entry.recipe_id = self._existing(entry)
        if entry.recipe_id:
            entry.status = "already_imported"
        elif not entry.source_url:
            entry.status = "missing_url"
        else:
            try:
                self.provider.validate_source(entry)
                entry.status, entry.error = "pending", None
            except ValueError as error:
                entry.status, entry.error = "failed", str(error)

    def _existing(self, entry, connection=None):
        def lookup(conn):
            linked = conn.execute(
                select(source_links.c.recipe_id).where(
                    source_links.c.provider == entry.provider,
                    source_links.c.external_recipe_id == entry.id,
                )
            ).scalar_one_or_none()
            if linked is not None or entry.source_url is None:
                return linked
            return conn.execute(
                select(recipes.c.id)
                .where(recipes.c.source_url == entry.source_url)
                .order_by(recipes.c.position)
                .limit(1)
            ).scalar_one_or_none()

        if connection is not None:
            return lookup(connection)
        with self.kitchen.store.engine.connect() as owned:
            return lookup(owned)

    async def start_import(
        self, actor_id, job_id, selected_ids: list[str], reviewed: bool
    ) -> CollectionJob:
        job = self._job(actor_id, job_id)
        await asyncio.to_thread(self._fence, job)
        if self._closed or reviewed is not True:
            raise ValueError("Review and explicitly confirm the selected recipes before import")
        if job.view.status not in {"ready", "completed"}:
            raise ValueError("Wait for the preview or import to finish")
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise ValueError("Select an explicit nonempty list of unique recipe IDs")
        by_id = {entry.id: entry for entry in job.view.entries}
        if any(key not in by_id for key in selected_ids):
            raise ValueError("Selection must contain exact IDs from this preview")
        selected = [by_id[key] for key in selected_ids]
        job.view.status, job.view.processed, job.view.total = "importing", 0, len(selected)
        job.task = asyncio.create_task(self._import(job, selected))
        return job.view.model_copy(deep=True)

    def _save(self, job, entry, draft):
        self._fence(job)
        # The public URL is the exact reviewed provider URL, even if an extractor redirects.
        if draft is not None and str(draft.source_url) != entry.source_url:
            raise ValueError("Extracted source URL changed; preview the source again")
        recipe = (
            None
            if draft is None
            else Recipe(
                id=uuid4(),
                title=draft.title,
                servings=draft.servings,
                ingredients=[
                    source_ingredient(raw, index) for index, raw in enumerate(draft.ingredients)
                ],
                instructions=draft.instructions,
                source=RecipeSource.IMPORTED,
                source_url=entry.source_url,
                image_url=draft.image_url,
                source_ingredients=[
                    SourceIngredient(index=index, text=raw)
                    for index, raw in enumerate(draft.ingredients)
                ],
            )
        )
        with self.kitchen.store.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"recipe-source:{entry.provider}:{entry.id}"},
            )
            self._fence(job)
            self.provider.lock_generation(connection, job.actor, job.generation)
            existing = self._existing(entry, connection)
            if existing is None:
                if recipe is None:
                    raise ValueError("Previously saved recipe was removed; preview again")
                connection.execute(insert(recipes).values(**entity_values(recipe)))
                self.history.record(
                    HistoryEntryCreate(
                        kind="domain_event",
                        message="Imported source recipe",
                        recipe_id=recipe.id,
                        structured={
                            "provider": entry.provider,
                            "external_recipe_id": entry.id,
                            "source_url": entry.source_url,
                            "badges": entry.badges,
                        },
                    ),
                    actor_id=job.actor,
                    connection=connection,
                )
            if existing is not None:
                recipe_id = existing
            else:
                assert recipe is not None
                recipe_id = recipe.id
            connection.execute(
                insert(source_links)
                .values(
                    provider=entry.provider,
                    external_recipe_id=entry.id,
                    recipe_id=recipe_id,
                    source_url=entry.source_url,
                    metadata={"badges": entry.badges, **entry.metadata},
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(
                    index_elements=[source_links.c.provider, source_links.c.external_recipe_id]
                )
            )
        return recipe_id, existing is not None

    async def _save_async(self, job, entry, draft):
        task = asyncio.create_task(asyncio.to_thread(self._save, job, entry, draft))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _import(self, job, selected):
        try:
            async with self._semaphore:
                for entry in selected:
                    await asyncio.to_thread(self._fence, job)
                    if entry.status == "imported" or not entry.source_url:
                        job.view.processed += 1
                        continue
                    entry.status, entry.error = "importing", None
                    try:
                        self.provider.validate_source(entry)
                        existing = await asyncio.to_thread(self._existing, entry)
                        if existing:
                            entry.recipe_id, _ = await self._save_async(job, entry, None)
                            entry.status = "already_imported"
                        else:
                            draft = await self.extractor(entry.source_url)
                            await asyncio.to_thread(self._fence, job)
                            entry.recipe_id, existed = await self._save_async(job, entry, draft)
                            entry.status = "already_imported" if existed else "imported"
                    except asyncio.CancelledError:
                        entry.status = "pending"
                        raise
                    except (ValueError, KeyError) as error:
                        if "connection changed" in str(error) or "expired" in str(error):
                            entry.status = "pending"
                            raise
                        entry.status, entry.error = (
                            "failed",
                            "Could not import this source. Review the source URL and explicitly retry.",
                        )
                    except Exception:
                        entry.status, entry.error = (
                            "failed",
                            "Could not import this source. Review the source URL and explicitly retry.",
                        )
                    job.view.processed += 1
                job.view.status = "completed"
        except asyncio.CancelledError:
            job.view.status = "cancelled"
        except Exception:
            job.view.status = "failed"
            job.view.errors.append(
                "Collection connection changed; preview again. Completed imports are retained."
            )

    async def cancel(self, actor_id, job_id):
        job = self._job(actor_id, job_id)
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        job.view.status = "cancelled"
        return job.view.model_copy(deep=True)

    async def close(self):
        self._closed = True
        tasks = []
        for job in self._jobs.values():
            job.cancelled = True
            if job.task and not job.task.done():
                job.task.cancel()
                tasks.append(job.task)
        await asyncio.gather(*tasks, return_exceptions=True)
