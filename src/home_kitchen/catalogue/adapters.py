"""Adapters exposing catalogue capabilities through consumer-defined ports."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from home_kitchen.catalogue import ai_mapper
from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_receipt_items_async
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.context import history_context


class CatalogueReceiptMappingAdapter:
    """Adapts catalogue AI receipt mapping to purchase sync's ReceiptMappingPort."""

    def __init__(
        self,
        store: Any,
        identity_service: CanonicalIdentityService,
    ) -> None:
        self.store = store
        self.identity_service = identity_service

    async def map_unmapped_items(
        self,
        items: list[dict[str, Any]],
        *,
        settings: Any,
    ) -> dict[str, int]:
        return await auto_map_unmapped_receipt_items_async(
            self.store,
            settings,
            items,
            identity_service=self.identity_service,
        )


class CatalogueRecipeAutoMapperAdapter:
    """Adapts catalogue AI recipe ingredient mapping to RecipeAutoMapperPort."""

    def __init__(
        self,
        mapping_repo: Any,
        *,
        settings_provider: Callable[[], Any] | None = None,
        household_id_provider: Callable[[], UUID | None] | None = None,
        identity_service: CanonicalIdentityService | None = None,
    ) -> None:
        self._mapping_repo = mapping_repo
        self._settings_provider = settings_provider
        self._household_id_provider = household_id_provider
        self._identity_service = identity_service

    def auto_map_recipe(
        self,
        recipe_id: UUID,
        *,
        excluded_fields: dict[tuple[UUID, int], set[str]] | None = None,
    ) -> int:
        settings = self._settings_provider() if self._settings_provider is not None else None
        if settings is None or not settings.ai_configured:
            return 0
        household_id = (
            self._household_id_provider() if self._household_id_provider is not None else None
        )
        summary = ai_mapper.auto_map_unmapped_ingredients(
            self._mapping_repo,
            settings,
            household_id=household_id,
            actor_id=history_context.get().actor_id or "system",
            recipe_id=recipe_id,
            identity_service=self._identity_service,
            excluded_fields=excluded_fields,
        )
        return int(summary.get("mapped_occurrences", 0))
