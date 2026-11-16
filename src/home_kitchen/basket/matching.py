"""Read-only product matching for the basket review workflow.

Provider results stay as candidates until a person selects one through the
existing basket review flow. Product-page pack text is treated as unverified
metadata and only a complete, unambiguous measurement can support a quantity.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from starlette.concurrency import run_in_threadpool

from home_kitchen.inventory import InventoryService, PurchasePlan
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
SEARCH_CACHE_TTL = timedelta(minutes=5)
SEARCH_EMPTY_CACHE_TTL = timedelta(seconds=20)
SEARCH_CACHE_MAX_ENTRIES = 512
ODA_QUERY_BATCH_SIZE = 5
# Imported recipes commonly keep English ingredient names, while Oda's search
# is strongest with Bokmål. Keep the source query intact and add one localized
# query so the provider request and the whole-plan cache remain bounded.
_BOKMAL_INGREDIENTS = {
    "chicken breast": "kyllingfilet",
    "chicken breasts": "kyllingfilet",
    "chicken thigh": "kyllinglår",
    "chicken thighs": "kyllinglår",
    "ground beef": "kjøttdeig",
    "minced beef": "kjøttdeig",
    "minced meat": "kjøttdeig",
    "crème fraîche": "crème fraîche",
    "creme fraiche": "crème fraîche",
    "sour cream": "rømme",
    "heavy cream": "kremfløte",
    "whipping cream": "kremfløte",
    "spring onion": "vårløk",
    "green onion": "vårløk",
    "red onion": "rødløk",
    "yellow onion": "gul løk",
    "cherry tomatoes": "cherrytomater",
    "coffee beans": "kaffebønner",
    "black pepper": "sort pepper",
    "olive oil": "olivenolje",
    "vegetable oil": "matolje",
    "coconut milk": "kokosmelk",
    "red bell pepper": "rød paprika",
    "bell pepper": "paprika",
    "tomatoes": "tomater",
    "tomato": "tomat",
    "potatoes": "poteter",
    "potato": "potet",
    "carrots": "gulrøtter",
    "carrot": "gulrot",
    "garlic": "hvitløk",
    "onions": "løk",
    "onion": "løk",
    "coffee": "kaffe",
    "milk": "melk",
    "butter": "smør",
    "flour": "hvetemel",
    "sugar": "sukker",
    "eggs": "egg",
    "egg": "egg",
    "lemon": "sitron",
    "parsley": "persille",
    "basil": "basilikum",
    "coriander": "koriander",
    "cucumber": "agurk",
    "spinach": "spinat",
    "salmon": "laks",
    "rice": "ris",
    "pasta": "pasta",
}
_BOKMAL_QUALIFIERS = {
    "fresh": "ferske",
    "chopped": "hakket",
    "diced": "terninger",
    "minced": "finhakket",
    "ground": "malt",
    "whole": "hel",
    "frozen": "fryst",
    "canned": "hermetisk",
    "dried": "tørket",
    "smoked": "røkt",
}
_INGREDIENT_PHRASES = re.compile(
    r"(?<!\w)("
    + "|".join(re.escape(key) for key in sorted(_BOKMAL_INGREDIENTS, key=len, reverse=True))
    + r")(?!\w)",
    re.I,
)
_QUALIFIER_WORDS = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(key) for key in _BOKMAL_QUALIFIERS) + r")(?!\w)",
    re.I,
)
_TRAILING_FORM_QUALIFIER = re.compile(
    r"^(.*?)(?:,\s*|\s+)(hakket|finhakket|terninger|malt|hel|fryst|hermetisk|tørket|røkt)$",
    re.I,
)


def _bokmal_ingredient_query(query: str) -> str | None:
    """Translate known food terms and common form qualifiers for Oda search."""
    if re.search(r"\bcoffee[\s-]*mate\b", query, re.I):
        return None
    translated, count = _INGREDIENT_PHRASES.subn(
        lambda match: _BOKMAL_INGREDIENTS[match.group(0).casefold()], query
    )
    if not count:
        return None
    translated = _QUALIFIER_WORDS.sub(
        lambda match: _BOKMAL_QUALIFIERS[match.group(0).casefold()], translated
    )
    translated = " ".join(translated.split())
    trailing_qualifier = _TRAILING_FORM_QUALIFIER.fullmatch(translated)
    if trailing_qualifier:
        translated = f"{trailing_qualifier.group(2)} {trailing_qualifier.group(1)}"
    return translated


def _query_expansions(query: str) -> tuple[str, ...]:
    localized = _bokmal_ingredient_query(query)
    if localized and localized.casefold() != query.casefold():
        return query, localized
    return (query,)


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
    pack_metadata_source: Literal["product_page", "oda_description"] | None = None
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
    approximate: bool = False
    provenance: str | None = None
    source_url: HttpUrl | None = None


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

    # The official endpoint accepts ten queries; reserve room for one localized
    # expansion per need while keeping the provider request bounded.
    max_queries = ODA_QUERY_BATCH_SIZE

    def __init__(self, service: OdaService):
        self.service = service

    async def cache_context(self, actor_id: str) -> Hashable | None:
        """Read connection lifecycle state without blocking the event loop."""
        context = getattr(self.service, "search_cache_context", None)
        return await run_in_threadpool(context, actor_id) if context is not None else None

    async def search_products(
        self, actor_id: str, queries: list[str], page: int, size: int
    ) -> list[ProviderSearchGroup]:
        expanded_by_query = {query: _query_expansions(query) for query in queries}
        provider_queries = list(
            dict.fromkeys(expanded for query in queries for expanded in expanded_by_query[query])
        )
        response = OdaSearchResponse.model_validate(
            await self.service.search_products(actor_id, provider_queries, page, size)
        )
        groups = {}
        for group in response.result:
            groups[" ".join(group.query.casefold().split())] = (
                group.has_more,
                [
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

        def product_is_mismatch(query: str, product: ProviderProduct) -> bool:
            normalized = " ".join(query.casefold().split())
            text = " ".join((product.name, product.description, product.brand or "")).casefold()
            coffee_need = (
                re.search(r"\b(?:coffee|kaffe)\b", normalized) is not None
                and re.search(r"\bcoffee[\s-]*mate\b", normalized) is None
            )
            tomato_need = re.search(r"\b(?:tomato(?:es)?|tomat(?:er)?)\b", normalized) is not None
            tomato_paste_need = re.search(r"\b(?:paste|pur[eé]e|pur[eé])\b", normalized) is not None
            if coffee_need and re.search(
                r"\bcoffee[\s-]*mate\b|\bkaffe(?:fløte|melk|krem)\b", text
            ):
                return True
            return (
                tomato_need
                and not tomato_paste_need
                and re.search(
                    r"\b(?:tomato\s+(?:paste|pur[eé]e)|tomatpur[eé]|tomatkonsentrat)\b", text
                )
                is not None
            )

        result = []
        for query in queries:
            product_map = OrderedDict()
            has_more = False
            for expanded in expanded_by_query[query]:
                group = groups.get(" ".join(expanded.casefold().split()))
                if group is None:
                    continue
                group_has_more, group_products = group
                has_more = has_more or group_has_more
                for product in group_products:
                    if not product_is_mismatch(query, product):
                        product_map.setdefault((product.provider, product.product_id), product)
            result.append(
                ProviderSearchGroup(
                    query=query,
                    has_more=has_more,
                    products=list(product_map.values()),
                )
            )
        return result


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


def _oda_description_pack(value: str) -> tuple[Decimal, str] | None:
    """Accept a complete Oda pack descriptor, never a measurement inside prose."""
    if len(value) > 256:
        return None
    pack = _explicit_pack(value)
    if pack is not None:
        return pack
    # Only a comma followed by whitespace separates descriptors. A decimal
    # comma (including the one in "0,5% fett") must remain part of its number.
    descriptors = re.split(r",\s+", value.strip())
    if len(descriptors) < 2:
        return None
    prefix = ", ".join(descriptors[:-1])
    # Percentages describe milk fat, etc. Other numbers may be competing pack
    # sizes, ranges, or nutrition values and are deliberately left unresolved.
    prefix_without_percent = re.sub(r"\d+(?:[.,]\d+)?\s*%", "", prefix)
    if (
        re.search(r"\d|[/×=<>]", prefix_without_percent)
        or not all(part.strip() for part in descriptors)
        or re.search(
            r"\b(per|pr|ca|cirka|omtrent|fra|til|næringsinnhold|nutrition|"
            r"energi|energy|protein|karbohydrat|carbohydrate|salt|kcal|kj)\b",
            prefix,
            re.I,
        )
    ):
        return None
    return _explicit_pack(descriptors[-1])


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.inventory = inventory
        self.provider = provider
        self.media_fetcher = media_fetcher
        self.media_cache = media_cache or _MediaCache()
        self.clock = clock
        self._search_cache: OrderedDict[tuple, tuple[datetime, ProviderSearchGroup]] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Future] = {}
        self._context_reads: dict[str, asyncio.Task] = {}
        self._search_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
        self._search_tasks: set[asyncio.Task] = set()
        self._media_semaphore = asyncio.Semaphore(MAX_CONCURRENT_MEDIA)

    async def _context(self, actor_id: str) -> Hashable | None:
        context = getattr(self.provider, "cache_context", None)
        if context is None:
            return id(self.provider)
        # A cache hit for a whole plan needs one shared lifecycle read, rather
        # than a database read for every need resolving on the same loop turn.
        task = self._context_reads.get(actor_id)
        if task is None:
            task = asyncio.create_task(context(actor_id))
            self._context_reads[actor_id] = task

            def clear(completed):
                if self._context_reads.get(actor_id) is completed:
                    del self._context_reads[actor_id]

            task.add_done_callback(clear)
        return await asyncio.shield(task)

    async def _search(
        self,
        actor_id: str,
        queries: list[str],
        page: int,
        size: int,
        force_refresh: bool,
        on_result: Callable[[str, ProviderSearchGroup | Exception | None], Awaitable[None]],
    ) -> None:
        context = await self._context(actor_id)
        pending = {}
        owned = []
        for query in queries:
            normalized = " ".join(query.casefold().split())
            if normalized in pending:
                continue
            key = (actor_id, id(self.provider), context, normalized, page, size)
            cached = self._search_cache.get(key) if context is not None else None
            if cached is not None and cached[0] > self.clock() and not force_refresh:
                self._search_cache.move_to_end(key)
                future = asyncio.get_running_loop().create_future()
                future.set_result(cached[1])
            elif context is not None and key in self._inflight:
                future = self._inflight[key]
            else:
                self._search_cache.pop(key, None)
                future = asyncio.get_running_loop().create_future()
                if context is not None:
                    self._inflight[key] = future
                owned.append((key, query, future))
            pending[normalized] = future

        async def fetch(batch):
            try:
                async with self._search_semaphore:
                    payload = await asyncio.wait_for(
                        self.provider.search_products(
                            actor_id, [query for _, query, _ in batch], page, size
                        ),
                        timeout=SEARCH_TIMEOUT_SECONDS,
                    )
                unchanged = context is not None and await self._context(actor_id) == context
                groups = {" ".join(group.query.casefold().split()): group for group in payload}
                for key, _, future in batch:
                    group = groups.get(key[3])
                    if context is not None and not unchanged:
                        group = RuntimeError("Provider connection changed during search")
                    if unchanged and isinstance(group, ProviderSearchGroup):
                        ttl = SEARCH_CACHE_TTL if group.products else SEARCH_EMPTY_CACHE_TTL
                        self._search_cache[key] = (self.clock() + ttl, group)
                        self._search_cache.move_to_end(key)
                        while len(self._search_cache) > SEARCH_CACHE_MAX_ENTRIES:
                            self._search_cache.popitem(last=False)
                    future.set_result(group)
            except BaseException as error:
                for _, _, future in batch:
                    if not future.done():
                        future.set_result(
                            error
                            if isinstance(error, Exception)
                            else RuntimeError("Search cancelled")
                        )
                if not isinstance(error, Exception):
                    raise
            finally:
                for key, _, future in batch:
                    if self._inflight.get(key) is future:
                        del self._inflight[key]

        batch_size = max(1, min(10, int(getattr(self.provider, "max_queries", 10))))
        for offset in range(0, len(owned), batch_size):
            task = asyncio.create_task(fetch(owned[offset : offset + batch_size]))
            self._search_tasks.add(task)
            task.add_done_callback(self._search_tasks.discard)

        async def resolve(query, future):
            group = await asyncio.shield(future)
            if context is not None and await self._context(actor_id) != context:
                group = RuntimeError("Provider connection changed during search")
            await on_result(query, group)

        await asyncio.gather(*(resolve(query, future) for query, future in pending.items()))

    async def find_products(
        self,
        actor_id: str,
        start: date,
        end: date,
        *,
        page: int = 1,
        size: int = 5,
        force_refresh: bool = False,
        progress: Callable[[NeedMatchResult], Awaitable[None]] | None = None,
        plan: PurchasePlan | None = None,
    ) -> BasketMatchResult:
        if end < start:
            raise ValueError("end date must be on or after start date")
        if not 1 <= page <= 50 or not 1 <= size <= MAX_RESULTS_PER_NEED:
            raise ValueError("page or result size is outside the supported range")
        if plan is None:
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
                approximate=line.approximate,
                provenance=line.provenance,
                source_url=line.source_url,
            )
            for line in bounded_lines
        ]
        if not bounded_lines:
            return BasketMatchResult(start=start, end=end, needs=[], skipped_need_count=0)

        media_slots = MAX_MEDIA_FETCHES

        async def enrich(candidate: BasketCandidate, current_index: int) -> BasketCandidate:
            nonlocal media_slots
            media = None
            url = str(candidate.product_url) if candidate.product_url else None
            if url:
                media = self.media_cache.get(url, self.clock())
            if url and media is None and media_slots > 0:
                media_slots -= 1
                try:
                    async with self._media_semaphore:
                        media = self.media_cache.get(url, self.clock())
                        if media is None:
                            media = await asyncio.wait_for(
                                self.media_fetcher(url), timeout=MEDIA_TIMEOUT_SECONDS
                            )
                            self.media_cache.put(url, media, self.clock())
                except Exception:
                    pass
            pack_description = media.pack_description if media else None
            pack = _explicit_pack(pack_description)
            pack_source = "product_page" if pack else None
            # A valid explicit page pack wins. The official search description
            # is only a fallback; names, titles and unit prices are never used.
            if pack is None and candidate.provider == "oda":
                pack = _oda_description_pack(candidate.description)
                if pack:
                    pack_description = candidate.description
                    pack_source = "oda_description"
            recommendation = _package_recommendation(
                bounded_lines[current_index].to_buy_quantity,
                bounded_lines[current_index].unit,
                pack,
            )
            return candidate.model_copy(
                update={
                    "media": media,
                    "pack_description": pack_description,
                    "pack_quantity": pack[0] if pack else None,
                    "pack_unit": pack[1] if pack else None,
                    "pack_metadata_source": pack_source,
                    "pack_metadata_status": "unverified" if pack_description else "unknown",
                    "recommended_packages": recommendation[0] if recommendation else None,
                    "recommended_fulfillment_quantity": recommendation[1]
                    if recommendation
                    else None,
                }
            )

        async def match_need(current_index: int, outcome):
            if isinstance(outcome, BaseException):
                results[current_index].status = "search_failed"
                results[current_index].detail = "Product search failed or timed out."
                if progress is not None:
                    await progress(results[current_index])
                return
            try:
                group = outcome
                if group is None:
                    results[current_index].status = "search_unavailable"
                    results[
                        current_index
                    ].detail = "The provider returned no result group for this query."
                    return
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
                    *(enrich(candidate, current_index) for candidate in candidates)
                )
                results[current_index].status = "matched" if candidates else "no_results"
                if group.has_more:
                    results[
                        current_index
                    ].detail = "More provider results are available on the next page."
            except Exception:
                results[current_index].status = "search_unavailable"
                results[current_index].detail = "The provider response could not be interpreted."
            finally:
                if progress is not None:
                    await progress(results[current_index])

        async def on_result(query, outcome):
            await asyncio.gather(
                *(
                    match_need(index, outcome)
                    for index, line in enumerate(bounded_lines)
                    if " ".join(line.name.casefold().split()) == query
                )
            )

        try:
            await self._search(
                actor_id,
                [line.name for line in bounded_lines],
                page,
                size,
                force_refresh,
                on_result,
            )
        except Exception as error:
            await asyncio.gather(
                *(
                    match_need(index, error)
                    for index, result in enumerate(results)
                    if result.status == "searching"
                )
            )

        return BasketMatchResult(
            start=start,
            end=end,
            needs=results,
            skipped_need_count=skipped_count,
        )
