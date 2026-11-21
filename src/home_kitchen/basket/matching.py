"""Read-only product matching for the basket review workflow.

Provider results stay as candidates until a person selects one through the
existing basket review flow. Product-page pack text is treated as unverified
metadata and only a complete, unambiguous measurement can support a quantity.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from starlette.concurrency import run_in_threadpool

from home_kitchen.basket.adapters.oda_search import OdaSearchAdapter
from home_kitchen.basket.domain.matching_logic import (
    MAX_CONCURRENT_MEDIA,
    MAX_CONCURRENT_SEARCHES,
    MAX_MEDIA_FETCHES,
    MAX_NEEDS,
    MAX_RESULTS_PER_NEED,
    MEDIA_CACHE_MAX_ENTRIES,
    MEDIA_CACHE_TTL,
    MEDIA_TIMEOUT_SECONDS,
    ODA_QUERY_BATCH_SIZE,
    SEARCH_CACHE_MAX_ENTRIES,
    SEARCH_CACHE_TTL,
    SEARCH_CACHE_VERSION,
    SEARCH_EMPTY_CACHE_TTL,
    SEARCH_TIMEOUT_SECONDS,
    AvailabilityCheckOutcome,
    AvailabilityTarget,
    BasketCandidate,
    BasketMatchResult,
    NeedMatchResult,
    ProviderProduct,
    ProviderSearchGroup,
    SearchProvider,
    SkuPreferenceCitation,
    StockEvidenceCitation,
    _bokmal_ingredient_query,
    _clean_ingredient_query,
    _decimal,
    _explicit_pack,
    _identity_status,
    _IdentityFamily,
    _MediaCache,
    _oda_description_pack,
    _package_recommendation,
    _product_identity,
    _product_matches_ingredient,
    _query_expansions,
    _query_identity,
)
from home_kitchen.basket.ports import (
    IdentityCorrectionPort,
    IdentityLookupPort,
    SkuPreferencePort,
)
from home_kitchen.catalogue.identity import IdentityCorrectionCitation
from home_kitchen.inventory import InventoryService, PurchasePlan
from home_kitchen.oda.media import fetch_product_media

__all__ = [
    "MAX_NEEDS",
    "MAX_RESULTS_PER_NEED",
    "MAX_MEDIA_FETCHES",
    "MAX_CONCURRENT_SEARCHES",
    "MAX_CONCURRENT_MEDIA",
    "SEARCH_TIMEOUT_SECONDS",
    "MEDIA_TIMEOUT_SECONDS",
    "MEDIA_CACHE_TTL",
    "MEDIA_CACHE_MAX_ENTRIES",
    "SEARCH_CACHE_TTL",
    "SEARCH_EMPTY_CACHE_TTL",
    "SEARCH_CACHE_MAX_ENTRIES",
    "ODA_QUERY_BATCH_SIZE",
    "SEARCH_CACHE_VERSION",
    "StockEvidenceCitation",
    "SkuPreferenceCitation",
    "BasketCandidate",
    "ProviderProduct",
    "ProviderSearchGroup",
    "NeedMatchResult",
    "BasketMatchResult",
    "AvailabilityTarget",
    "AvailabilityCheckOutcome",
    "SearchProvider",
    "OdaSearchAdapter",
    "BasketMatchingService",
    "_IdentityFamily",
    "_MediaCache",
    "_clean_ingredient_query",
    "_query_identity",
    "_product_identity",
    "_identity_status",
    "_product_matches_ingredient",
    "_bokmal_ingredient_query",
    "_query_expansions",
    "_decimal",
    "_explicit_pack",
    "_oda_description_pack",
    "_package_recommendation",
]


MediaFetcher = Callable[[str], Awaitable[Any]]


class _CallableSkuPreferenceAdapter(SkuPreferencePort):
    def __init__(self, fn: Callable[[UUID], tuple[list[str], list[str]]]) -> None:
        self._fn = fn

    def get_sku_preferences(self, product_id: UUID) -> tuple[list[str], list[str]]:
        return self._fn(product_id)


class _CallableIdentityLookupAdapter(IdentityLookupPort):
    def __init__(self, fn: Callable[[str], UUID | None]) -> None:
        self._fn = fn

    def resolve_product_id(self, name: str) -> UUID | None:
        return self._fn(name)


class _CallableIdentityCorrectionAdapter(IdentityCorrectionPort):
    def __init__(
        self,
        fn: Callable[[str, UUID | None], Sequence[IdentityCorrectionCitation | Any]],
    ) -> None:
        self._fn = fn

    def get_identity_corrections(
        self, need_name: str, product_id: UUID | None
    ) -> list[IdentityCorrectionCitation]:
        citations: list[IdentityCorrectionCitation] = []
        for item in self._fn(need_name, product_id):
            if isinstance(item, IdentityCorrectionCitation):
                citations.append(item)
            else:
                citations.append(IdentityCorrectionCitation.from_event(item))
        return citations


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
        sku_preferences: SkuPreferencePort | None = None,
        identity_lookup: IdentityLookupPort | None = None,
        identity_corrections: IdentityCorrectionPort | None = None,
        preferences_provider: Callable[[UUID], tuple[list[str], list[str]]] | None = None,
        product_id_resolver: Callable[[str], UUID | None] | None = None,
        identity_corrections_provider: Callable[
            [str, UUID | None], list[IdentityCorrectionCitation]
        ]
        | None = None,
        purchase_evidence_provider: Callable[[UUID], list[dict[str, Any]]] | None = None,
    ):
        self.inventory = inventory
        self.provider = provider
        self.media_fetcher = media_fetcher
        self.media_cache = media_cache or _MediaCache()
        self.clock = clock
        self.sku_preferences: SkuPreferencePort | None = sku_preferences or (
            _CallableSkuPreferenceAdapter(preferences_provider)
            if preferences_provider is not None
            else None
        )
        self.identity_lookup: IdentityLookupPort | None = identity_lookup or (
            _CallableIdentityLookupAdapter(product_id_resolver)
            if product_id_resolver is not None
            else None
        )
        self.identity_corrections: IdentityCorrectionPort | None = identity_corrections or (
            _CallableIdentityCorrectionAdapter(identity_corrections_provider)
            if identity_corrections_provider is not None
            else None
        )
        self.purchase_evidence_provider = purchase_evidence_provider
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
            key = (
                actor_id,
                id(self.provider),
                context,
                normalized,
                page,
                size,
                SEARCH_CACHE_VERSION,
            )
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
        query_overrides: dict[UUID, str] | None = None,
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

        def _effective_query(line) -> str:
            if query_overrides and line.id in query_overrides:
                cleaned = " ".join(query_overrides[line.id].split())
                if cleaned:
                    return cleaned
            return line.name

        effective_queries = [_effective_query(line) for line in bounded_lines]
        normalized_queries = [" ".join(query.casefold().split()) for query in effective_queries]
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
                search_query=effective_queries[index],
                stock_evidence=(
                    StockEvidenceCitation(
                        available_quantity=line.available_quantity,
                        covered_quantity=line.covered_quantity,
                        unit=line.unit,
                    )
                    if (
                        getattr(line, "available_quantity", Decimal("0")) > 0
                        or getattr(line, "covered_quantity", Decimal("0")) > 0
                    )
                    else None
                ),
            )
            for index, line in enumerate(bounded_lines)
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
                preferred_ids: list[str] = []
                excluded_ids: set[str] = set()
                canon_id = getattr(bounded_lines[current_index], "canonical_product_id", None)
                if canon_id is None and self.identity_lookup is not None:
                    try:
                        canon_id = self.identity_lookup.resolve_product_id(
                            bounded_lines[current_index].name
                        )
                    except Exception:
                        canon_id = None
                if canon_id is not None and self.sku_preferences is not None:
                    try:
                        pref_list, excl_list = self.sku_preferences.get_sku_preferences(canon_id)
                        preferred_ids = list(pref_list)
                        excluded_ids = set(excl_list)
                    except Exception:
                        pass
                sku_preference_evidence = (
                    SkuPreferenceCitation(
                        preferred_product_ids=list(preferred_ids),
                        excluded_product_ids=sorted(excluded_ids),
                    )
                    if preferred_ids or excluded_ids
                    else None
                )
                purchase_evidence: list[dict[str, Any]] = []
                if canon_id is not None and self.purchase_evidence_provider is not None:
                    try:
                        purchase_evidence = list(self.purchase_evidence_provider(canon_id))
                    except Exception:
                        purchase_evidence = []
                correction_citations: list[IdentityCorrectionCitation] = []
                if self.identity_corrections is not None:
                    try:
                        correction_citations = list(
                            self.identity_corrections.get_identity_corrections(
                                bounded_lines[current_index].name, canon_id
                            )
                        )
                    except Exception:
                        correction_citations = []

                effective_query = effective_queries[current_index]
                if " ".join(group.query.casefold().split()) != " ".join(
                    effective_query.casefold().split()
                ):
                    results[current_index].status = "search_unavailable"
                    results[
                        current_index
                    ].detail = "The provider returned a result group for a different query."
                    return
                filtered_products = [
                    p
                    for p in group.products
                    if p.product_id not in excluded_ids
                    and _identity_status(effective_query, p) is not None
                ]
                if preferred_ids:
                    rank_map = {pid: idx for idx, pid in enumerate(preferred_ids)}
                    filtered_products.sort(
                        key=lambda p: (
                            (0, rank_map[p.product_id]) if p.product_id in rank_map else (1, 0)
                        )
                    )

                preferred_rank = {pid: idx + 1 for idx, pid in enumerate(preferred_ids)}
                correction_prefix = (
                    f"Identitetsretting ({correction_citations[0].source_type}, "
                    f"{correction_citations[0].channel} av {correction_citations[0].actor_id}); "
                    if correction_citations
                    else ""
                )
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
                            (
                                f"Foretrukket Oda-vare (rang {preferred_rank[product.product_id]}); "
                                if product.product_id in preferred_rank
                                else ""
                            )
                            + correction_prefix
                            + f"{'Identity compatible' if product.identity_status == 'compatible' else 'Identity unverified; explicit confirmation required'}; "
                            f"returned by {product.provider} product search for the need name "
                            f"'{group.query}'."
                        ),
                        identity_status=product.identity_status,
                        is_preferred=product.product_id in preferred_rank,
                        preference_rank=preferred_rank.get(product.product_id),
                        identity_correction_citations=correction_citations,
                    )
                    for product in filtered_products[:size]
                ]
                results[current_index].sku_preference_evidence = sku_preference_evidence
                results[current_index].purchase_evidence = purchase_evidence
                results[current_index].identity_correction_citations = correction_citations
                results[current_index].has_more = group.has_more or len(filtered_products) > size
                results[current_index].candidates = await asyncio.gather(
                    *(enrich(candidate, current_index) for candidate in candidates)
                )
                results[current_index].status = "matched" if candidates else "no_results"
                results[current_index].observed_at = self.clock()
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
                    for index, normalized in enumerate(normalized_queries)
                    if normalized == query
                )
            )

        try:
            await self._search(
                actor_id,
                effective_queries,
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

    @staticmethod
    def _prices_differ(previous: str | None, current: str | None) -> bool:
        if previous is None or current is None:
            return False
        prev_text = previous.strip()
        curr_text = current.strip()
        if not prev_text or not curr_text:
            return False
        try:
            return Decimal(prev_text) != Decimal(curr_text)
        except InvalidOperation:
            return prev_text != curr_text

    async def check_products_availability(
        self,
        actor_id: str,
        targets: list[AvailabilityTarget],
    ) -> list[AvailabilityCheckOutcome]:
        """Verify live provider availability for selected products without using cached searches."""
        if not targets:
            return []
        context = await self._context(actor_id)
        if context is None:
            raise ValueError("Connect your product provider before checking product availability")

        batch_size = max(1, min(10, int(getattr(self.provider, "max_queries", 10))))

        async def run_queries(
            queries: list[str],
        ) -> tuple[dict[tuple[str, str], ProviderProduct], set[str]]:
            unique_queries = list(
                dict.fromkeys(
                    " ".join(query.split()) for query in queries if query and query.strip()
                )
            )
            found: dict[tuple[str, str], ProviderProduct] = {}
            failed_queries: set[str] = set()

            async def run_batch(batch: list[str]) -> None:
                try:
                    async with self._search_semaphore:
                        groups = await asyncio.wait_for(
                            self.provider.search_products(actor_id, batch, 1, 20),
                            timeout=SEARCH_TIMEOUT_SECONDS,
                        )
                    for group in groups:
                        for product in group.products:
                            found[(product.provider, product.product_id)] = product
                except Exception:
                    for query in batch:
                        failed_queries.add(" ".join(query.casefold().split()))

            await asyncio.gather(
                *(
                    run_batch(unique_queries[offset : offset + batch_size])
                    for offset in range(0, len(unique_queries), batch_size)
                )
            )
            return found, failed_queries

        primary_queries = [
            (target.product_name or target.search_query or target.need_name).strip()
            for target in targets
        ]
        found_products, failed_primary = await run_queries(primary_queries)

        fallback_queries: list[str] = []
        for target, primary in zip(targets, primary_queries, strict=True):
            if (target.provider, target.product_id) in found_products:
                continue
            for candidate_query in (target.search_query, target.need_name):
                if candidate_query and candidate_query.strip():
                    cleaned = " ".join(candidate_query.split())
                    if cleaned.casefold() != primary.casefold():
                        fallback_queries.append(cleaned)
                        break

        failed_fallback: set[str] = set()
        if fallback_queries:
            extra_products, failed_fallback = await run_queries(fallback_queries)
            found_products.update(extra_products)

        if await self._context(actor_id) != context:
            raise ValueError("Provider connection changed during availability check")

        outcomes: list[AvailabilityCheckOutcome] = []
        for target, primary in zip(targets, primary_queries, strict=True):
            fresh = found_products.get((target.provider, target.product_id))
            if fresh is None:
                norm_primary = " ".join(primary.casefold().split())
                search_errored = norm_primary in failed_primary or (
                    target.search_query
                    and " ".join(target.search_query.casefold().split()) in failed_fallback
                )
                description = (
                    "Kunne ikke hente oppdatert tilgjengelighet fra Oda akkurat nå."
                    if search_errored
                    else "Produktet ble ikke funnet i ferskt søk hos Oda."
                )
                outcomes.append(
                    AvailabilityCheckOutcome(
                        target=target,
                        available=None,
                        availability_status="unknown",
                        availability_description=description,
                        current_price=target.previous_price,
                        price_changed=False,
                        fresh_product=None,
                        checked_query=primary,
                    )
                )
                continue

            avail = fresh.available
            status: Literal["available", "unavailable", "unknown"] = (
                "available" if avail is True else "unavailable" if avail is False else "unknown"
            )
            description = (
                "Oda oppgir at varen er tilgjengelig."
                if avail is True
                else "Oda oppgir at varen ikke er tilgjengelig."
                if avail is False
                else "Oda oppga ikke tilgjengelighet i det ferske søket."
            )
            outcomes.append(
                AvailabilityCheckOutcome(
                    target=target,
                    available=avail,
                    availability_status=status,
                    availability_description=description,
                    current_price=fresh.price,
                    price_changed=self._prices_differ(target.previous_price, fresh.price),
                    fresh_product=fresh,
                    checked_query=primary,
                )
            )
        return outcomes
