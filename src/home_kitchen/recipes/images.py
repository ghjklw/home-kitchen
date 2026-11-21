"""Provider-neutral recipe image search sessions and explicit image selection."""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, StringConstraints

from home_kitchen.models import Recipe, RecipeImageAttribution
from home_kitchen.recipes.ports import RecipeImageRepository

MAX_QUERY_LENGTH = 200
MAX_RESULTS = 10
MAX_CONTINUATION_LENGTH = 300

ImageQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_LENGTH)
]


class RecipeImage(BaseModel):
    """A candidate only; selecting or persisting it is the caller's responsibility."""

    provider: Literal["wikimedia_commons"] = "wikimedia_commons"
    provider_id: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    title: str = Field(min_length=1, max_length=300)
    image_url: str
    thumbnail_url: str
    source_page_url: str
    artist: str | None = Field(default=None, max_length=500)
    credit: str | None = Field(default=None, max_length=500)
    license_name: str = Field(min_length=1, max_length=100)
    license_url: str
    attribution_required: bool
    attribution: str | None = Field(default=None, max_length=500)
    obligations: list[str] = Field(default_factory=list, max_length=4)


class RecipeImageSearch(BaseModel):
    status: Literal[
        "ok", "no_usable_results", "rate_limited", "unavailable", "invalid_continuation"
    ]
    detail: str | None = Field(default=None, max_length=300)
    results: list[RecipeImage] = Field(default_factory=list, max_length=MAX_RESULTS)
    continuation: str | None = Field(default=None, max_length=MAX_CONTINUATION_LENGTH)


class ImageSearchProvider(Protocol):
    async def search(
        self, query: str, *, limit: int = MAX_RESULTS, continuation: str | None = None
    ) -> RecipeImageSearch: ...


class RecipeImageSearchSession(BaseModel):
    search_id: UUID
    search: RecipeImageSearch


class _SearchSession:
    def __init__(self, actor_id: str, result: RecipeImageSearch):
        self.actor_id = actor_id
        self.result = result
        self.created = time.monotonic()


class RecipeImageService:
    """Actor-scoped image search sessions and explicit image selection."""

    MAX_SESSIONS = 32
    TTL_SECONDS = 15 * 60

    def __init__(
        self,
        repository: RecipeImageRepository,
        provider: ImageSearchProvider | None = None,
    ):
        self.repository = repository
        if provider is None:
            from home_kitchen.recipes.adapters.wikimedia import WikimediaCommonsImageProvider

            self.provider: ImageSearchProvider = WikimediaCommonsImageProvider()
        else:
            self.provider = provider
        self._sessions: dict[UUID, _SearchSession] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for session_id, session in list(self._sessions.items()):
            if now - session.created >= self.TTL_SECONDS:
                del self._sessions[session_id]
        while len(self._sessions) >= self.MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].created)
            del self._sessions[oldest]

    async def search(
        self,
        actor_id: str,
        query: str,
        *,
        limit: int = MAX_RESULTS,
        continuation: str | None = None,
    ) -> RecipeImageSearchSession:
        self._actor(actor_id)
        result = await self.provider.search(query, limit=limit, continuation=continuation)
        self._prune()
        search_id = uuid4()
        if result.status == "ok" and result.results:
            self._sessions[search_id] = _SearchSession(actor_id, result)
        return RecipeImageSearchSession(search_id=search_id, search=result)

    def select(
        self,
        actor_id: str,
        recipe_id: UUID,
        search_id: UUID,
        provider_id: str,
        *,
        reviewed: bool = False,
    ) -> Recipe:
        self._actor(actor_id)
        if reviewed is not True:
            raise ValueError("Review and explicitly confirm the selected image before applying it")
        self._prune()
        session = self._sessions.get(UUID(str(search_id)))
        if session is None or session.actor_id != actor_id:
            raise KeyError("Image search unavailable; search again before selecting an image")
        candidate = next(
            (item for item in session.result.results if item.provider_id == provider_id), None
        )
        if candidate is None:
            raise ValueError("Select an exact candidate from this image search")
        attribution = RecipeImageAttribution.model_validate(
            candidate.model_dump(exclude={"thumbnail_url"})
        )
        return self.repository.save_selected_image(
            actor_id=actor_id,
            recipe_id=recipe_id,
            image_url=str(candidate.image_url),
            attribution=attribution,
            provider=candidate.provider,
            provider_id=candidate.provider_id,
            source_page_url=candidate.source_page_url,
            license_name=candidate.license_name,
            license_url=candidate.license_url,
        )

    @staticmethod
    def _actor(actor_id: str) -> None:
        if not actor_id or not actor_id.strip():
            raise ValueError("Household authentication required")


def __getattr__(name: str) -> Any:
    if name in {
        "COMMONS_API",
        "HTTP_TIMEOUT_SECONDS",
        "MAX_JSON_DEPTH",
        "MAX_JSON_NODES",
        "MAX_RESPONSE_BYTES",
        "USER_AGENT",
        "WikimediaCommonsImageProvider",
    }:
        from home_kitchen.recipes.adapters import wikimedia as _wikimedia

        return getattr(_wikimedia, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
