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
    def product(
        self, canonical_name: str, variant: str, identity_id: UUID | None = None
    ) -> CanonicalProduct: ...
    def unit(self, canonical_name: str, identity_id: UUID | None = None) -> CanonicalUnit: ...
    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None: ...
    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None: ...
    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]: ...
    def validate_identities(self, kind: str, ids: set[UUID]) -> None: ...


class CanonicalIdentityService:
    def __init__(self, repository: IdentityRepository):
        self.repository = repository

    def create_product(
        self, name: str, variant: str = "", identity_id: UUID | None = None
    ) -> CanonicalProduct:
        try:
            return self.repository.product(
                normalize_identity_text(name),
                normalize_identity_text(variant),
                identity_id=identity_id,
            )
        except TypeError:
            return self.repository.product(
                normalize_identity_text(name), normalize_identity_text(variant)
            )

    def create_unit(self, name: str, identity_id: UUID | None = None) -> CanonicalUnit:
        try:
            return self.repository.unit(normalize_identity_text(name), identity_id=identity_id)
        except TypeError:
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

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        self.repository.validate_identities(kind, ids)

    def has_reviewed_alias(self, kind: str, locale: str, context: str, alias: str) -> bool:
        checker = getattr(self.repository, "has_reviewed_alias", None)
        if callable(checker):
            return bool(
                checker(
                    kind,
                    normalize_identity_text(locale),
                    normalize_identity_text(context),
                    alias.strip(),
                )
            )
        return False
