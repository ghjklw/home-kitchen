"""Adapters exposing catalogue capabilities through consumer-defined ports."""

from __future__ import annotations

from typing import Any

from home_kitchen.catalogue.ai_mapper import auto_map_unmapped_receipt_items_async
from home_kitchen.catalogue.identity_service import CanonicalIdentityService


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
