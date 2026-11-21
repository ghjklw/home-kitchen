"""Deterministic substring autocomplete for the small shared food catalogue."""

from typing import Protocol
from uuid import UUID

from home_kitchen.catalogue.models import FoodCatalogueItem


class FoodCatalogueRepository(Protocol):
    def list_all(self) -> list[FoodCatalogueItem]: ...


class FoodCatalogueService:
    def __init__(self, repository: FoodCatalogueRepository):
        self.repository = repository

    def get(self, item_id: UUID) -> FoodCatalogueItem | None:
        return next((item for item in self.repository.list_all() if item.id == item_id), None)

    def list_foods(self, limit: int = 37) -> list[FoodCatalogueItem]:
        """Return deterministic seeded foods for empty-query catalogue browsing."""
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        items = sorted(
            self.repository.list_all(),
            key=lambda item: (
                " ".join(item.display_name.split()).casefold(),
                item.source_item_id,
                str(item.id),
            ),
        )
        return items[:limit]

    def autocomplete(self, query: str, limit: int = 10) -> list[FoodCatalogueItem]:
        """Search names and source keywords; never equate or merge separate foods."""
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        needle = " ".join(query.split()).casefold()
        if not needle:
            return []

        matches: list[tuple[tuple[int, int, str, str], FoodCatalogueItem]] = []
        for item in self.repository.list_all():
            display_name = " ".join(item.display_name.split()).casefold()
            source_name = " ".join(item.source_name.split()).casefold()
            display_position = display_name.find(needle)
            source_position = source_name.find(needle)
            keyword_match = any(needle in keyword.casefold() for keyword in item.search_keywords)
            if display_position < 0 and source_position < 0 and not keyword_match:
                continue
            # Display-name matches rank ahead of source-name and keyword matches.
            if display_position >= 0:
                rank = (
                    0 if display_position == 0 else 1,
                    display_position,
                    display_name,
                    str(item.id),
                )
            elif source_position >= 0:
                rank = (2, source_position, display_name, str(item.id))
            else:
                rank = (3, len(display_name), display_name, str(item.id))
            matches.append((rank, item))

        matches.sort(key=lambda match: match[0])
        return [item for _, item in matches[:limit]]
