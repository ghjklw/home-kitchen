"""Private photo persistence and explicitly requested transcription proposals."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlalchemy import Column, DateTime, Integer, LargeBinary, Table, Text, Uuid, select
from sqlalchemy.engine import Engine

from home_kitchen.models import PhotoSource
from home_kitchen.recipes.models import EditableRecipeDraft, RecipeContext
from home_kitchen.recipes.photos import (
    PhotoExtraction,
    ValidatedPhoto,
    extract_photo,
    photo_agent,
    validate_photo,
)
from home_kitchen.settings import Household, Settings
from home_kitchen.store import metadata

recipe_photos = Table(
    "recipe_photos",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("household_id", Uuid, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("mime_type", Text, nullable=False),
    Column("width", Integer, nullable=False),
    Column("height", Integer, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("book_title", Text),
    Column("page", Text),
    Column("created_by", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# All service instances share the decoder admission limit. No waiting queue holds images.
_PROCESSING = threading.Lock()


class StoredPhoto(ValidatedPhoto):
    id: UUID
    household_id: UUID
    sha256: str
    book_title: str | None = None
    page: str | None = None
    created_by: str
    created_at: datetime

    @property
    def photo_source(self) -> PhotoSource:
        return PhotoSource(id=self.id, book_title=self.book_title, page=self.page)


class PhotoAcquisition(BaseModel):
    photo_source: PhotoSource
    draft: EditableRecipeDraft | None = None
    uncertainties: list[str] = Field(default_factory=list)
    status: Literal["draft_ready", "extraction_failed"]


class PhotoRecipeService:
    def __init__(
        self,
        settings: Settings,
        household_id: UUID,
        engine: Engine,
        household: Callable[[], Household],
        agent: Agent[None, PhotoExtraction] | None = None,
    ):
        self.settings = settings
        self.household_id = household_id
        self.engine = engine
        self.household = household
        self.agent = agent

    async def create(
        self,
        data: bytes,
        mime_type: str,
        actor_id: str,
        *,
        book_title: str | None = None,
        page: str | None = None,
    ) -> PhotoAcquisition:
        self._actor(actor_id)
        # Validate attribution before decoding or persistence.
        source = PhotoSource(id=uuid4(), book_title=book_title, page=page)
        if not _PROCESSING.acquire(blocking=False):
            raise ValueError("Photo processing is busy; try again shortly")
        try:
            photo = await self._offload(validate_photo, data, mime_type)
            stored = StoredPhoto(
                **photo.model_dump(),
                **source.model_dump(),
                household_id=self.household_id,
                sha256=hashlib.sha256(photo.data).hexdigest(),
                created_by=actor_id,
                created_at=datetime.now(UTC),
            )
            await self._offload(self._save, stored)
            return await self._extract(stored, actor_id)
        finally:
            _PROCESSING.release()

    async def get(self, photo_id: UUID) -> StoredPhoto:
        photo_id = UUID(str(photo_id))
        return await self._offload(self._get, photo_id)

    async def redraft(self, photo_id: UUID, actor_id: str) -> PhotoAcquisition:
        self._actor(actor_id)
        photo_id = UUID(str(photo_id))
        if not _PROCESSING.acquire(blocking=False):
            raise ValueError("Photo processing is busy; try again shortly")
        try:
            stored = await self._offload(self._get, photo_id)
            return await self._extract(stored, actor_id)
        finally:
            _PROCESSING.release()

    @staticmethod
    def _actor(actor_id: str) -> None:
        if not actor_id or not actor_id.strip():
            raise ValueError("Actor identity is required")

    @staticmethod
    async def _offload(function, *args):
        # Cancellation cannot release decoder admission while its thread still runs.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _save(self, photo: StoredPhoto) -> None:
        with self.engine.begin() as connection:
            connection.execute(recipe_photos.insert().values(**photo.model_dump()))

    def _get(self, photo_id: UUID) -> StoredPhoto:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(recipe_photos).where(
                        recipe_photos.c.id == photo_id,
                        recipe_photos.c.household_id == self.household_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(str(photo_id))
        return StoredPhoto.model_validate(dict(row))

    async def _extract(self, stored: StoredPhoto, actor_id: str) -> PhotoAcquisition:
        try:
            extraction = await extract_photo(
                stored,
                RecipeContext(household=self.household(), actor_id=actor_id),
                agent=self.agent if self.agent is not None else photo_agent(self.settings),
            )
            draft = extraction.draft.model_copy(update={"photo_source": stored.photo_source})
            return PhotoAcquisition(
                photo_source=stored.photo_source,
                draft=draft,
                uncertainties=extraction.uncertainties,
                status="draft_ready",
            )
        except Exception:
            # Provider errors and source text must never be exposed through failure messages.
            return PhotoAcquisition(
                photo_source=stored.photo_source,
                status="extraction_failed",
                uncertainties=["Could not transcribe this photo. Review it or retry extraction."],
            )
