"""Read-only product matching for the basket review workflow.

Provider results stay as candidates until a person selects one through the
existing basket review flow. Product-page pack text is treated as unverified
metadata and only a complete, unambiguous measurement can support a quantity.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.concurrency import run_in_threadpool

from home_kitchen.inventory import InventoryService
from home_kitchen.oda.media import ProductMedia, fetch_product_media
from home_kitchen.oda.models import OdaSearchResponse
from home_kitchen.oda.service import OdaService
from home_kitchen.units import (
    canonicalize_quantity,
    compatible_units,
    multiply_integer_exact,
    normalize_unit,
)

MAX_NEEDS = 100
MAX_RESULTS_PER_NEED = 5
MAX_MEDIA_FETCHES = 30
MAX_CONCURRENT_SEARCHES = 3
MAX_CONCURRENT_MEDIA = 3
SEARCH_TIMEOUT_SECONDS = 15
MEDIA_TIMEOUT_SECONDS = 8
MEDIA_CACHE_TTL = timedelta(minutes=15)
MEDIA_CACHE_MAX_ENTRIES = 256


class BasketCandidate(BaseModel):
    """A provider result with explicit unknowns kept unknown."""

    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    product_id: str
    name: str
    description: str
    brand: str | None = None
    product_url: HttpUrl | None = None
    price: str | None = None
    unit_price: str | None = None
    unit_name: str | None = None
    available: bool | None = None
    media: ProductMedia | None = None
    pack_description: str | None = None
    pack_quantity: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    pack_unit: str | None = None
    pack_metadata_status: Literal["unknown", "unverified"] = "unknown"
    match_query: str
    match_reason: str
    recommended_packages: int | None = Field(default=None, gt=0)
    recommended_fulfillment_quantity: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )


class ProviderProduct(BaseModel):
    """Provider-neutral fields needed by matching and the review UI."""

    provider: str
    product_id: str
    name: str
    description: str
    brand: str | None = None
    product_url: HttpUrl | None = None
    price: str | None = None
    unit_price: str | None = None
    unit_name: str | None = None
    available: bool | None = None


class ProviderSearchGroup(BaseModel):
    query: str
    has_more: bool
    products: list[ProviderProduct]


class NeedMatchResult(BaseModel):
    need_id: UUID
    name: str
    quantity: Decimal
    unit: str
    page: int
    has_more: bool
    candidates: list[BasketCandidate]
    status: str = "matched"
    detail: str | None = None


class BasketMatchResult(BaseModel):
    start: date
    end: date
    needs: list[NeedMatchResult]
    skipped_need_count: int = 0


class SearchProvider(Protocol):
    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> list[ProviderSearchGroup]:
        """Search a bounded batch and return normalized provider candidates."""


class OdaSearchAdapter:
    """Normalize the official Oda response to the provider-neutral protocol."""

    def __init__(self, service: OdaService):
        self.service = service

    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> list[ProviderSearchGroup]:
        response = OdaSearchResponse.model_validate(
            await self.service.search_products(actor_id, queries, page, size)
        )
        return [
            ProviderSearchGroup(
                query=group.query,
                has_more=group.has_more,
                products=[
                    ProviderProduct(
                        provider="oda",
                        product_id=str(product.id),
                        name=product.name,
                        description=product.description,
                        brand=product.brand,
                        product_url=product.url,
                        price=product.price,
                        unit_price=product.unit_price,
                        unit_name=product.unit_name,
                        available=(
                            product.availability.is_available if product.availability else None
                        ),
                    )
                    for product in group.products
                ],
            )
            for group in response.result
        ]


MediaFetcher = Callable[[str], Awaitable[ProductMedia]]


class _MediaCache:
    def __init__(
        self, *, ttl: timedelta = MEDIA_CACHE_TTL, max_entries: int = MEDIA_CACHE_MAX_ENTRIES
    ):
        self.ttl = ttl
        self.max_entries = max_entries
        self._values: OrderedDict[str, tuple[datetime, ProductMedia]] = OrderedDict()

    def get(self, url: str, now: datetime) -> ProductMedia | None:
        entry = self._values.get(url)
        if entry is None:
            return None
        expires_at, media = entry
        if expires_at <= now:
            del self._values[url]
            return None
        self._values.move_to_end(url)
        return media

    def put(self, url: str, media: ProductMedia, now: datetime) -> None:
        self._values[url] = (now + self.ttl, media)
        self._values.move_to_end(url)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)


_PACK_MEASURE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml|stk|pcs?)$", re.I)
_MULTIPACK = re.compile(r"^(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|dl|cl|ml|stk|pcs?)$", re.I)


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", "."))


def _explicit_pack(value: str | None) -> tuple[Decimal, str] | None:
    """Parse only a complete simple or explicit multipack measurement."""
    if not value or len(value) > 64:
        return None
    text = " ".join(value.split())
    match = _PACK_MEASURE.fullmatch(text)
    if match is None:
        match = _MULTIPACK.fullmatch(text)
        if match is None:
            return None
        count = int(match.group(1))
        if not 1 <= count <= 999:
            return None
        quantity = multiply_integer_exact(_decimal(match.group(2)), count)
        unit = match.group(3)
    else:
        quantity = _decimal(match.group(1))
        unit = match.group(2)
    unit = normalize_unit(unit)
    if unit in {"stk", "pc", "pcs"}:
        unit = "item"
    quantity, unit = canonicalize_quantity(quantity, unit)
    if not quantity.is_finite() or quantity <= 0:
        return None
    return quantity, unit


def _package_recommendation(
    need_quantity: Decimal, need_unit: str, pack: tuple[Decimal, str] | None
) -> tuple[int, Decimal] | None:
    if pack is None:
        return None
    pack_quantity, pack_unit = pack
    need_quantity, need_unit = canonicalize_quantity(need_quantity, need_unit)
    if need_unit not in compatible_units(pack_unit):
        return None
    if need_unit != pack_unit:
        pack_quantity, pack_unit = canonicalize_quantity(pack_quantity, pack_unit)
        need_quantity, need_unit = canonicalize_quantity(need_quantity, need_unit)
    if pack_unit != need_unit or pack_quantity <= 0:
        return None
    with localcontext() as context:
        context.prec = max(
            28, len(need_quantity.as_tuple().digits) + len(pack_quantity.as_tuple().digits) + 8
        )
        packages = int((need_quantity / pack_quantity).to_integral_value(rounding=ROUND_CEILING))
        if packages > 99:
            return None
        fulfillment = multiply_integer_exact(pack_quantity, packages)
    return packages, fulfillment


class BasketMatchingService:
    """Find candidates for current positive inventory-adjusted purchase needs."""

    def __init__(
        self,
        inventory: InventoryService,
        provider: SearchProvider,
        *,
        media_fetcher: MediaFetcher = fetch_product_media,
        media_cache: _MediaCache | None = None,
    ):
        self.inventory = inventory
        self.provider = provider
        self.media_fetcher = media_fetcher
        self.media_cache = media_cache or _MediaCache()

    async def find_products(
        self, actor_id: str, start: date, end: date, *, page: int = 1, size: int = 5
    ) -> BasketMatchResult:
        if end < start:
            raise ValueError("end date must be on or after start date")
        if not 1 <= page <= 50 or not 1 <= size <= MAX_RESULTS_PER_NEED:
            raise ValueError("page or result size is outside the supported range")
        plan = await run_in_threadpool(self.inventory.plan, start, end)
        purchase_lines = plan.purchase_lines
        bounded_lines = purchase_lines[:MAX_NEEDS]
        skipped_count = max(0, len(purchase_lines) - len(bounded_lines))
        results = [
            NeedMatchResult(
                need_id=line.id,
                name=line.name,
                quantity=line.to_buy_quantity,
                unit=line.unit,
                page=page,
                has_more=False,
                candidates=[],
                status="searching",
            )
            for line in bounded_lines
        ]
        if not bounded_lines:
            return BasketMatchResult(start=start, end=end, needs=[], skipped_need_count=0)

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)

        async def search_batch(indexes: list[int]):
            async with semaphore:
                queries = [bounded_lines[index].name for index in indexes]
                return indexes, await asyncio.wait_for(
                    self.provider.search_products(actor_id, queries, page, size),
                    timeout=SEARCH_TIMEOUT_SECONDS,
                )

        batches = [
            list(range(offset, min(offset + 10, len(bounded_lines))))
            for offset in range(0, len(bounded_lines), 10)
        ]
        search_outcomes = await asyncio.gather(
            *(search_batch(indexes) for indexes in batches),
            return_exceptions=True,
        )
        groups_by_index = {}
        for indexes, outcome in zip(batches, search_outcomes, strict=True):
            if isinstance(outcome, BaseException):
                for index in indexes:
                    groups_by_index[index] = outcome
                continue
            returned_indexes, payload = outcome
            try:
                for index in returned_indexes:
                    query = bounded_lines[index].name
                    group = next(
                        (item for item in payload if item.query.casefold() == query.casefold()),
                        None,
                    )
                    groups_by_index[index] = group
            except Exception as error:
                for index in returned_indexes:
                    groups_by_index[index] = error
        media_slots = MAX_MEDIA_FETCHES
        media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA)

        async def enrich(candidate: BasketCandidate) -> BasketCandidate:
            nonlocal media_slots
            if candidate.product_url is None or media_slots <= 0:
                return candidate
            media_slots -= 1
            url = str(candidate.product_url)
            media = self.media_cache.get(url, datetime.now(UTC))
            if media is None:
                try:
                    async with media_semaphore:
                        media = await asyncio.wait_for(
                            self.media_fetcher(url), timeout=MEDIA_TIMEOUT_SECONDS
                        )
                    self.media_cache.put(url, media, datetime.now(UTC))
                except Exception:
                    return candidate
            pack = _explicit_pack(media.pack_description)
            recommendation = _package_recommendation(
                bounded_lines[current_index].to_buy_quantity,
                bounded_lines[current_index].unit,
                pack,
            )
            return candidate.model_copy(
                update={
                    "media": media,
                    "pack_description": media.pack_description,
                    "pack_quantity": pack[0] if pack else None,
                    "pack_unit": pack[1] if pack else None,
                    "pack_metadata_status": "unverified" if media.pack_description else "unknown",
                    "recommended_packages": recommendation[0] if recommendation else None,
                    "recommended_fulfillment_quantity": recommendation[1]
                    if recommendation
                    else None,
                }
            )

        for current_index in range(len(bounded_lines)):
            outcome = groups_by_index.get(current_index)
            if isinstance(outcome, BaseException):
                results[current_index].status = "search_failed"
                results[current_index].detail = "Product search failed or timed out."
                continue
            try:
                group = outcome
                if group is None:
                    results[current_index].status = "search_unavailable"
                    results[
                        current_index
                    ].detail = "The provider returned no result group for this query."
                    continue
                candidates = [
                    BasketCandidate(
                        id=f"{product.provider}:{product.product_id}",
                        provider=product.provider,
                        product_id=product.product_id,
                        name=product.name,
                        description=product.description,
                        brand=product.brand,
                        product_url=product.product_url,
                        price=product.price,
                        unit_price=product.unit_price,
                        unit_name=product.unit_name,
                        available=product.available,
                        match_query=group.query,
                        match_reason=(
                            f"Returned by {product.provider} product search for the need name "
                            f"'{group.query}'."
                        ),
                    )
                    for product in group.products[:size]
                ]
                results[current_index].has_more = group.has_more or len(group.products) > size
                results[current_index].candidates = await asyncio.gather(
                    *(enrich(candidate) for candidate in candidates)
                )
                results[current_index].status = "matched" if candidates else "no_results"
                if group.has_more:
                    results[
                        current_index
                    ].detail = "More provider results are available on the next page."
            except Exception:
                results[current_index].status = "search_unavailable"
                results[current_index].detail = "The provider response could not be interpreted."

        return BasketMatchResult(
            start=start,
            end=end,
            needs=results,
            skipped_need_count=skipped_count,
        )
