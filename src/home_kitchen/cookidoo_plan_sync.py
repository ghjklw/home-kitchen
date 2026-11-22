"""Cookidoo My Week <-> Kitchen meal plan preview and explicit confirm service."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from home_kitchen.connector_ports import ConnectorCredentialPort
from home_kitchen.cookidoo_recipes import (
    CookidooCollectionProvider,
    CookidooMyWeekEntry,
)
from home_kitchen.models import Meal, RecipeSource
from home_kitchen.recipes.collections import CollectionEntry
from home_kitchen.recipes.ports import ExtractedRecipeDraft, GenerationLockableProvider

SyncDirection = Literal["cookidoo_to_kitchen", "kitchen_to_cookidoo"]
_LOG = logging.getLogger(__name__)


class CookidooMyWeekProviderPort(GenerationLockableProvider, Protocol):
    """Port for reading and writing Cookidoo My Week entries with generation fencing."""

    def generation(self, actor_id: str) -> object: ...

    async def get_my_week_plan(
        self, start: date, end: date, *, resolve_custom_recipes: bool = True
    ) -> list[CookidooMyWeekEntry]: ...

    async def extract_entry(self, entry: CollectionEntry) -> ExtractedRecipeDraft: ...

    async def add_to_my_week(
        self,
        cookidoo_recipe_id: str,
        scheduled_date: date,
        *,
        servings: int | None = None,
        label: str | None = None,
        entry_id: str | None = None,
        read_before_write: bool = True,
        is_custom: bool = False,
        previous_recipe_id: str | None = None,
        previous_date: date | None = None,
        previous_is_custom: bool = False,
    ) -> CookidooMyWeekEntry: ...

    async def remove_from_my_week(
        self, recipe_id: str, planned_date: date, *, is_custom: bool = False
    ) -> None: ...


class CookidooRecipeImporterPort(Protocol):
    """Port for deduplicating and atomically importing Cookidoo recipes."""

    def check_duplicate(
        self, entry: CollectionEntry, session: Any = None
    ) -> tuple[UUID | None, bool]: ...

    def import_recipe_atomic(
        self, actor_id: str, entry: CollectionEntry, reviewed: bool
    ) -> tuple[UUID, bool]: ...


class CookidooPlanSyncSession(Protocol):
    """Transactional session for Cookidoo plan sync writes and history recording."""

    def lock_provider_generation(
        self, provider: GenerationLockableProvider, actor_id: str, generation: object
    ) -> None: ...

    def add_meal(self, meal: Meal) -> None: ...

    def get_meal(self, meal_id: UUID) -> Meal | None: ...

    def update_meal(self, meal: Meal) -> None: ...

    def delete_meal(self, meal_id: UUID) -> None: ...

    def find_sync_history(
        self, direction: str, external_entry_id: str
    ) -> dict[str, Any] | None: ...

    def record_sync_history(
        self,
        *,
        actor_id: str,
        direction: str,
        action: str,
        meal_id: UUID | None,
        recipe_id: UUID | None,
        cookidoo_recipe_id: str,
        source_url: str,
        scheduled_date: date,
        message: str,
        external_entry_id: str | None = None,
        include_external_entry_id: bool = False,
        sync_state: dict[str, Any] | None = None,
    ) -> None: ...


class CookidooPlanSyncUnitOfWork(Protocol):
    """Port for reading kitchen meal/recipe state and committing Cookidoo sync updates."""

    def load_kitchen_state(
        self, start: date, end: date
    ) -> tuple[
        list[Meal],
        dict[UUID, dict[str, Any]],
        dict[UUID, tuple[str, str]],
        dict[str, UUID],
    ]: ...

    def load_sync_history(self) -> list[dict[str, Any]]: ...

    def session(self) -> AbstractContextManager[CookidooPlanSyncSession]: ...


class SyncEditTarget(BaseModel):
    """Explicit mapping identifying an existing target meal/entry to update instead of adding."""

    source_key: str = Field(min_length=1, max_length=200)
    target_meal_id: UUID | None = None
    target_external_entry_id: str | None = Field(default=None, max_length=200)


class CookidooPlanSyncPreviewRequest(BaseModel):
    direction: SyncDirection
    start: date
    end: date
    meal_ids: list[UUID] | None = Field(default=None, max_length=100)
    edits: list[SyncEditTarget] = Field(default_factory=list, max_length=100)


class CookidooPlanSyncConfirmRequest(BaseModel):
    reviewed: bool = False
    selected_ids: list[str] | None = Field(default=None, max_length=200)


class SyncPreviewItem(BaseModel):
    item_id: str
    action: Literal["addition", "edit"]
    direction: SyncDirection
    date: date
    title: str
    servings: int = Field(gt=0)
    label: str | None = None
    cookidoo_recipe_id: str
    source_url: str
    image_url: str | None = None
    kitchen_recipe_id: UUID | None = None
    will_import_recipe: bool = False
    kitchen_meal_id: UUID | None = None
    external_entry_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    same_date_existing_count: int = 0
    same_date_existing_titles: list[str] = Field(default_factory=list)
    repeated_recipe_count: int = 0
    status: Literal["pending", "applied", "skipped", "failed"] = "pending"
    applied_meal_id: UUID | None = None
    applied_external_entry_id: str | None = None
    error: str | None = None


class SyncSkippedItem(BaseModel):
    kitchen_meal_id: UUID | None = None
    date: date
    title: str
    reason: str


class CookidooPlanSyncPreview(BaseModel):
    preview_id: UUID
    direction: SyncDirection
    start: date
    end: date
    status: Literal["ready", "applied", "expired", "failed"] = "ready"
    created_at: datetime
    expires_at: datetime
    additions: list[SyncPreviewItem] = Field(default_factory=list)
    edits: list[SyncPreviewItem] = Field(default_factory=list)
    skipped: list[SyncSkippedItem] = Field(default_factory=list)
    existing_kitchen_meals: list[dict[str, Any]] = Field(default_factory=list)
    existing_my_week_entries: list[dict[str, Any]] = Field(default_factory=list)
    summary: str
    applied_count: int = 0
    errors: list[str] = Field(default_factory=list)


class _SyncJob:
    def __init__(
        self,
        *,
        actor_id: str,
        generation: object,
        preview: CookidooPlanSyncPreview,
        my_week_entries: list[CookidooMyWeekEntry],
    ):
        self.actor_id = actor_id
        self.generation = generation
        self.preview = preview
        self.my_week_entries: dict[str, CookidooMyWeekEntry] = {}
        for idx, entry in enumerate(my_week_entries):
            self.my_week_entries[f"mw:{idx}"] = entry
            if entry.external_entry_id:
                self.my_week_entries[entry.external_entry_id] = entry
        self.created_monotonic = time.monotonic()


def _extract_cookidoo_id_from_url(source_url: str | None) -> str | None:
    if not source_url:
        return None
    try:
        parsed = urlparse(source_url)
    except (TypeError, ValueError):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"recipes", "created-recipes"}:
        return parts[-1].strip() or None
    return None


def _cookidoo_entry_key(entry_id: str | None, recipe_id: str, scheduled_date: date) -> str:
    return entry_id or f"{recipe_id}@{scheduled_date.isoformat()}"


def _week_anchor(day: date) -> date:
    """Represent a flexible week-level meal by Monday while retaining legacy dates."""
    return day - timedelta(days=day.weekday())


def _meal_sync_state(meal: Meal) -> dict[str, Any]:
    return {
        "date": meal.date.isoformat(),
        "recipe_id": str(meal.recipe_id),
        "servings": meal.servings,
        "label": meal.label,
    }


def _cookidoo_sync_state(entry: CookidooMyWeekEntry) -> dict[str, Any]:
    return {
        "date": entry.date.isoformat(),
        "cookidoo_recipe_id": entry.cookidoo_recipe_id,
        "servings": entry.servings,
        "label": entry.label,
    }


class CookidooPlanSyncService:
    """Preview and explicitly confirm meal plan sync between Kitchen and Cookidoo My Week."""

    MAX_PREVIEWS = 32
    TTL_SECONDS = 15 * 60
    MAX_RANGE_DAYS = 90

    def __init__(
        self,
        uow_or_kitchen: CookidooPlanSyncUnitOfWork | Any,
        history: Any = None,
        store: ConnectorCredentialPort | Any | None = None,
        household_id: UUID | None = None,
        *,
        provider: CookidooMyWeekProviderPort | Any | None = None,
        recipe_service: CookidooRecipeImporterPort | Any | None = None,
    ):
        self.history = history
        self.store = store
        self.household_id = household_id
        if callable(getattr(uow_or_kitchen, "session", None)) and callable(
            getattr(uow_or_kitchen, "load_kitchen_state", None)
        ):
            self._uow: CookidooPlanSyncUnitOfWork = uow_or_kitchen
            self.kitchen: Any = None
            if provider is not None:
                self.provider: CookidooMyWeekProviderPort | Any = provider
            else:
                if store is None or household_id is None:
                    raise ValueError("store and household_id are required when provider is omitted")
                self.provider = CookidooCollectionProvider(store=store, household_id=household_id)
            if recipe_service is None:
                raise ValueError(
                    "recipe_service is required when bootstrapping CookidooPlanSyncService with a UoW"
                )
            self.recipe_service: CookidooRecipeImporterPort | Any = recipe_service
        else:
            from home_kitchen.bootstrap import create_cookidoo_plan_sync_deps

            self.kitchen = uow_or_kitchen
            if store is None or household_id is None:
                raise ValueError(
                    "store and household_id are required for legacy CookidooPlanSyncService wiring"
                )
            self._uow, default_provider, default_recipe_service = create_cookidoo_plan_sync_deps(
                uow_or_kitchen,
                history,
                store,
                household_id,
                provider=provider,
                recipe_service=recipe_service,
            )
            self.provider = default_provider
            self.recipe_service = default_recipe_service
        self._previews: dict[UUID, _SyncJob] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for key, job in list(self._previews.items()):
            if now - job.created_monotonic >= self.TTL_SECONDS:
                del self._previews[key]

    def _get_job(self, actor_id: str, preview_id: UUID | str) -> _SyncJob:
        self._prune()
        if not actor_id or not actor_id.strip():
            raise ValueError("Autentisert bruker er påkrevd.")
        try:
            pid = UUID(str(preview_id))
            job = self._previews[pid]
        except (KeyError, ValueError, TypeError):
            raise KeyError(
                "Synkroniseringsforhåndsvisningen finnes ikke eller har utløpt."
            ) from None
        if job.actor_id != actor_id:
            raise KeyError("Synkroniseringsforhåndsvisningen tilhører en annen bruker.")
        return job

    def _load_kitchen_state(
        self, start: date, end: date
    ) -> tuple[
        list[Meal],
        dict[UUID, dict[str, Any]],
        dict[UUID, tuple[str, str]],
        dict[str, UUID],
    ]:
        return self._uow.load_kitchen_state(start, end)

    async def _load_sync_history(self) -> list[dict[str, Any]]:
        loader = getattr(self._uow, "load_sync_history", None)
        if not callable(loader):
            return []
        loaded = await asyncio.to_thread(loader)
        if not isinstance(loaded, list):
            return []
        return [cast(dict[str, Any], record) for record in loaded if isinstance(record, dict)]

    @staticmethod
    def _validate_range(start: date, end: date) -> None:
        if end < start:
            raise ValueError("Sluttdato må være på eller etter startdato.")
        if (end - start).days + 1 > CookidooPlanSyncService.MAX_RANGE_DAYS:
            raise ValueError(
                f"Datointervallet kan maksimalt være {CookidooPlanSyncService.MAX_RANGE_DAYS} dager."
            )

    def get_preview(self, actor_id: str, preview_id: UUID | str) -> CookidooPlanSyncPreview:
        job = self._get_job(actor_id, preview_id)
        if job.preview.expires_at <= datetime.now(UTC) and job.preview.status == "ready":
            job.preview.status = "expired"
        return job.preview.model_copy(deep=True)

    async def preview(
        self, actor_id: str, request: CookidooPlanSyncPreviewRequest
    ) -> CookidooPlanSyncPreview:
        if not actor_id or not actor_id.strip():
            raise ValueError("Autentisert bruker er påkrevd.")
        self._validate_range(request.start, request.end)
        self._prune()
        if len(self._previews) >= self.MAX_PREVIEWS:
            oldest_key = min(self._previews, key=lambda k: self._previews[k].created_monotonic)
            del self._previews[oldest_key]

        generation = await asyncio.to_thread(self.provider.generation, actor_id)
        my_week_entries = await self.provider.get_my_week_plan(request.start, request.end)
        kitchen_meals, recipes_by_id, links_by_recipe, links_by_ext_id = await asyncio.to_thread(
            self._load_kitchen_state, request.start, request.end
        )

        source_url_to_recipe_id: dict[str, UUID] = {}
        for rid, rec in recipes_by_id.items():
            src_url = rec.get("source_url")
            if isinstance(src_url, str) and src_url:
                source_url_to_recipe_id.setdefault(src_url, rid)

        existing_kitchen_summary = [
            {
                "meal_id": str(meal.id),
                "date": meal.date.isoformat(),
                "recipe_id": str(meal.recipe_id),
                "title": recipes_by_id.get(meal.recipe_id, {}).get("title", "Ukjent oppskrift"),
                "servings": meal.servings,
                "label": meal.label,
            }
            for meal in kitchen_meals
        ]
        existing_mw_summary = [
            {
                "external_entry_id": entry.external_entry_id,
                "cookidoo_recipe_id": entry.cookidoo_recipe_id,
                "date": entry.date.isoformat(),
                "title": entry.title,
                "servings": entry.servings,
                "source_url": entry.source_url,
                "label": entry.label,
            }
            for entry in my_week_entries
        ]

        additions: list[SyncPreviewItem] = []
        edits: list[SyncPreviewItem] = []
        skipped: list[SyncSkippedItem] = []
        edits_by_key: dict[str, SyncEditTarget] = {}
        seen_target_meals: set[UUID] = set()
        seen_target_entries: set[str] = set()
        for edit in request.edits:
            if edit.source_key in edits_by_key:
                raise ValueError(f"Kildeoppføringen {edit.source_key!r} kan bare ha én endring.")
            edits_by_key[edit.source_key] = edit
            if edit.target_meal_id is not None:
                if edit.target_meal_id in seen_target_meals:
                    raise ValueError("Hvert mål-måltid kan bare oppdateres av én endring.")
                seen_target_meals.add(edit.target_meal_id)
            if edit.target_external_entry_id:
                if edit.target_external_entry_id in seen_target_entries:
                    raise ValueError(
                        "Hver Cookidoo Min uke-oppføring kan bare oppdateres av én endring."
                    )
                seen_target_entries.add(edit.target_external_entry_id)
        matched_edit_keys: set[str] = set()

        if request.direction == "cookidoo_to_kitchen":
            kitchen_meals_by_id = {meal.id: meal for meal in kitchen_meals}
            for idx, mw in enumerate(my_week_entries):
                kitchen_recipe_id = links_by_ext_id.get(
                    mw.cookidoo_recipe_id
                ) or source_url_to_recipe_id.get(mw.source_url)
                same_date_meals = [meal for meal in kitchen_meals if meal.date == mw.date]
                same_date_titles = [
                    recipes_by_id.get(meal.recipe_id, {}).get("title", "Ukjent oppskrift")
                    for meal in same_date_meals
                ]
                repeated_count = (
                    sum(1 for meal in same_date_meals if meal.recipe_id == kitchen_recipe_id)
                    if kitchen_recipe_id is not None
                    else 0
                )

                edit_target = (
                    (edits_by_key.get(mw.external_entry_id) if mw.external_entry_id else None)
                    or edits_by_key.get(f"{mw.cookidoo_recipe_id}@{mw.date.isoformat()}")
                    or edits_by_key.get(f"mw:{idx}")
                    or edits_by_key.get(str(idx))
                )
                if edit_target is not None:
                    matched_edit_keys.add(edit_target.source_key)
                    if edit_target.target_meal_id is None:
                        raise ValueError(
                            "Endringsmål krever eksplisitt target_meal_id ved import til Kjøkken."
                        )
                    target_meal = kitchen_meals_by_id.get(edit_target.target_meal_id)
                    if target_meal is None:
                        raise ValueError(
                            f"Fant ikke måltidet {edit_target.target_meal_id} i Kjøkken for oppdatering."
                        )
                    target_title = recipes_by_id.get(target_meal.recipe_id, {}).get(
                        "title", "Ukjent oppskrift"
                    )
                    edits.append(
                        SyncPreviewItem(
                            item_id=f"edit-{idx}",
                            action="edit",
                            direction=request.direction,
                            date=mw.date,
                            title=mw.title,
                            servings=mw.servings,
                            label=mw.label,
                            cookidoo_recipe_id=mw.cookidoo_recipe_id,
                            source_url=mw.source_url,
                            image_url=mw.image_url,
                            kitchen_recipe_id=kitchen_recipe_id,
                            will_import_recipe=kitchen_recipe_id is None,
                            kitchen_meal_id=target_meal.id,
                            external_entry_id=mw.external_entry_id or f"mw:{idx}",
                            before={
                                "meal_id": str(target_meal.id),
                                "date": target_meal.date.isoformat(),
                                "recipe_id": str(target_meal.recipe_id),
                                "title": target_title,
                                "servings": target_meal.servings,
                                "label": target_meal.label,
                            },
                            after={
                                "date": mw.date.isoformat(),
                                "cookidoo_recipe_id": mw.cookidoo_recipe_id,
                                "title": mw.title,
                                "servings": mw.servings,
                                "label": mw.label,
                            },
                            same_date_existing_count=len(same_date_meals),
                            same_date_existing_titles=same_date_titles,
                            repeated_recipe_count=repeated_count,
                        )
                    )
                else:
                    additions.append(
                        SyncPreviewItem(
                            item_id=f"add-{idx}",
                            action="addition",
                            direction=request.direction,
                            date=mw.date,
                            title=mw.title,
                            servings=mw.servings,
                            label=mw.label,
                            cookidoo_recipe_id=mw.cookidoo_recipe_id,
                            source_url=mw.source_url,
                            image_url=mw.image_url,
                            kitchen_recipe_id=kitchen_recipe_id,
                            will_import_recipe=kitchen_recipe_id is None,
                            external_entry_id=mw.external_entry_id or f"mw:{idx}",
                            same_date_existing_count=len(same_date_meals),
                            same_date_existing_titles=same_date_titles,
                            repeated_recipe_count=repeated_count,
                        )
                    )
        else:
            meals_by_id = {meal.id: meal for meal in kitchen_meals}
            if request.meal_ids is not None:
                selected_meals: list[Meal] = []
                for mid in request.meal_ids:
                    found = meals_by_id.get(mid)
                    if found is None:
                        raise ValueError(f"Fant ikke måltidet {mid} i valgt periode.")
                    selected_meals.append(found)
            else:
                selected_meals = list(kitchen_meals)

            mw_by_entry_id = {
                entry.external_entry_id: entry
                for entry in my_week_entries
                if entry.external_entry_id
            }

            for idx, meal in enumerate(selected_meals):
                rec = recipes_by_id.get(meal.recipe_id)
                rec_title = str(rec.get("title") or "Uten tittel") if rec else "Ukjent oppskrift"
                if rec is None:
                    skipped.append(
                        SyncSkippedItem(
                            kitchen_meal_id=meal.id,
                            date=meal.date,
                            title=rec_title,
                            reason="Oppskriften finnes ikke lenger i Kjøkken.",
                        )
                    )
                    continue

                linked = links_by_recipe.get(meal.recipe_id)
                cookidoo_recipe_id: str | None = None
                source_url: str | None = None
                if linked is not None:
                    cookidoo_recipe_id, source_url = linked
                elif str(rec.get("source") or "") == str(RecipeSource.COOKIDOO):
                    source_url = rec.get("source_url")
                    cookidoo_recipe_id = _extract_cookidoo_id_from_url(source_url)

                if not cookidoo_recipe_id or not source_url:
                    skipped.append(
                        SyncSkippedItem(
                            kitchen_meal_id=meal.id,
                            date=meal.date,
                            title=rec_title,
                            reason="Oppskriften er ikke koblet til Cookidoo (KI-tilpasninger og egne oppskrifter forblir uavhengige utkast).",
                        )
                    )
                    continue

                target_date = meal.date + timedelta(days=6 - meal.date.weekday())
                same_date_mw = [entry for entry in my_week_entries if entry.date == target_date]
                same_date_titles = [entry.title for entry in same_date_mw]
                repeated_count = sum(
                    1 for entry in same_date_mw if entry.cookidoo_recipe_id == cookidoo_recipe_id
                )

                edit_target = edits_by_key.get(str(meal.id))
                if edit_target is not None:
                    matched_edit_keys.add(edit_target.source_key)
                    if not edit_target.target_external_entry_id:
                        raise ValueError(
                            "Endringsmål krever eksplisitt target_external_entry_id ved eksport til Cookidoo."
                        )
                    target_mw = mw_by_entry_id.get(edit_target.target_external_entry_id)
                    if target_mw is None:
                        raise ValueError(
                            f"Fant ikke Cookidoo Min uke-oppføringen {edit_target.target_external_entry_id} for oppdatering."
                        )
                    edits.append(
                        SyncPreviewItem(
                            item_id=f"edit-{idx}",
                            action="edit",
                            direction=request.direction,
                            date=target_date,
                            title=rec_title,
                            servings=meal.servings,
                            label=meal.label,
                            cookidoo_recipe_id=cookidoo_recipe_id,
                            source_url=source_url,
                            image_url=rec.get("image_url"),
                            kitchen_recipe_id=meal.recipe_id,
                            kitchen_meal_id=meal.id,
                            external_entry_id=target_mw.external_entry_id,
                            before={
                                "external_entry_id": target_mw.external_entry_id,
                                "date": target_mw.date.isoformat(),
                                "cookidoo_recipe_id": target_mw.cookidoo_recipe_id,
                                "title": target_mw.title,
                                "servings": target_mw.servings,
                                "label": target_mw.label,
                            },
                            after={
                                "date": target_date.isoformat(),
                                "cookidoo_recipe_id": cookidoo_recipe_id,
                                "title": rec_title,
                                "servings": meal.servings,
                                "label": meal.label,
                            },
                            same_date_existing_count=len(same_date_mw),
                            same_date_existing_titles=same_date_titles,
                            repeated_recipe_count=repeated_count,
                        )
                    )
                else:
                    additions.append(
                        SyncPreviewItem(
                            item_id=f"add-{idx}",
                            action="addition",
                            direction=request.direction,
                            date=target_date,
                            title=rec_title,
                            servings=meal.servings,
                            label=meal.label,
                            cookidoo_recipe_id=cookidoo_recipe_id,
                            source_url=source_url,
                            image_url=rec.get("image_url"),
                            kitchen_recipe_id=meal.recipe_id,
                            kitchen_meal_id=meal.id,
                            same_date_existing_count=len(same_date_mw),
                            same_date_existing_titles=same_date_titles,
                            repeated_recipe_count=repeated_count,
                        )
                    )

        unmatched_edits = [key for key in edits_by_key if key not in matched_edit_keys]
        if unmatched_edits:
            raise ValueError(
                f"Fant ikke kildeoppføringen {unmatched_edits[0]!r} i valgt periode for oppdatering."
            )

        now = datetime.now(UTC)
        summary = (
            f"{len(additions)} nye tillegg, {len(edits)} planlagte endringer"
            + (f", {len(skipped)} utelatt" if skipped else "")
            + "."
        )
        preview = CookidooPlanSyncPreview(
            preview_id=uuid4(),
            direction=request.direction,
            start=request.start,
            end=request.end,
            status="ready",
            created_at=now,
            expires_at=now + timedelta(seconds=self.TTL_SECONDS),
            additions=additions,
            edits=edits,
            skipped=skipped,
            existing_kitchen_meals=existing_kitchen_summary,
            existing_my_week_entries=existing_mw_summary,
            summary=summary,
        )
        self._previews[preview.preview_id] = _SyncJob(
            actor_id=actor_id,
            generation=generation,
            preview=preview,
            my_week_entries=my_week_entries,
        )
        return preview.model_copy(deep=True)

    async def confirm(
        self,
        actor_id: str,
        preview_id: UUID | str,
        request: CookidooPlanSyncConfirmRequest,
    ) -> CookidooPlanSyncPreview:
        if request.reviewed is not True:
            raise ValueError("Gjennomgå og bekreft synkroniseringsplanen før endringer lagres.")
        job = self._get_job(actor_id, preview_id)

        if job.preview.status == "applied":
            return job.preview.model_copy(deep=True)
        if job.preview.status == "expired" or job.preview.expires_at <= datetime.now(UTC):
            job.preview.status = "expired"
            raise ValueError("Forhåndsvisningen har utløpt; hent ukeplanen på nytt.")

        current_gen = await asyncio.to_thread(self.provider.generation, actor_id)
        if current_gen != job.generation:
            raise ValueError("Cookidoo-tilkoblingen ble endret; forhåndsvis på nytt.")

        all_items = [*job.preview.additions, *job.preview.edits]
        if request.selected_ids is not None:
            if not request.selected_ids or len(request.selected_ids) != len(
                set(request.selected_ids)
            ):
                raise ValueError("Velg minst én unik oppføring før bekreftelse.")
            items_by_id = {item.item_id: item for item in all_items}
            if any(item_id not in items_by_id for item_id in request.selected_ids):
                raise ValueError("Valget må inneholde nøyaktige ID-er fra denne forhåndsvisningen.")
            selected_set = set(request.selected_ids)
            selected_items = [items_by_id[item_id] for item_id in request.selected_ids]
        else:
            if not all_items:
                raise ValueError("Ingen synkroniseringsoppføringer å bekrefte.")
            selected_items = list(all_items)
            selected_set = {item.item_id for item in selected_items}

        if job.preview.direction == "cookidoo_to_kitchen":
            await self._apply_cookidoo_to_kitchen(actor_id, job, selected_items, selected_set)
        else:
            await self._apply_kitchen_to_cookidoo(actor_id, job, selected_items, selected_set)

        for item in all_items:
            if item.item_id not in selected_set and item.status == "pending":
                item.status = "skipped"

        job.preview.applied_count = sum(1 for item in all_items if item.status == "applied")
        job.preview.status = "applied"
        return job.preview.model_copy(deep=True)

    async def _resolve_or_import_cookidoo_recipe(
        self, actor_id: str, job: _SyncJob, item: SyncPreviewItem
    ) -> UUID:
        if item.kitchen_recipe_id is not None:
            return item.kitchen_recipe_id

        mw_entry = job.my_week_entries.get(item.external_entry_id or "")
        entry = CollectionEntry(
            id=item.cookidoo_recipe_id,
            provider="cookidoo",
            title=item.title,
            source_url=item.source_url,
            image_url=item.image_url,
            servings=item.servings,
            ingredients=list(mw_entry.ingredients) if mw_entry else [],
            instructions=list(mw_entry.instructions) if mw_entry else [],
            guided_steps=list(mw_entry.guided_steps) if mw_entry else [],
        )
        existing_id, is_dup = await asyncio.to_thread(self.recipe_service.check_duplicate, entry)
        if is_dup and existing_id is not None:
            return existing_id

        if not entry.ingredients or not entry.instructions:
            try:
                draft = await self.provider.extract_entry(entry)
                entry = entry.model_copy(
                    update={
                        "title": draft.title,
                        "servings": draft.servings,
                        "ingredients": draft.ingredients,
                        "instructions": draft.instructions,
                        "image_url": str(draft.image_url) if draft.image_url else entry.image_url,
                        "recipe_type": draft.recipe_type,
                        "prep_time_minutes": draft.prep_time_minutes,
                        "prep_category": draft.prep_category,
                        "guided_steps": draft.guided_steps,
                    }
                )
            except Exception:
                # Fallback minimal Cookidoo placeholder when My Week entry had no full detail endpoint
                if not entry.ingredients:
                    entry.ingredients = ["1 porsjon ingredienser"]
                if not entry.instructions:
                    entry.instructions = ["Følg veiledning i Cookidoo / TM6"]

        recipe_id, _ = await asyncio.to_thread(
            self.recipe_service.import_recipe_atomic, actor_id, entry, True
        )
        return recipe_id

    async def _apply_cookidoo_to_kitchen(
        self,
        actor_id: str,
        job: _SyncJob,
        selected_items: list[SyncPreviewItem],
        selected_set: set[str],
    ) -> None:
        del selected_set
        resolved_recipes: dict[str, UUID] = {}
        for item in selected_items:
            if item.cookidoo_recipe_id in resolved_recipes:
                continue
            resolved_recipes[
                item.cookidoo_recipe_id
            ] = await self._resolve_or_import_cookidoo_recipe(actor_id, job, item)

        def _commit() -> None:
            with self._uow.session() as session:
                session.lock_provider_generation(self.provider, actor_id, job.generation)
                for item in selected_items:
                    recipe_id = resolved_recipes[item.cookidoo_recipe_id]
                    if item.action == "addition":
                        new_meal = Meal(
                            id=uuid4(),
                            date=item.date,
                            recipe_id=recipe_id,
                            servings=item.servings,
                            label=item.label,
                        )
                        session.add_meal(new_meal)
                        item.status = "applied"
                        item.applied_meal_id = new_meal.id
                        item.kitchen_recipe_id = recipe_id
                        session.record_sync_history(
                            actor_id=actor_id,
                            direction="cookidoo_to_kitchen",
                            action="addition",
                            meal_id=new_meal.id,
                            recipe_id=recipe_id,
                            cookidoo_recipe_id=item.cookidoo_recipe_id,
                            source_url=item.source_url,
                            scheduled_date=item.date,
                            message="Synkroniserte måltid fra Cookidoo Min uke",
                            external_entry_id=_cookidoo_entry_key(
                                item.external_entry_id, item.cookidoo_recipe_id, item.date
                            ),
                            include_external_entry_id=True,
                            sync_state=_meal_sync_state(new_meal),
                        )
                    else:
                        assert item.kitchen_meal_id is not None
                        current = session.get_meal(item.kitchen_meal_id)
                        if current is None:
                            raise ValueError(
                                "Måltidet som skulle oppdateres ble fjernet; forhåndsvis på nytt."
                            )
                        before = item.before or {}
                        if (
                            current.date.isoformat() != before.get("date")
                            or str(current.recipe_id) != before.get("recipe_id")
                            or current.servings != before.get("servings")
                            or current.label != before.get("label")
                        ):
                            raise ValueError(
                                "Måltidet ble endret før bekreftelse; forhåndsvis på nytt."
                            )
                        updated_meal = Meal(
                            id=item.kitchen_meal_id,
                            date=item.date,
                            recipe_id=recipe_id,
                            servings=item.servings,
                            label=item.label,
                            version=current.version,
                        )
                        session.update_meal(updated_meal)
                        item.status = "applied"
                        item.applied_meal_id = item.kitchen_meal_id
                        item.kitchen_recipe_id = recipe_id
                        session.record_sync_history(
                            actor_id=actor_id,
                            direction="cookidoo_to_kitchen",
                            action="edit",
                            meal_id=item.kitchen_meal_id,
                            recipe_id=recipe_id,
                            cookidoo_recipe_id=item.cookidoo_recipe_id,
                            source_url=item.source_url,
                            scheduled_date=item.date,
                            message="Oppdaterte måltid fra Cookidoo Min uke",
                            external_entry_id=_cookidoo_entry_key(
                                item.external_entry_id, item.cookidoo_recipe_id, item.date
                            ),
                            include_external_entry_id=True,
                            sync_state=_meal_sync_state(updated_meal),
                        )

        await asyncio.to_thread(_commit)

    async def sync_cookidoo_to_kitchen_best_effort(
        self, actor_id: str, start: date, end: date
    ) -> dict[str, int]:
        """Pull safe, already-known Cookidoo week changes without replacing reviewed data.

        Automatically managed rows are tracked by Cookidoo entry ID. Existing rows are
        changed or removed only while they still match the last state written by this sync.
        New recipes that need importing remain in the explicit review flow.
        """
        result = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        try:
            self._validate_range(start, end)
            generation = await asyncio.to_thread(self.provider.generation, actor_id)
            source_entries = await self.provider.get_my_week_plan(start, end)
            meals, recipes_by_id, links_by_recipe, links_by_ext_id = await asyncio.to_thread(
                self._load_kitchen_state, start, end
            )
            recipe_by_source_url = {
                str(recipe.get("source_url")): recipe_id
                for recipe_id, recipe in recipes_by_id.items()
                if recipe.get("source_url")
                and str(recipe.get("source") or "") == str(RecipeSource.COOKIDOO)
            }
            history = await self._load_sync_history()

            # The latest durable inbound record owns a source entry. A meal edited locally
            # since that record no longer matches sync_state and is left untouched.
            inbound_by_entry: dict[str, dict[str, Any]] = {}
            for record in history:
                if record.get("direction") != "cookidoo_to_kitchen":
                    continue
                entry_id = record.get("external_entry_id")
                if isinstance(entry_id, str) and entry_id:
                    inbound_by_entry[entry_id] = record

            kitchen_by_id = {meal.id: meal for meal in meals}
            source_by_key: dict[str, CookidooMyWeekEntry] = {}
            for entry in source_entries:
                key = _cookidoo_entry_key(
                    entry.external_entry_id, entry.cookidoo_recipe_id, entry.date
                )
                source_by_key[key] = entry

            operations: list[tuple[str, str, CookidooMyWeekEntry, UUID | None, Meal | None]] = []
            counts: dict[tuple[date, UUID], int] = {}
            for meal in meals:
                week_key = _week_anchor(meal.date)
                counts[(week_key, meal.recipe_id)] = counts.get((week_key, meal.recipe_id), 0) + 1
            reserved_counts: dict[tuple[date, UUID], int] = {}

            for key, entry in source_by_key.items():
                recipe_id = links_by_ext_id.get(entry.cookidoo_recipe_id)
                if recipe_id is None:
                    recipe_id = next(
                        (
                            rid
                            for rid, (_external_id, source_url) in links_by_recipe.items()
                            if source_url == entry.source_url
                        ),
                        None,
                    )
                if recipe_id is None:
                    recipe_id = recipe_by_source_url.get(entry.source_url)
                # Importing an unknown Cookidoo recipe changes source data and still needs
                # the established recipe review flow.
                if recipe_id is None:
                    result["skipped"] += 1
                    continue

                prior = inbound_by_entry.get(key)
                if prior is not None and prior.get("action") == "removed":
                    prior = None
                mapped_id: UUID | None = None
                if prior is not None:
                    try:
                        mapped_id = UUID(str(prior.get("meal_id")))
                    except (ValueError, TypeError):
                        mapped_id = None
                mapped_meal = kitchen_by_id.get(mapped_id) if mapped_id else None
                if mapped_meal is not None and prior is not None:
                    prior_state = prior.get("sync_state") or {}
                    if _meal_sync_state(mapped_meal) != prior_state:
                        result["skipped"] += 1
                        continue
                    desired = Meal(
                        id=mapped_meal.id,
                        date=(
                            mapped_meal.date
                            if _week_anchor(mapped_meal.date) == _week_anchor(entry.date)
                            else _week_anchor(entry.date)
                        ),
                        recipe_id=recipe_id,
                        servings=entry.servings,
                        label=entry.label,
                    )
                    if _meal_sync_state(desired) != prior_state:
                        operations.append(("update", key, entry, mapped_meal.id, desired))
                    continue
                if prior is not None:
                    # A user removed a previously synced Kitchen meal. Do not silently
                    # recreate it while the source entry remains in Cookidoo.
                    result["skipped"] += 1
                    continue

                kitchen_date = _week_anchor(entry.date)
                duplicate_key = (kitchen_date, recipe_id)
                already_present = reserved_counts.get(duplicate_key, 0)
                if already_present < counts.get(duplicate_key, 0):
                    reserved_counts[duplicate_key] = already_present + 1
                    continue
                operations.append(("add", key, entry, None, None))
                reserved_counts[duplicate_key] = already_present + 1

            active_keys = set(source_by_key)
            for key, prior in inbound_by_entry.items():
                if key in active_keys or prior.get("action") == "removed":
                    continue
                try:
                    prior_date = date.fromisoformat(str(prior.get("date")))
                except (TypeError, ValueError):
                    continue
                if not start <= prior_date <= end:
                    continue
                try:
                    meal_id = UUID(str(prior.get("meal_id")))
                except (ValueError, TypeError):
                    continue
                meal = kitchen_by_id.get(meal_id)
                if meal is None:
                    continue
                if _meal_sync_state(meal) != (prior.get("sync_state") or {}):
                    result["skipped"] += 1
                    continue
                source_url = str(prior.get("source_url") or "")
                removed_entry = CookidooMyWeekEntry(
                    external_entry_id=key,
                    cookidoo_recipe_id=str(prior.get("cookidoo_recipe_id") or "unknown"),
                    title="Removed from Cookidoo Min uke",
                    date=date.fromisoformat(str(prior.get("date"))),
                    servings=meal.servings,
                    source_url=source_url
                    or "https://cookidoo.international/recipes/recipe/nb-NO/unknown",
                    label=meal.label,
                )
                operations.append(("remove", key, removed_entry, meal_id, meal))

            def _commit() -> None:
                with self._uow.session() as session:
                    session.lock_provider_generation(self.provider, actor_id, generation)
                    for action, key, entry, meal_id, desired in operations:
                        planned_prior = inbound_by_entry.get(key)
                        current_prior = session.find_sync_history("cookidoo_to_kitchen", key)
                        if current_prior != planned_prior:
                            # Another sync may have committed this source entry after the
                            # external read. Re-check under the credential row lock before
                            # writing so overlapping connect/view/periodic triggers stay idempotent.
                            result["skipped"] += 1
                            continue
                        recipe_id = links_by_ext_id.get(entry.cookidoo_recipe_id)
                        if recipe_id is None:
                            recipe_id = next(
                                (
                                    rid
                                    for rid, (_external_id, source_url) in links_by_recipe.items()
                                    if source_url == entry.source_url
                                ),
                                None,
                            )
                        if action == "add":
                            if recipe_id is None:
                                result["skipped"] += 1
                                continue
                            meal = Meal(
                                id=uuid4(),
                                date=_week_anchor(entry.date),
                                recipe_id=recipe_id,
                                servings=entry.servings,
                                label=entry.label,
                            )
                            session.add_meal(meal)
                            result["added"] += 1
                            session.record_sync_history(
                                actor_id=actor_id,
                                direction="cookidoo_to_kitchen",
                                action="addition",
                                meal_id=meal.id,
                                recipe_id=recipe_id,
                                cookidoo_recipe_id=entry.cookidoo_recipe_id,
                                source_url=entry.source_url,
                                scheduled_date=meal.date,
                                message="Synkroniserte måltid fra Cookidoo Min uke",
                                external_entry_id=key,
                                include_external_entry_id=True,
                                sync_state=_meal_sync_state(meal),
                            )
                        elif action == "update" and meal_id is not None and desired is not None:
                            current = session.get_meal(meal_id)
                            prior = inbound_by_entry.get(key) or {}
                            if current is None or _meal_sync_state(current) != (
                                prior.get("sync_state") or {}
                            ):
                                result["skipped"] += 1
                                continue
                            if recipe_id is None:
                                result["skipped"] += 1
                                continue
                            desired = desired.model_copy(
                                update={"recipe_id": recipe_id, "version": current.version}
                            )
                            session.update_meal(desired)
                            result["updated"] += 1
                            session.record_sync_history(
                                actor_id=actor_id,
                                direction="cookidoo_to_kitchen",
                                action="edit",
                                meal_id=desired.id,
                                recipe_id=recipe_id,
                                cookidoo_recipe_id=entry.cookidoo_recipe_id,
                                source_url=entry.source_url,
                                scheduled_date=entry.date,
                                message="Oppdaterte måltid fra Cookidoo Min uke",
                                external_entry_id=key,
                                include_external_entry_id=True,
                                sync_state=_meal_sync_state(desired),
                            )
                        elif action == "remove" and meal_id is not None:
                            current = session.get_meal(meal_id)
                            prior = inbound_by_entry.get(key) or {}
                            if current is None:
                                continue
                            if _meal_sync_state(current) != (prior.get("sync_state") or {}):
                                result["skipped"] += 1
                                continue
                            session.delete_meal(meal_id)
                            result["removed"] += 1
                            session.record_sync_history(
                                actor_id=actor_id,
                                direction="cookidoo_to_kitchen",
                                action="removed",
                                meal_id=meal_id,
                                recipe_id=current.recipe_id,
                                cookidoo_recipe_id=entry.cookidoo_recipe_id,
                                source_url=entry.source_url,
                                scheduled_date=current.date,
                                message="Fjernet synkronisert måltid som ikke lenger finnes i Cookidoo Min uke",
                                external_entry_id=key,
                                include_external_entry_id=True,
                                sync_state=_meal_sync_state(current),
                            )

            if operations:
                await asyncio.to_thread(_commit)
        except Exception:
            _LOG.info("Automatic Cookidoo-to-Kitchen sync failed", exc_info=True)
        return result

    async def sync_forever(
        self, interval_seconds: float = 300.0, *, actor_id: str = "system"
    ) -> None:
        """Periodically import safe Cookidoo My Week changes for the current week."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        while True:
            today = datetime.now().date()
            week_start = today - timedelta(days=today.weekday())
            week_end = week_start + timedelta(days=6)
            await self.sync_cookidoo_to_kitchen_best_effort(actor_id, week_start, week_end)
            await asyncio.sleep(interval_seconds)

    async def sync_kitchen_to_cookidoo_best_effort(
        self, actor_id: str, start: date, end: date
    ) -> dict[str, int]:
        """Reconcile Cookidoo-linked Kitchen meals to their week's Sunday, best-effort."""
        result = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
        try:
            self._validate_range(start, end)
            generation = await asyncio.to_thread(self.provider.generation, actor_id)
            meals, recipes_by_id, links_by_recipe, _links_by_ext_id = await asyncio.to_thread(
                self._load_kitchen_state, start, end
            )
            history = await self._load_sync_history()
            outbound_by_meal: dict[str, dict[str, Any]] = {}
            for record in history:
                if record.get("direction") != "kitchen_to_cookidoo":
                    continue
                meal_id = record.get("meal_id")
                if isinstance(meal_id, str) and meal_id:
                    outbound_by_meal[meal_id] = record

            live_entries = await self.provider.get_my_week_plan(start, end)
            live_by_entry_id = {
                entry.external_entry_id: entry for entry in live_entries if entry.external_entry_id
            }
            live_meal_ids = {str(meal.id) for meal in meals}
            pending_history: list[dict[str, Any]] = []
            claimed_entry_ids = {
                str(record.get("external_entry_id"))
                for meal_id, record in outbound_by_meal.items()
                if meal_id in live_meal_ids
                and record.get("action") != "removed"
                and record.get("external_entry_id")
            }

            for meal in meals:
                linked = links_by_recipe.get(meal.recipe_id)
                rec = recipes_by_id.get(meal.recipe_id)
                if rec is None:
                    continue
                if linked is not None:
                    cookidoo_recipe_id, source_url = linked
                elif str(rec.get("source") or "") == str(RecipeSource.COOKIDOO):
                    source_url = str(rec.get("source_url") or "")
                    cookidoo_recipe_id = _extract_cookidoo_id_from_url(source_url)
                    if not cookidoo_recipe_id:
                        continue
                else:
                    continue
                target_date = meal.date + timedelta(days=6 - meal.date.weekday())
                prior = outbound_by_meal.get(str(meal.id))
                if prior is not None and prior.get("action") == "removed":
                    prior = None
                prior_entry_id = str(prior.get("external_entry_id") or "") if prior else ""
                target_entry = live_by_entry_id.get(prior_entry_id) if prior_entry_id else None
                if target_entry is not None and prior is not None:
                    claimed_entry_ids.add(prior_entry_id)
                    expected = prior.get("sync_state") or {}
                    if _cookidoo_sync_state(target_entry) != expected:
                        result["skipped"] += 1
                        continue
                    desired_state = {
                        "date": target_date.isoformat(),
                        "cookidoo_recipe_id": cookidoo_recipe_id,
                        "servings": meal.servings,
                        "label": meal.label,
                    }
                    if expected == desired_state:
                        continue
                    if (
                        target_entry.cookidoo_recipe_id != cookidoo_recipe_id
                        or target_entry.date != target_date
                    ) and sum(
                        candidate.cookidoo_recipe_id == target_entry.cookidoo_recipe_id
                        and candidate.date == target_entry.date
                        for candidate in live_entries
                    ) > 1:
                        result["skipped"] += 1
                        continue
                    written = await self.provider.add_to_my_week(
                        cookidoo_recipe_id,
                        target_date,
                        servings=meal.servings,
                        label=meal.label,
                        entry_id=prior_entry_id,
                        read_before_write=False,
                        is_custom="/created-recipes/" in source_url,
                        previous_recipe_id=target_entry.cookidoo_recipe_id,
                        previous_date=target_entry.date,
                        previous_is_custom=target_entry.is_custom,
                    )
                    result["updated"] += 1
                else:
                    candidate = next(
                        (
                            entry
                            for entry in live_entries
                            if entry.external_entry_id
                            and entry.external_entry_id not in claimed_entry_ids
                            and entry.cookidoo_recipe_id == cookidoo_recipe_id
                            and entry.date == target_date
                        ),
                        None,
                    )
                    if candidate is not None:
                        written = candidate
                        claimed_entry_ids.add(candidate.external_entry_id or "")
                    else:
                        written = await self.provider.add_to_my_week(
                            cookidoo_recipe_id,
                            target_date,
                            servings=meal.servings,
                            label=meal.label,
                            read_before_write=False,
                            is_custom="/created-recipes/" in source_url,
                        )
                        result["added"] += 1
                        if written.external_entry_id:
                            claimed_entry_ids.add(written.external_entry_id)
                pending_history.append(
                    {
                        "direction": "kitchen_to_cookidoo",
                        "action": "addition" if prior is None else "edit",
                        "meal_id": str(meal.id),
                        "recipe_id": meal.recipe_id,
                        "cookidoo_recipe_id": cookidoo_recipe_id,
                        "source_url": source_url,
                        "date": target_date,
                        "external_entry_id": written.external_entry_id,
                        "sync_state": _cookidoo_sync_state(written),
                        "message": "Synkroniserte måltid til Cookidoo Min uke",
                    }
                )

            for meal_id, prior in outbound_by_meal.items():
                if meal_id in live_meal_ids or prior.get("action") == "removed":
                    continue
                try:
                    prior_date = date.fromisoformat(
                        str((prior.get("sync_state") or {}).get("date"))
                    )
                except (TypeError, ValueError):
                    try:
                        prior_date = date.fromisoformat(str(prior.get("date")))
                    except (TypeError, ValueError):
                        continue
                if not start <= prior_date <= end:
                    continue
                external_entry_id = str(prior.get("external_entry_id") or "")
                entry = live_by_entry_id.get(external_entry_id)
                if entry is None:
                    continue
                if _cookidoo_sync_state(entry) != (prior.get("sync_state") or {}):
                    result["skipped"] += 1
                    continue
                if any(
                    str(meal.id) != meal_id
                    and (link := links_by_recipe.get(meal.recipe_id)) is not None
                    and link[0] == entry.cookidoo_recipe_id
                    and meal.date + timedelta(days=6 - meal.date.weekday()) == entry.date
                    for meal in meals
                ):
                    result["skipped"] += 1
                    continue
                if (
                    sum(
                        candidate.cookidoo_recipe_id == entry.cookidoo_recipe_id
                        and candidate.date == entry.date
                        for candidate in live_entries
                    )
                    > 1
                ):
                    result["skipped"] += 1
                    continue
                await self.provider.remove_from_my_week(
                    entry.cookidoo_recipe_id,
                    entry.date,
                    is_custom=entry.is_custom,
                )
                result["removed"] += 1
                pending_history.append(
                    {
                        "direction": "kitchen_to_cookidoo",
                        "action": "removed",
                        "meal_id": meal_id,
                        "recipe_id": prior.get("recipe_id"),
                        "cookidoo_recipe_id": entry.cookidoo_recipe_id,
                        "source_url": entry.source_url,
                        "date": entry.date,
                        "external_entry_id": external_entry_id,
                        "sync_state": _cookidoo_sync_state(entry),
                        "message": "Fjernet måltid fra Cookidoo Min uke etter fjerning i Kjøkken",
                    }
                )

            if pending_history:

                def _record() -> None:
                    with self._uow.session() as session:
                        session.lock_provider_generation(self.provider, actor_id, generation)
                        for record in pending_history:
                            session.record_sync_history(
                                actor_id=actor_id,
                                direction=record["direction"],
                                action=record["action"],
                                meal_id=UUID(record["meal_id"]),
                                recipe_id=(
                                    record["recipe_id"]
                                    if isinstance(record["recipe_id"], UUID)
                                    else UUID(str(record["recipe_id"]))
                                    if record["recipe_id"]
                                    else None
                                ),
                                cookidoo_recipe_id=record["cookidoo_recipe_id"],
                                source_url=record["source_url"],
                                scheduled_date=record["date"],
                                message=record["message"],
                                external_entry_id=record["external_entry_id"],
                                include_external_entry_id=True,
                                sync_state=record["sync_state"],
                            )

                await asyncio.to_thread(_record)
        except Exception:
            _LOG.info("Automatic Kitchen-to-Cookidoo sync failed", exc_info=True)
        return result

    async def _apply_kitchen_to_cookidoo(
        self,
        actor_id: str,
        job: _SyncJob,
        selected_items: list[SyncPreviewItem],
        selected_set: set[str],
    ) -> None:
        del selected_set
        live_mw = await self.provider.get_my_week_plan(job.preview.start, job.preview.end)
        live_mw_by_id = {
            entry.external_entry_id: entry for entry in live_mw if entry.external_entry_id
        }
        for item in selected_items:
            if item.action == "edit":
                if not item.external_entry_id or item.external_entry_id not in live_mw_by_id:
                    raise ValueError(
                        "Cookidoo-oppføringen ble endret eller fjernet før bekreftelse; forhåndsvis på nytt."
                    )

        written_entries: dict[str, CookidooMyWeekEntry] = {}
        for item in selected_items:
            target_mw = (
                live_mw_by_id.get(item.external_entry_id or "") if item.action == "edit" else None
            )
            written = await self.provider.add_to_my_week(
                item.cookidoo_recipe_id,
                item.date,
                servings=item.servings,
                label=item.label,
                entry_id=item.external_entry_id if item.action == "edit" else None,
                read_before_write=False,
                is_custom="/created-recipes/" in (item.source_url or ""),
                previous_recipe_id=target_mw.cookidoo_recipe_id if target_mw else None,
                previous_date=target_mw.date if target_mw else None,
                previous_is_custom=target_mw.is_custom if target_mw else False,
            )
            item.status = "applied"
            item.applied_external_entry_id = written.external_entry_id
            written_entries[item.item_id] = written

        def _record_history() -> None:
            with self._uow.session() as session:
                session.lock_provider_generation(self.provider, actor_id, job.generation)
                for item in selected_items:
                    session.record_sync_history(
                        actor_id=actor_id,
                        direction="kitchen_to_cookidoo",
                        action=item.action,
                        meal_id=item.kitchen_meal_id,
                        recipe_id=item.kitchen_recipe_id,
                        cookidoo_recipe_id=item.cookidoo_recipe_id,
                        source_url=item.source_url,
                        scheduled_date=item.date,
                        external_entry_id=item.applied_external_entry_id,
                        include_external_entry_id=True,
                        sync_state=(
                            _cookidoo_sync_state(written_entries[item.item_id])
                            if item.item_id in written_entries
                            else None
                        ),
                        message="Synkroniserte måltid til Cookidoo Min uke",
                    )

        await asyncio.to_thread(_record_history)
