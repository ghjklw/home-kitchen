"""Explicit identity creation, scoped alias lookup, and reviewed corrections."""

from typing import Protocol
from uuid import UUID

from home_kitchen.catalogue.identity import (
    AliasResolution,
    CanonicalProduct,
    CanonicalUnit,
    normalize_identity_text,
)


class IdentityRepository(Protocol):
    def product(self, canonical_name: str, variant: str) -> CanonicalProduct: ...
    def unit(self, canonical_name: str) -> CanonicalUnit: ...
    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None: ...
    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None: ...
    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]: ...


class CanonicalIdentityService:
    def __init__(self, repository: IdentityRepository):
        self.repository = repository

    def create_product(self, name: str, variant: str = "") -> CanonicalProduct:
        return self.repository.product(
            normalize_identity_text(name), normalize_identity_text(variant)
        )

    def create_unit(self, name: str) -> CanonicalUnit:
        return self.repository.unit(normalize_identity_text(name))

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        self.repository.add_alias(
            kind,
            normalize_identity_text(locale),
            normalize_identity_text(context),
            alias.strip(),
            identity_id,
        )

    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        self.repository.correct_alias(
            kind,
            normalize_identity_text(locale),
            normalize_identity_text(context),
            alias.strip(),
            identity_id,
        )

    def resolve(self, kind: str, locale: str, context: str, alias: str) -> AliasResolution:
        candidates = self.repository.resolve(
            kind, normalize_identity_text(locale), normalize_identity_text(context), alias
        )
        status = (
            "unmapped" if not candidates else "resolved" if len(candidates) == 1 else "ambiguous"
        )
        return AliasResolution(status=status, candidates=candidates)
