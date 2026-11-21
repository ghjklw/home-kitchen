"""Secondary ports for basket matching against catalogue preferences and identities."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from home_kitchen.catalogue.identity import IdentityCorrectionCitation


class SkuPreferencePort(Protocol):
    """Port for reading preferred and excluded provider SKUs for a canonical product."""

    def get_sku_preferences(self, product_id: UUID) -> tuple[list[str], list[str]]: ...


class IdentityLookupPort(Protocol):
    """Port for resolving a purchase need name to a canonical product ID."""

    def resolve_product_id(self, name: str) -> UUID | None: ...


class IdentityCorrectionPort(Protocol):
    """Port for retrieving identity correction citations for a purchase need."""

    def get_identity_corrections(
        self, need_name: str, product_id: UUID | None
    ) -> list[IdentityCorrectionCitation]: ...
