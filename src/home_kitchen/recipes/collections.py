"""Bounded, actor-scoped collection previews and explicitly reviewed bulk imports."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from home_kitchen.models import (
    GuidedCookingStep,
    Ingredient,
    PrepCategory,
    PurchaseEstimate,
    Recipe,
    RecipeSource,
    RecipeType,
    SourceIngredient,
    infer_recipe_metadata,
)
from home_kitchen.recipes.ports import (
    CollectionUnitOfWork,
    ExtractedRecipeDraft,
    PostImportIngredientMapper,
    RecipeExtractor,
    RecipeIdentityResolver,
)
from home_kitchen.recipes.source_quantities import source_ingredient_parts

_LOG = logging.getLogger(__name__)
CollectionKind = str
LABELS: dict[str, str] = {
    "liked": "Liked on Oda",
    "purchased": "Ingredients bought on Oda",
    "saved": "Lagret i Cookidoo",
    "created": "Opprettet i Cookidoo",
    "bookmarks": "Bokmerker i Cookidoo",
}
PROVIDER_KINDS: dict[str, set[str]] = {
    "oda": {"liked", "purchased"},
    "cookidoo": {"saved", "created", "bookmarks"},
}


class _InlineRecipeDraft(BaseModel):
    title: str
    servings: int
    ingredients: list[str]
    instructions: list[str]
    source_url: str
    image_url: str | None = None
    recipe_type: RecipeType | None = None
    prep_time_minutes: int | None = None
    prep_category: PrepCategory | None = None
    guided_steps: list[GuidedCookingStep] = Field(default_factory=list)


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
    servings: int | None = None
    ingredients: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    recipe_type: RecipeType | None = None
    prep_time_minutes: int | None = None
    prep_category: PrepCategory | None = None
    guided_steps: list[GuidedCookingStep] = Field(default_factory=list)


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

    def lock_generation(self, connection: Any, actor_id: str, generation: object) -> None: ...


class CollectionJob(BaseModel):
    job_id: UUID
    provider: str = "oda"
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
    def __init__(
        self,
        actor: str,
        generation: object,
        view: CollectionJob,
        provider: CollectionProvider,
    ):
        self.actor = actor
        self.generation = generation
        self.view = view
        self.provider = provider
        self.created = time.monotonic()
        self.task: asyncio.Task[None] | None = None
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
        uow: CollectionUnitOfWork,
        provider: CollectionProvider | None = None,
        extractor: RecipeExtractor | Callable[[str], Awaitable[ExtractedRecipeDraft]] | None = None,
        *,
        providers: dict[str, CollectionProvider] | None = None,
        identity_resolver: RecipeIdentityResolver | None = None,
        post_import_mapper: PostImportIngredientMapper | None = None,
        default_extractor: RecipeExtractor
        | Callable[[str], Awaitable[ExtractedRecipeDraft]]
        | None = None,
    ):
        self.uow = uow
        self.history = getattr(uow, "history", None)
        self.extractor = extractor
        self._default_extractor = default_extractor
        self.identity_resolver = identity_resolver
        self.post_import_mapper = post_import_mapper
        self._jobs: dict[UUID, _Job] = {}
        self._job_reservations = 0
        self._semaphore = asyncio.Semaphore(2)
        self._closed = False

        if providers is not None:
            self.providers = dict(providers)
        elif provider is not None:
            if isinstance(provider, dict):
                self.providers = dict(provider)
            else:
                self.providers = {provider.id: provider}
        else:
            self.providers = {}

    @property
    def provider(self) -> CollectionProvider:
        return self.providers.get("oda") or next(iter(self.providers.values()))

    @provider.setter
    def provider(self, value: CollectionProvider) -> None:
        self.providers[value.id] = value

    def _provider_for(self, provider_id: str) -> CollectionProvider:
        if provider_id not in self.providers:
            raise ValueError(f"Ukjent leverandør: {provider_id}")
        return self.providers[provider_id]

    def _prune(self) -> None:
        now = time.monotonic()
        for key, job in list(self._jobs.items()):
            if now - job.created >= self.TTL_SECONDS:
                job.cancelled = True
                if job.task and not job.task.done():
                    job.task.cancel()
                del self._jobs[key]

    def _job(self, actor_id: str, job_id: UUID | str) -> _Job:
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

    def _fence(self, job: Any) -> None:
        if job.cancelled:
            raise asyncio.CancelledError
        if time.monotonic() - job.created >= self.TTL_SECONDS:
            raise ValueError("Collection preview expired; preview again")
        prov = getattr(job, "provider", None) or self.provider
        if prov.generation(job.actor) != job.generation:
            label = "Cookidoo" if prov.id == "cookidoo" else "Oda"
            raise ValueError(f"{label} connection changed; preview the collections again")

    async def start_preview(
        self,
        actor_id: str,
        kinds: list[CollectionKind],
        provider_id: str = "oda",
    ) -> CollectionJob:
        self._prune()
        if self._closed:
            raise ValueError("Collection service is closed")
        provider = self._provider_for(provider_id)
        if not kinds:
            kinds = ["saved"] if provider.id == "cookidoo" else ["liked"]
        supported_kinds = PROVIDER_KINDS.get(provider.id, set(LABELS))
        if any(kind not in supported_kinds for kind in kinds):
            raise ValueError("Select supported collection kinds")
        if len(self._jobs) + self._job_reservations >= self.MAX_JOBS:
            raise ValueError(
                "Too many collection jobs; cancel or wait for an existing job to expire"
            )
        view = CollectionJob(
            job_id=uuid4(),
            provider=provider.id,
            status="previewing",
            kinds=list(dict.fromkeys(kinds)),
        )
        self._job_reservations += 1
        try:
            generation = await asyncio.to_thread(provider.generation, actor_id)
        finally:
            self._job_reservations -= 1
        self._prune()
        if self._closed:
            raise ValueError("Collection service is closed")
        if len(self._jobs) >= self.MAX_JOBS:
            raise ValueError(
                "Too many collection jobs; cancel or wait for an existing job to expire"
            )
        job = _Job(actor_id, generation, view, provider)
        self._jobs[view.job_id] = job
        job.task = asyncio.create_task(self._preview(job))
        return view.model_copy(deep=True)

    async def get(self, actor_id: str, job_id: UUID | str) -> CollectionJob:
        job = self._job(actor_id, job_id)
        if not job.cancelled:
            await asyncio.to_thread(self._fence, job)
        return job.view.model_copy(deep=True)

    async def _preview(self, job: _Job) -> None:
        try:
            async with self._semaphore:
                prov = getattr(job, "provider", None) or self.provider
                seen: dict[str, CollectionEntry] = {}
                for kind in job.view.kinds:
                    try:
                        for number in range(1, self.MAX_PAGES + 1):
                            await asyncio.to_thread(self._fence, job)
                            page = await prov.page(job.actor, kind, number, self.PAGE_SIZE)
                            await asyncio.to_thread(self._fence, job)
                            job.view.pages_read += 1
                            for entry in page.entries[: self.PAGE_SIZE]:
                                if entry.provider != prov.id:
                                    raise ValueError("Collection provider identity changed")
                                if entry.id in seen:
                                    existing = seen[entry.id]
                                    existing.badges = list(
                                        dict.fromkeys([*existing.badges, *entry.badges])
                                    )
                                    existing.metadata.update(entry.metadata)
                                    if existing.image_url is None and entry.image_url is not None:
                                        existing.image_url = entry.image_url
                                    if (
                                        existing.recipe_type is None
                                        and entry.recipe_type is not None
                                    ):
                                        existing.recipe_type = entry.recipe_type
                                    if (
                                        existing.prep_time_minutes is None
                                        and entry.prep_time_minutes is not None
                                    ):
                                        existing.prep_time_minutes = entry.prep_time_minutes
                                    if (
                                        existing.prep_category is None
                                        and entry.prep_category is not None
                                    ):
                                        existing.prep_category = entry.prep_category
                                    if not existing.source_url and entry.source_url:
                                        existing.source_url = entry.source_url
                                        await asyncio.to_thread(self._inspect_entry, existing, prov)
                                    continue
                                seen[entry.id] = entry
                                await asyncio.to_thread(self._inspect_entry, entry, prov)
                                job.view.entries.append(entry)
                            if not page.has_more:
                                break
                            if number == self.MAX_PAGES:
                                job.view.truncated = True
                                job.view.truncated_kinds.append(kind)
                    except Exception as error:
                        _LOG.warning("Could not finish collection %s: %s", kind, error)
                        await asyncio.to_thread(self._fence, job)
                        detail = getattr(error, "detail", str(error))
                        detail_lower = str(detail).lower()
                        if (
                            getattr(error, "status_code", None) in (401, 409, 502, 503)
                            or "expired" in detail_lower
                            or "connect" in detail_lower
                            or "utløpt" in detail_lower
                            or "logg inn" in detail_lower
                        ):
                            job.view.errors.append(
                                str(detail)
                                or f"Could not finish {LABELS.get(kind, kind)}; preview again to retry."
                            )
                            break
                        job.view.errors.append(
                            f"Could not finish {LABELS.get(kind, kind)}; preview again to retry."
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

    def _inspect_entry(
        self, entry: CollectionEntry, provider: CollectionProvider | None = None
    ) -> None:
        prov = provider or self._provider_for(entry.provider)
        entry.recipe_id = self._existing(entry)
        if entry.recipe_id:
            entry.status = "already_imported"
        elif not entry.source_url:
            entry.status = "missing_url"
        else:
            try:
                prov.validate_source(entry)
                entry.status, entry.error = "pending", None
            except ValueError as error:
                entry.status, entry.error = "failed", str(error)

    def _existing(self, entry: CollectionEntry) -> UUID | None:
        return self.uow.find_existing_recipe_id(entry.provider, entry.id, entry.source_url)

    async def start_import(
        self,
        actor_id: str,
        job_id: UUID | str,
        selected_ids: list[str],
        reviewed: bool,
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

    def _save(
        self, job: Any, entry: CollectionEntry, draft: ExtractedRecipeDraft | None
    ) -> tuple[UUID, bool]:
        self._fence(job)
        prov = getattr(job, "provider", None) or self._provider_for(entry.provider)
        # The public URL is the exact reviewed provider URL, even if an extractor redirects.
        if draft is not None and str(draft.source_url) != entry.source_url:
            raise ValueError("Extracted source URL changed; preview the source again")
        source = RecipeSource.COOKIDOO if entry.provider == "cookidoo" else RecipeSource.IMPORTED
        if draft is None:
            recipe = None
        else:
            raw_type = draft.recipe_type or entry.recipe_type
            raw_minutes = (
                draft.prep_time_minutes
                if draft.prep_time_minutes is not None
                else entry.prep_time_minutes
            )
            raw_prep_category = draft.prep_category or entry.prep_category
            inferred_type, inferred_minutes, inferred_prep_category = infer_recipe_metadata(
                title=draft.title,
                ingredients=draft.ingredients,
                categories=entry.badges,
                prep_time_minutes=raw_minutes,
                servings=draft.servings,
                recipe_type=raw_type,
                prep_category=raw_prep_category,
            )
            recipe = Recipe(
                id=uuid4(),
                title=draft.title,
                servings=draft.servings,
                ingredients=[
                    source_ingredient(raw, index) for index, raw in enumerate(draft.ingredients)
                ],
                instructions=draft.instructions,
                source=source,
                source_url=entry.source_url,
                image_url=draft.image_url or entry.image_url,
                source_ingredients=[
                    SourceIngredient(index=index, text=raw)
                    for index, raw in enumerate(draft.ingredients)
                ],
                recipe_type=inferred_type,
                prep_time_minutes=inferred_minutes,
                prep_category=inferred_prep_category,
                guided_steps=list(draft.guided_steps or entry.guided_steps or []),
            )
            if self.identity_resolver is not None:
                try:
                    recipe = self.identity_resolver.resolve_recipe_identities(recipe)
                except Exception:
                    _LOG.warning(
                        "Fast alias resolution failed during collection import",
                        exc_info=True,
                    )
        with self.uow.session() as session:
            session.acquire_source_lock(entry.provider, entry.id)
            self._fence(job)
            session.lock_provider_generation(prov, job.actor, job.generation)
            existing = session.find_existing_recipe_id(entry.provider, entry.id, entry.source_url)
            if existing is None:
                if recipe is None:
                    raise ValueError("Previously saved recipe was removed; preview again")
                session.save_recipe(recipe)
                history_msg = (
                    "Importerte Cookidoo-oppskrift"
                    if entry.provider == "cookidoo"
                    else "Imported source recipe"
                )
                session.record_import_history(
                    actor_id=job.actor,
                    recipe_id=recipe.id,
                    provider=entry.provider,
                    external_recipe_id=entry.id,
                    source_url=entry.source_url,
                    badges=entry.badges,
                    message=history_msg,
                )
                recipe_id = recipe.id
            else:
                recipe_id = existing
            session.link_source(
                provider=entry.provider,
                external_recipe_id=entry.id,
                recipe_id=recipe_id,
                source_url=entry.source_url,
                metadata={"badges": entry.badges, **entry.metadata},
            )
        return recipe_id, existing is not None

    async def _save_async(
        self, job: Any, entry: CollectionEntry, draft: ExtractedRecipeDraft | None
    ) -> tuple[UUID, bool]:
        task = asyncio.create_task(asyncio.to_thread(self._save, job, entry, draft))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _auto_map_imported_recipes(self, job: Any, recipe_ids: list[UUID]) -> None:
        if self.post_import_mapper is None:
            return
        try:
            await self.post_import_mapper.map_imported_recipes(job.actor, recipe_ids)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.warning(
                "Auto-mapping unmapped ingredients failed after collection import for %d recipes",
                len(recipe_ids),
                exc_info=True,
            )

    async def _import(self, job: Any, selected: list[CollectionEntry]) -> None:
        imported_recipe_ids: list[UUID] = []
        try:
            async with self._semaphore:
                prov = getattr(job, "provider", None) or self._provider_for(job.view.provider)
                for entry in selected:
                    await asyncio.to_thread(self._fence, job)
                    if entry.status == "imported" or not entry.source_url:
                        job.view.processed += 1
                        continue
                    entry.status, entry.error = "importing", None
                    try:
                        prov.validate_source(entry)
                        existing = await asyncio.to_thread(self._existing, entry)
                        if existing:
                            entry.recipe_id, _ = await self._save_async(job, entry, None)
                            entry.status = "already_imported"
                        else:
                            ingredients = entry.ingredients or entry.metadata.get("ingredients")
                            instructions = entry.instructions or entry.metadata.get("instructions")
                            draft: ExtractedRecipeDraft
                            if ingredients and instructions:
                                draft = _InlineRecipeDraft(
                                    title=entry.title,
                                    servings=entry.servings or entry.metadata.get("servings", 4),
                                    ingredients=ingredients,
                                    instructions=instructions,
                                    source_url=entry.source_url,
                                    image_url=entry.image_url,
                                    recipe_type=entry.recipe_type,
                                    prep_time_minutes=entry.prep_time_minutes,
                                    prep_category=entry.prep_category,
                                    guided_steps=list(entry.guided_steps or []),
                                )
                            else:
                                extract_entry = getattr(prov, "extract_entry", None)
                                if self.extractor is not None:
                                    draft = await self.extractor(entry.source_url)
                                elif extract_entry is not None:
                                    draft = await extract_entry(entry)
                                elif self._default_extractor is not None:
                                    draft = await self._default_extractor(entry.source_url)
                                else:
                                    raise ValueError("No recipe extractor configured")
                                model_copy = getattr(draft, "model_copy", None)
                                if (
                                    draft.image_url is None
                                    and entry.image_url is not None
                                    and callable(model_copy)
                                ):
                                    draft = cast(
                                        ExtractedRecipeDraft,
                                        model_copy(update={"image_url": entry.image_url}),
                                    )
                            await asyncio.to_thread(self._fence, job)
                            entry.recipe_id, existed = await self._save_async(job, entry, draft)
                            entry.status = "already_imported" if existed else "imported"
                            if not existed and entry.recipe_id is not None:
                                imported_recipe_ids.append(entry.recipe_id)
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
                if imported_recipe_ids and not job.cancelled:
                    await self._auto_map_imported_recipes(job, imported_recipe_ids)
                job.view.status = "completed"
        except asyncio.CancelledError:
            job.view.status = "cancelled"
        except Exception:
            job.view.status = "failed"
            job.view.errors.append(
                "Collection connection changed; preview again. Completed imports are retained."
            )

    async def cancel(self, actor_id: str, job_id: UUID | str) -> CollectionJob:
        job = self._job(actor_id, job_id)
        job.cancelled = True
        if job.task and not job.task.done():
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        job.view.status = "cancelled"
        return job.view.model_copy(deep=True)

    async def close(self) -> None:
        self._closed = True
        tasks = []
        for job in self._jobs.values():
            job.cancelled = True
            if job.task and not job.task.done():
                job.task.cancel()
                tasks.append(job.task)
        await asyncio.gather(*tasks, return_exceptions=True)


def __getattr__(name: str) -> Any:
    if name == "OdaCollectionProvider":
        from home_kitchen.recipes.adapters.oda import OdaCollectionProvider

        return OdaCollectionProvider
    if name == "source_links":
        from home_kitchen.recipes.adapters.postgres import source_links

        return source_links
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
