"""Oda search adapter mapping Oda service responses to provider search groups."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable

from starlette.concurrency import run_in_threadpool

from home_kitchen.basket.domain.matching_logic import (
    ODA_QUERY_BATCH_SIZE,
    ProviderProduct,
    ProviderSearchGroup,
    _identity_status,
    _query_expansions,
)
from home_kitchen.oda.models import OdaSearchResponse
from home_kitchen.oda.service import OdaService


class OdaSearchAdapter:
    """Normalize the official Oda response to the provider-neutral protocol."""

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
        seen_provider_queries: set[str] = set()
        provider_queries: list[str] = []
        for query in queries:
            for expanded in expanded_by_query[query]:
                normalized_expanded = " ".join(expanded.casefold().split())
                if normalized_expanded not in seen_provider_queries:
                    seen_provider_queries.add(normalized_expanded)
                    provider_queries.append(expanded)
        response = OdaSearchResponse.model_validate(
            await self.service.search_products(actor_id, provider_queries, page, size)
        )
        groups: dict[str, tuple[bool, list[ProviderProduct]]] = {}
        for group in response.result:
            normalized_group_query = " ".join(group.query.casefold().split())
            products = [
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
                    available=(product.availability.is_available if product.availability else None),
                )
                for product in group.products
            ]
            if normalized_group_query in groups:
                prev_has_more, prev_products = groups[normalized_group_query]
                groups[normalized_group_query] = (
                    prev_has_more or group.has_more,
                    prev_products + products,
                )
            else:
                groups[normalized_group_query] = (group.has_more, products)

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
                    identity_status = _identity_status(query, product)
                    if identity_status is not None:
                        product_map.setdefault(
                            (product.provider, product.product_id),
                            product.model_copy(update={"identity_status": identity_status}),
                        )
            result.append(
                ProviderSearchGroup(
                    query=query,
                    has_more=has_more,
                    products=list(product_map.values()),
                )
            )
        return result
