"""Cookidoo My Week <-> Kitchen meal plan preview and explicit confirm service."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import insert, select, update

from home_kitchen.connector_store import ConnectorStore
from home_kitchen.cookidoo_recipes import (
    CookidooCollectionProvider,
    CookidooMyWeekEntry,
    CookidooRecipeService,
)
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.models import Meal, RecipeSource
from home_kitchen.recipes.adapters.postgres import source_links
from home_kitchen.recipes.collections import CollectionEntry
from home_kitchen.service import KitchenService
from home_kitchen.store import entity_values, meals, recipes

SyncDirection = Literal["cookidoo_to_kitchen", "kitchen_to_cookidoo"]


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


class CookidooPlanSyncService:
    """Preview and explicitly confirm meal plan sync between Kitchen and Cookidoo My Week."""

    MAX_PREVIEWS = 32
    TTL_SECONDS = 15 * 60
    MAX_RANGE_DAYS = 90

    def __init__(
        self,
        kitchen: KitchenService | Any,
        history: HistoryRepository | Any,
        store: ConnectorStore,
        household_id: UUID,
        *,
        provider: CookidooCollectionProvider | None = None,
        recipe_service: CookidooRecipeService | None = None,
    ):
        self.kitchen = kitchen
        self.history = history
        self.store = store
        self.household_id = household_id
        self.provider = provider or CookidooCollectionProvider(
            store=store, household_id=household_id
        )
        self.recipe_service = recipe_service or CookidooRecipeService(
            kitchen=kitchen,
            history=history,
            store=store,
            household_id=household_id,
            collection_provider=self.provider,
        )
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
        with self.kitchen.store.engine.connect() as conn:
            meal_rows = (
                conn.execute(
                    select(meals)
                    .where(meals.c.date >= start, meals.c.date <= end)
                    .order_by(meals.c.date, meals.c.position)
                )
                .mappings()
                .all()
            )
            kitchen_meals = [Meal.model_validate(dict(row)) for row in meal_rows]

            recipe_rows = conn.execute(select(recipes)).mappings().all()
            recipes_by_id: dict[UUID, dict[str, Any]] = {}
            for row in recipe_rows:
                rid = row["id"]
                rid_uuid = rid if isinstance(rid, UUID) else UUID(str(rid))
                recipes_by_id[rid_uuid] = dict(row)

            link_rows = (
                conn.execute(select(source_links).where(source_links.c.provider == "cookidoo"))
                .mappings()
                .all()
            )
            links_by_recipe: dict[UUID, tuple[str, str]] = {}
            links_by_ext_id: dict[str, UUID] = {}
            for row in link_rows:
                rid = row["recipe_id"]
                rid_uuid = rid if isinstance(rid, UUID) else UUID(str(rid))
                ext_id = str(row["external_recipe_id"])
                src_url = str(row["source_url"])
                links_by_recipe[rid_uuid] = (ext_id, src_url)
                links_by_ext_id[ext_id] = rid_uuid

        return kitchen_meals, recipes_by_id, links_by_recipe, links_by_ext_id

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

                same_date_mw = [entry for entry in my_week_entries if entry.date == meal.date]
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
                            date=meal.date,
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
                                "date": meal.date.isoformat(),
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
                            date=meal.date,
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
            with self.kitchen.store.engine.begin() as connection:
                self.provider.lock_generation(connection, actor_id, job.generation)
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
                        connection.execute(insert(meals).values(**entity_values(new_meal)))
                        item.status = "applied"
                        item.applied_meal_id = new_meal.id
                        item.kitchen_recipe_id = recipe_id
                        self.history.record(
                            HistoryEntryCreate(
                                kind="planning",
                                message="Synkroniserte måltid fra Cookidoo Min uke",
                                recipe_id=recipe_id,
                                structured={
                                    "provider": "cookidoo",
                                    "direction": "cookidoo_to_kitchen",
                                    "action": "addition",
                                    "meal_id": str(new_meal.id),
                                    "cookidoo_recipe_id": item.cookidoo_recipe_id,
                                    "source_url": item.source_url,
                                    "date": item.date.isoformat(),
                                },
                            ),
                            actor_id=actor_id,
                            connection=connection,
                        )
                    else:
                        assert item.kitchen_meal_id is not None
                        row = (
                            connection.execute(
                                select(meals).where(meals.c.id == item.kitchen_meal_id)
                            )
                            .mappings()
                            .first()
                        )
                        if row is None:
                            raise ValueError(
                                "Måltidet som skulle oppdateres ble fjernet; forhåndsvis på nytt."
                            )
                        current = Meal.model_validate(dict(row))
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
                        )
                        values = entity_values(updated_meal)
                        values.pop("id")
                        connection.execute(
                            update(meals).where(meals.c.id == item.kitchen_meal_id).values(**values)
                        )
                        item.status = "applied"
                        item.applied_meal_id = item.kitchen_meal_id
                        item.kitchen_recipe_id = recipe_id
                        self.history.record(
                            HistoryEntryCreate(
                                kind="planning",
                                message="Oppdaterte måltid fra Cookidoo Min uke",
                                recipe_id=recipe_id,
                                structured={
                                    "provider": "cookidoo",
                                    "direction": "cookidoo_to_kitchen",
                                    "action": "edit",
                                    "meal_id": str(item.kitchen_meal_id),
                                    "cookidoo_recipe_id": item.cookidoo_recipe_id,
                                    "source_url": item.source_url,
                                    "date": item.date.isoformat(),
                                },
                            ),
                            actor_id=actor_id,
                            connection=connection,
                        )

        await asyncio.to_thread(_commit)

    async def _apply_kitchen_to_cookidoo(
        self,
        actor_id: str,
        job: _SyncJob,
        selected_items: list[SyncPreviewItem],
        selected_set: set[str],
    ) -> None:
        del selected_set
        live_mw = await self.provider.get_my_week_plan(job.preview.start, job.preview.end)
        live_entry_ids = {entry.external_entry_id for entry in live_mw if entry.external_entry_id}
        for item in selected_items:
            if item.action == "edit":
                if not item.external_entry_id or item.external_entry_id not in live_entry_ids:
                    raise ValueError(
                        "Cookidoo-oppføringen ble endret eller fjernet før bekreftelse; forhåndsvis på nytt."
                    )

        for item in selected_items:
            written = await self.provider.add_to_my_week(
                item.cookidoo_recipe_id,
                item.date,
                servings=item.servings,
                label=item.label,
                entry_id=item.external_entry_id if item.action == "edit" else None,
                read_before_write=False,
            )
            item.status = "applied"
            item.applied_external_entry_id = written.external_entry_id

        def _record_history() -> None:
            with self.kitchen.store.engine.begin() as connection:
                self.provider.lock_generation(connection, actor_id, job.generation)
                for item in selected_items:
                    self.history.record(
                        HistoryEntryCreate(
                            kind="planning",
                            message="Synkroniserte måltid til Cookidoo Min uke",
                            recipe_id=item.kitchen_recipe_id,
                            structured={
                                "provider": "cookidoo",
                                "direction": "kitchen_to_cookidoo",
                                "action": item.action,
                                "meal_id": str(item.kitchen_meal_id)
                                if item.kitchen_meal_id
                                else None,
                                "cookidoo_recipe_id": item.cookidoo_recipe_id,
                                "source_url": item.source_url,
                                "date": item.date.isoformat(),
                                "external_entry_id": item.applied_external_entry_id,
                            },
                        ),
                        actor_id=actor_id,
                        connection=connection,
                    )

        await asyncio.to_thread(_record_history)
