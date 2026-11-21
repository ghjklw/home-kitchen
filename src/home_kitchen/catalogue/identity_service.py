"""Explicit identity creation, scoped alias lookup, and reviewed corrections."""

from typing import Protocol
from uuid import UUID

from home_kitchen.catalogue.identity import (
    AliasResolution,
    AliasSourceType,
    CanonicalProduct,
    CanonicalUnit,
    IdentityCorrectionEvent,
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
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        actor_id: str = "system",
        household_id: UUID | None = None,
        channel: str = "api",
        source_type: str = "reviewed_correction",
    ) -> IdentityCorrectionEvent | None: ...
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
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        household_id: UUID | None = None,
        source_type: AliasSourceType = "automatic_suggestion",
    ) -> None:
        norm_locale = normalize_identity_text(locale)
        norm_context = normalize_identity_text(context)
        clean_alias = alias.strip()
        try:
            self.repository.add_alias(
                kind,
                norm_locale,
                norm_context,
                clean_alias,
                identity_id,
                household_id=household_id,  # ty: ignore[unknown-argument]
                source_type=source_type,  # ty: ignore[unknown-argument]
            )
        except TypeError:
            try:
                self.repository.add_alias(
                    kind,
                    norm_locale,
                    norm_context,
                    clean_alias,
                    identity_id,
                    source_type=source_type,  # ty: ignore[unknown-argument]
                )
            except TypeError:
                self.repository.add_alias(
                    kind,
                    norm_locale,
                    norm_context,
                    clean_alias,
                    identity_id,
                )

    def correct_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        actor_id: str = "system",
        household_id: UUID | None = None,
        channel: str = "api",
        source_type: AliasSourceType = "reviewed_correction",
    ) -> IdentityCorrectionEvent | None:
        norm_locale = normalize_identity_text(locale)
        norm_context = normalize_identity_text(context)
        clean_alias = alias.strip()
        try:
            return self.repository.correct_alias(
                kind,
                norm_locale,
                norm_context,
                clean_alias,
                identity_id,
                actor_id=actor_id,
                household_id=household_id,
                channel=channel,
                source_type=source_type,
            )
        except TypeError:
            self.repository.correct_alias(
                kind,
                norm_locale,
                norm_context,
                clean_alias,
                identity_id,
            )
            return None

    def list_correction_events(
        self,
        *,
        kind: str | None = None,
        alias: str | None = None,
        household_id: UUID | None = None,
        limit: int = 50,
    ) -> list[IdentityCorrectionEvent]:
        lister = getattr(self.repository, "list_correction_events", None)
        if not callable(lister):
            return []
        result = lister(kind=kind, alias=alias, household_id=household_id, limit=limit)
        return result if isinstance(result, list) else []

    def resolve(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> AliasResolution:
        norm_locale = normalize_identity_text(locale)
        norm_context = normalize_identity_text(context)
        resolver_with_meta = getattr(self.repository, "resolve_with_metadata", None)
        if callable(resolver_with_meta):
            res = resolver_with_meta(
                kind,
                norm_locale,
                norm_context,
                alias,
                household_id=household_id,
            )
            if isinstance(res, AliasResolution):
                return res
        try:
            candidates = self.repository.resolve(
                kind,
                norm_locale,
                norm_context,
                alias,
                household_id=household_id,  # ty: ignore[unknown-argument]
            )
        except TypeError:
            candidates = self.repository.resolve(kind, norm_locale, norm_context, alias)
        status = (
            "unmapped" if not candidates else "resolved" if len(candidates) == 1 else "ambiguous"
        )
        return AliasResolution(status=status, candidates=candidates)

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        self.repository.validate_identities(kind, ids)

    def has_reviewed_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> bool:
        checker = getattr(self.repository, "has_reviewed_alias", None)
        if callable(checker):
            norm_locale = normalize_identity_text(locale)
            norm_context = normalize_identity_text(context)
            clean_alias = alias.strip()
            try:
                return bool(
                    checker(
                        kind,
                        norm_locale,
                        norm_context,
                        clean_alias,
                        household_id=household_id,
                    )
                )
            except TypeError:
                return bool(checker(kind, norm_locale, norm_context, clean_alias))
        return False
