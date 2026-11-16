"""Deterministic, network-free providers for test scenarios."""

from copy import deepcopy
from typing import Any

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


def fresh_fixture_value(value: Any) -> Any:
    """Return an independent mutable fixture copy for test isolation."""
    return deepcopy(value)
