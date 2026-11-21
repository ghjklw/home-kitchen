"""Oda adapters implementing recipe collection and discovery ports."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import HttpUrl
from sqlalchemy import select

from home_kitchen.imports import validate_url
from home_kitchen.recipes.adapters.discovery import _safe_url
from home_kitchen.recipes.collections import (
    LABELS,
    CollectionEntry,
    CollectionKind,
    CollectionPage,
)
from home_kitchen.recipes.discovery import (
    DiscoveryContext,
    ProviderSearch,
    RecipeDiscoveryItem,
)
from home_kitchen.recipes.ports import (
    OdaCollectionClient,
    OdaGenerationLocker,
    OdaRecipeSearchClient,
)


class OdaCollectionProvider:
    """`CollectionProvider` implementation backed by an `OdaCollectionClient` port."""

    id = "oda"

    def __init__(
        self,
        oda: OdaCollectionClient,
        *,
        generation_locker: OdaGenerationLocker | None = None,
    ) -> None:
        self.oda = oda
        self._generation_locker = generation_locker

    def generation(self, actor_id: str) -> object:
        generation = self.oda.search_cache_context(actor_id)
        if generation is None or not self.oda.status(actor_id).get("connected"):
            raise ValueError("Connect Oda before reading recipe collections")
        return generation

    async def page(
        self, actor_id: str, kind: CollectionKind, page: int, size: int
    ) -> CollectionPage:
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

    def lock_generation(self, connection: Any, actor_id: str, generation: object) -> None:
        if self._generation_locker is not None:
            self._generation_locker.lock_generation(connection, actor_id, generation)
            return
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

    def validate_source(self, entry: CollectionEntry) -> None:
        if entry.source_url is None:
            raise ValueError("This Oda recipe has no public source URL")
        validate_url(entry.source_url)
        parsed = urlparse(entry.source_url)
        match = re.fullmatch(r"/no/recipes/([1-9][0-9]*)-[a-z0-9]+(?:-[a-z0-9]+)*/?", parsed.path)
        if parsed.hostname not in {"oda.com", "www.oda.com"} or not match or match[1] != entry.id:
            raise ValueError("Oda source URL does not match the listed recipe identity")


class OdaDiscoveryProvider:
    """`DiscoveryProvider` implementation backed by an `OdaRecipeSearchClient` port."""

    source: Literal["oda"] = "oda"
    timeout_seconds = 20.0

    def __init__(self, service: OdaRecipeSearchClient) -> None:
        self.service = service

    async def search(
        self,
        query: str,
        limit: int,
        actor_id: str | None = None,
        *,
        context: DiscoveryContext | None = None,
    ) -> ProviderSearch:
        del context
        if actor_id is None or not self.service.status(actor_id).get("connected"):
            return ProviderSearch(
                status="connection_required",
                detail="Connect your Oda account to search Oda recipes.",
            )
        payload = await self.service.search_recipes(actor_id, query, page=1, size=min(limit, 20))
        results = []
        for recipe in payload.get("recipes", []):
            title = recipe.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            raw_url = recipe.get("url")
            recipe_url = None
            if isinstance(raw_url, str):
                try:
                    validate_url(raw_url)
                    recipe_url = HttpUrl(raw_url)
                except (TypeError, ValueError):
                    pass
            raw_image = recipe.get("imageUrl")
            image = _safe_url(
                raw_image if isinstance(raw_image, str) else None,
                {"oda.com", "www.oda.com", "images.oda.com"},
                "https://oda.com/",
            )
            provider_name = recipe.get("providerName")
            duration = recipe.get("cookingDurationIso8601")
            difficulty = recipe.get("difficultyString")
            results.append(
                RecipeDiscoveryItem(
                    source="oda",
                    title=title.strip()[:300],
                    url=recipe_url,
                    image_url=image,
                    source_attribution=(provider_name.strip()[:200] if provider_name else "Oda"),
                    duration=duration.strip()[:100] if duration else None,
                    difficulty=difficulty.strip()[:100] if difficulty else None,
                    importable=recipe_url is not None,
                    detail=None
                    if recipe_url
                    else "Oda did not provide a supported public recipe link.",
                )
            )
        return ProviderSearch(results=results[:limit])
