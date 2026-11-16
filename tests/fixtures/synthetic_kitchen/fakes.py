"""Deterministic, network-free providers for test scenarios."""

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from home_kitchen.catalogue.identity import (
    CanonicalProduct,
    CanonicalUnit,
    normalize_identity_text,
)

from .identity import (
    SyntheticAlias,
    synthetic_canonical_products,
    synthetic_canonical_units,
    synthetic_reviewed_aliases,
    synthetic_unreviewed_aliases,
)
from .oda import (
    synthetic_oda_product_search_response,
    synthetic_oda_recipe_collection_response,
)
from .purchase_history import SyntheticHistoryFailure, SyntheticSourcePage


class StaticOdaClient:
    """Satisfy the app adapter boundary from fixture data; never makes HTTP calls."""

    def __init__(self) -> None:
        self.search_calls: list[tuple[str, tuple[str, ...], int, int]] = []
        self.collection_calls: list[tuple[str, str, int, int]] = []
        self.network_calls = 0

    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> dict[str, Any]:
        self.search_calls.append((actor_id, tuple(queries), page, size))
        return deepcopy(synthetic_oda_product_search_response())

    async def list_liked_recipes(self, actor_id: str, page: int, size: int) -> dict[str, Any]:
        self.collection_calls.append((actor_id, "liked", page, size))
        return deepcopy(synthetic_oda_recipe_collection_response())

    async def list_purchased_recipes(self, actor_id: str, page: int, size: int) -> dict[str, Any]:
        self.collection_calls.append((actor_id, "purchased", page, size))
        return deepcopy(synthetic_oda_recipe_collection_response())


class SyntheticReaderFailure(RuntimeError):
    """Local fake-reader failure; it carries no provider status or response text."""

    def __init__(self, scenario: SyntheticHistoryFailure) -> None:
        self.provider = scenario.provider
        self.kind = scenario.kind
        super().__init__(f"synthetic {scenario.provider} {scenario.kind} scenario")


class ScriptedHistoryReader:
    """Return fresh fixture pages or a scripted failure without network access."""

    def __init__(
        self,
        pages: tuple[SyntheticSourcePage, ...],
        failure: SyntheticHistoryFailure | None = None,
    ) -> None:
        self._pages = pages
        self._failure = failure
        self.calls = 0
        self.network_calls = 0

    def page(self, index: int) -> SyntheticSourcePage:
        self.calls += 1
        if self._failure is not None:
            raise SyntheticReaderFailure(self._failure)
        return deepcopy(self._pages[index])


class StaticIdentityRepository:
    """Network-free, in-memory repository for canonical identity and alias testing (#98)."""

    def __init__(
        self,
        products: list[CanonicalProduct] | None = None,
        units: list[CanonicalUnit] | None = None,
        aliases: list[SyntheticAlias] | None = None,
    ) -> None:
        self.network_calls = 0
        self._products: dict[tuple[str, str], CanonicalProduct] = {}
        self._units: dict[str, CanonicalUnit] = {}
        self._aliases: dict[tuple[str, str, str, str], set[UUID]] = {}
        self.reviewed: set[tuple[str, str, str, str]] = set()

        seed_products = products if products is not None else synthetic_canonical_products()
        for prod in seed_products:
            norm_name = normalize_identity_text(prod.canonical_name)
            norm_var = normalize_identity_text(prod.variant)
            self._products[(norm_name, norm_var)] = deepcopy(prod)

        seed_units = units if units is not None else synthetic_canonical_units()
        for u in seed_units:
            norm_name = normalize_identity_text(u.canonical_name)
            self._units[norm_name] = deepcopy(u)

        seed_aliases = (
            aliases
            if aliases is not None
            else (synthetic_reviewed_aliases() + synthetic_unreviewed_aliases())
        )
        for al in seed_aliases:
            key = (
                normalize_identity_text(al.kind),
                normalize_identity_text(al.locale),
                normalize_identity_text(al.context),
                normalize_identity_text(al.alias),
            )
            self._aliases.setdefault(key, set()).add(al.identity_id)
            if al.reviewed:
                self.reviewed.add(key)

    def product(self, canonical_name: str, variant: str = "") -> CanonicalProduct:
        norm_name = normalize_identity_text(canonical_name)
        norm_var = normalize_identity_text(variant)
        return self._products.setdefault(
            (norm_name, norm_var),
            CanonicalProduct(id=uuid4(), canonical_name=norm_name, variant=norm_var),
        )

    def unit(self, canonical_name: str) -> CanonicalUnit:
        norm_name = normalize_identity_text(canonical_name)
        return self._units.setdefault(
            norm_name,
            CanonicalUnit(id=uuid4(), canonical_name=norm_name),
        )

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        self._require_identity(kind, identity_id)
        key = (
            normalize_identity_text(kind),
            normalize_identity_text(locale),
            normalize_identity_text(context),
            normalize_identity_text(alias),
        )
        self._aliases.setdefault(key, set()).add(identity_id)

    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        self._require_identity(kind, identity_id)
        key = (
            normalize_identity_text(kind),
            normalize_identity_text(locale),
            normalize_identity_text(context),
            normalize_identity_text(alias),
        )
        self._aliases[key] = {identity_id}
        self.reviewed.add(key)

    def _require_identity(self, kind: str, identity_id: UUID) -> None:
        if kind == "product":
            if not any(prod.id == identity_id for prod in self._products.values()):
                raise ValueError(f"Unknown canonical {kind} identity")
        elif kind == "unit":
            if not any(u.id == identity_id for u in self._units.values()):
                raise ValueError(f"Unknown canonical {kind} identity")
        else:
            raise ValueError("kind must be product or unit")

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        norm_kind = normalize_identity_text(kind)
        norm_locale = normalize_identity_text(locale)
        norm_context = normalize_identity_text(context)
        norm_alias = normalize_identity_text(alias)

        ids = self._aliases.get(
            (norm_kind, norm_locale, norm_context, norm_alias), set()
        ) | self._aliases.get((norm_kind, norm_locale, "", norm_alias), set())

        if norm_kind == "product":
            products = [prod for prod in self._products.values() if prod.id in ids]
            return sorted(products, key=lambda p: (p.canonical_name, str(p.id)))
        if norm_kind == "unit":
            units = [u for u in self._units.values() if u.id in ids]
            return sorted(units, key=lambda u: (u.canonical_name, str(u.id)))
        return []


def fresh_fixture_value(value: Any) -> Any:
    """Return an independent mutable fixture copy for test isolation."""
    return deepcopy(value)
