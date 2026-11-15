from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from home_kitchen.basket.matching import (
    BasketMatchingService,
    OdaSearchAdapter,
    ProviderProduct,
    ProviderSearchGroup,
    _explicit_pack,
    _oda_description_pack,
)
from home_kitchen.oda.media import ProductMedia


class FakeInventory:
    def __init__(self, lines):
        self.lines = lines

    def plan(self, start, end):
        assert start == date(2026, 11, 16)
        assert end == date(2026, 11, 22)
        return type("Plan", (), {"purchase_lines": self.lines})()


class FakeProvider:
    def __init__(self, *, fail_query=None):
        self.calls = []
        self.fail_query = fail_query

    async def search_products(self, actor_id, queries, page, size):
        assert actor_id == "household-1"
        self.calls.append((queries, page, size))
        if self.fail_query in queries:
            raise TimeoutError("simulated provider timeout")
        return [
            ProviderSearchGroup(
                query=query,
                has_more=page == 1,
                products=[
                    ProviderProduct(
                        provider="oda",
                        product_id=str(index + 10),
                        name=f"{query} product",
                        description="Provider description kept as-is",
                        price="29.90",
                        unit_price="29.90",
                        unit_name="per kg",
                        product_url=f"https://oda.com/no/products/{index + 10}-product",
                    )
                ],
            )
            for index, query in enumerate(queries)
        ]


def line(name="carrot", quantity="1", unit="kg", index=1):
    return type(
        "PurchaseLine",
        (),
        {
            "id": UUID(int=index),
            "name": name,
            "to_buy_quantity": Decimal(quantity),
            "unit": unit,
        },
    )()


@pytest.mark.asyncio
async def test_search_batches_queries_and_exposes_next_page_and_reviewable_candidates():
    provider = FakeProvider()
    service = BasketMatchingService(
        FakeInventory([line("carrot", index=1), line("milk", quantity="2", unit="l", index=2)]),
        provider,
        media_fetcher=lambda _: _media("500 g"),
    )

    result = await service.find_products(
        "household-1", date(2026, 11, 16), date(2026, 11, 22), page=1, size=5
    )

    assert len(provider.calls) == 1
    assert provider.calls[0][0] == ["carrot", "milk"]
    assert [item.status for item in result.needs] == ["matched", "matched"]
    assert all(item.has_more for item in result.needs)
    first = result.needs[0].candidates[0]
    assert first.id == "oda:10"
    assert first.description == "Provider description kept as-is"
    assert first.pack_quantity == Decimal("500")
    assert first.pack_unit == "g"
    assert first.recommended_packages == 2
    assert first.recommended_fulfillment_quantity == Decimal("1000")
    assert result.needs[1].candidates[0].recommended_packages is None


@pytest.mark.asyncio
async def test_failed_batch_is_reported_without_dropping_successful_need():
    provider = FakeProvider(fail_query="milk")
    service = BasketMatchingService(
        FakeInventory(
            [line(f"item {index}", index=index) for index in range(1, 11)]
            + [line("milk", index=11)]
        ),
        provider,
        media_fetcher=lambda _: _media(None),
    )

    result = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))

    assert [item.status for item in result.needs[:10]] == ["matched"] * 10
    assert result.needs[10].status == "search_failed"
    assert all(item.candidates for item in result.needs[:10])
    assert result.needs[10].candidates == []


def test_pack_metadata_requires_full_unambiguous_supported_measurement():
    assert _explicit_pack(" 8 x 125 g ") == (Decimal("1000"), "g")
    assert _explicit_pack("1,5 l") == (Decimal("1500"), "ml")
    assert _explicit_pack("8 stk") == (Decimal("8"), "item")
    assert _explicit_pack("Milk 1 l, price 25 kr") is None
    assert _explicit_pack("per kg") is None
    assert _explicit_pack("1 l / 500 ml") is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("500 g", (Decimal("500"), "g")),
        ("525 g", (Decimal("525"), "g")),
        ("1% fett, 1,75 l", (Decimal("1750"), "ml")),
        ("0,5% fett, 1,75 l", (Decimal("1750"), "ml")),
        ("Hele bønner, 500 g", (Decimal("500"), "g")),
        ("Små beger, 8 x 125 g", (Decimal("1000"), "g")),
    ],
)
def test_official_oda_description_accepts_only_complete_pack_descriptors(description, expected):
    assert _oda_description_pack(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "Milk 1 l",
        "Hele bønner,500 g",
        "500–600 g",
        "Bønner, 500-600 g",
        "500 g, 1 kg",
        "2 x 500 g, 1 kg",
        "500-600 g, 500 g",
        "1 l / 500 ml",
        "Næringsinnhold per 100 g, 500 g",
        "Nutrition, 500 g",
        "Protein, 500 g",
        "per kg, 500 g",
        "ca. 500 g",
        "ca., 500 g",
        "Bønner, , 500 g",
        "Bønner, 500 g ekstra",
        "1, 75 l",
        "",
        "a" * 257,
    ],
)
def test_official_oda_description_rejects_ambiguous_or_embedded_measurements(description):
    assert _oda_description_pack(description) is None


@pytest.mark.asyncio
async def test_description_pack_recommendations_recompute_with_cached_search_and_failed_media():
    from unittest.mock import AsyncMock

    provider = FakeProvider()
    provider.search_products = AsyncMock()
    provider.search_products.return_value = [
        ProviderSearchGroup(
            query=name,
            has_more=False,
            products=[
                ProviderProduct(
                    provider="oda",
                    product_id=str(index),
                    name=name,
                    description=description,
                    product_url=f"https://oda.com/no/products/{index}-product",
                )
            ],
        )
        for index, (name, description) in enumerate(
            [
                ("pasta", "500 g"),
                ("milk", "0,5% fett, 1,75 l"),
                ("coffee", "Hele bønner, 500 g"),
                ("tomato", "525 g"),
            ]
        )
    ]
    inventory = FakeInventory(
        [
            line("pasta", "233", "g"),
            line("milk", "1000", "ml", 2),
            line("coffee", "1", "pack", 3),
            line("tomato", "1", "item", 4),
        ]
    )
    service = BasketMatchingService(
        inventory, provider, media_fetcher=AsyncMock(side_effect=TimeoutError)
    )
    first = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    candidates = [need.candidates[0] for need in first.needs]
    assert [candidate.recommended_packages for candidate in candidates] == [1, 1, None, None]
    assert candidates[1].pack_description == "0,5% fett, 1,75 l"
    assert candidates[1].pack_metadata_source == "oda_description"
    assert candidates[1].pack_metadata_status == "unverified"
    inventory.lines[0] = line("pasta", "1001", "g")
    second = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert provider.search_products.await_count == 1
    assert second.needs[0].candidates[0].recommended_packages == 3
    assert second.needs[0].candidates[0].recommended_fulfillment_quantity == Decimal("1500")


@pytest.mark.asyncio
async def test_pack_evidence_prefers_valid_media_and_does_not_guess_from_other_fields():
    from unittest.mock import AsyncMock

    provider = FakeProvider()
    provider.search_products = AsyncMock()
    provider.search_products.return_value = [
        ProviderSearchGroup(
            query="pasta",
            has_more=False,
            products=[
                ProviderProduct(
                    provider="oda",
                    product_id="1",
                    name="500 g pasta",
                    description="525 g",
                    product_url="https://oda.com/no/products/1-pasta",
                ),
                ProviderProduct(
                    provider="other",
                    product_id="2",
                    name="500 g pasta",
                    description="500 g",
                    unit_price="500",
                    unit_name="g",
                ),
                ProviderProduct(
                    provider="oda",
                    product_id="3",
                    name="500 g pasta",
                    description="pasta",
                    unit_price="500",
                    unit_name="g",
                ),
                ProviderProduct(provider="oda", product_id="4", name="pasta", description="500 g"),
            ],
        )
    ]
    service = BasketMatchingService(
        FakeInventory([line("pasta", "501", "g")]),
        provider,
        media_fetcher=lambda _: _media("500 g"),
    )
    result = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    candidates = result.needs[0].candidates
    assert [candidate.recommended_packages for candidate in candidates] == [2, None, None, 2]
    assert candidates[0].pack_metadata_source == "product_page"
    assert candidates[0].pack_description == "500 g"
    assert candidates[3].pack_metadata_source == "oda_description"


@pytest.mark.asyncio
async def test_oda_adapter_normalizes_official_fields_to_provider_contract():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            return {
                "result": [
                    {
                        "query": "carrot",
                        "hasMore": False,
                        "products": [
                            {
                                "id": 77,
                                "name": "Carrots",
                                "description": "1 kg carrots",
                                "price": "25.00",
                                "unitPrice": "25.00",
                                "unitName": "per kg",
                                "url": "https://oda.com/no/products/77-carrots",
                            }
                        ],
                    }
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products("household-1", ["carrot"], 1, 5)

    assert groups[0].query == "carrot"
    assert groups[0].products[0].provider == "oda"
    assert groups[0].products[0].product_id == "77"
    assert groups[0].products[0].description == "1 kg carrots"


@pytest.mark.asyncio
async def test_oda_adapter_expands_and_filters_known_false_food_matches():
    class Oda:
        def __init__(self):
            self.queries = None

        async def search_products(self, actor_id, queries, page, size):
            self.queries = queries

            def product(id, name, brand=None):
                return {
                    "id": id,
                    "name": name,
                    "description": name,
                    "brand": brand,
                    "price": "29.90",
                    "unitPrice": "29.90",
                    "unitName": "per kg",
                    "url": f"https://oda.com/no/products/{id}-{id}",
                }

            groups = {
                "Coffee": {
                    "query": "Coffee",
                    "hasMore": False,
                    "products": [
                        product(1, "Coffee Mate Original", "Nestlé"),
                        product(2, "Filter coffee", "Evergood"),
                    ],
                },
                "kaffe": {
                    "query": "kaffe",
                    "hasMore": True,
                    "products": [product(2, "Filter coffee", "Evergood"), product(3, "Kaffe")],
                },
                "Tomato": {
                    "query": "Tomato",
                    "hasMore": False,
                    "products": [product(4, "Tomatpure"), product(5, "Tomatoes")],
                },
                "tomat": {
                    "query": "tomat",
                    "hasMore": False,
                    "products": [product(6, "Tomat"), product(4, "Tomatpure")],
                },
            }
            return {"result": [groups[query] for query in queries]}

    service = Oda()
    groups = await OdaSearchAdapter(service).search_products(
        "household-1", ["Coffee", "Tomato"], 1, 5
    )

    assert service.queries == ["Coffee", "kaffe", "Tomato", "tomat"]
    assert [product.name for product in groups[0].products] == [
        "Filter coffee",
        "Kaffe",
    ]
    assert groups[0].has_more is True
    assert [product.name for product in groups[1].products] == ["Tomatoes", "Tomat"]


async def _media(pack_description):
    return ProductMedia(
        url="https://oda.com/no/products/10-product",
        image_url="https://images.oda.com/10.jpg",
        pack_description=pack_description,
        detail="Public Oda product page metadata.",
    )


@pytest.mark.asyncio
async def test_search_cache_reuses_queries_and_recomputes_current_quantities():
    from unittest.mock import AsyncMock

    provider = FakeProvider()
    inventory = FakeInventory([line()])
    media = AsyncMock(return_value=await _media("500 g"))
    service = BasketMatchingService(inventory, provider, media_fetcher=media)
    first = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    inventory.lines = [line("  CARROT  ", quantity="2"), line("milk", index=2)]
    second = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert [call[0] for call in provider.calls] == [["carrot"], ["milk"]]
    assert first.needs[0].candidates[0].recommended_packages == 2
    assert second.needs[0].candidates[0].recommended_packages == 4
    assert second.needs[0].candidates[0].pack_metadata_status == "unverified"


@pytest.mark.asyncio
async def test_cache_ttl_actor_page_size_and_force_refresh_are_independent():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    now = [datetime(2026, 11, 15, tzinfo=UTC)]
    provider = AsyncMock()
    provider.cache_context.return_value = 1
    provider.search_products.return_value = [
        ProviderSearchGroup(
            query="carrot",
            has_more=False,
            products=[
                ProviderProduct(provider="oda", product_id="1", name="Carrot", description="")
            ],
        )
    ]
    service = BasketMatchingService(FakeInventory([line()]), provider, clock=lambda: now[0])

    async def find(actor="a", **kwargs):
        return await service.find_products(actor, date(2026, 11, 16), date(2026, 11, 22), **kwargs)

    await find()
    now[0] += timedelta(minutes=4)
    await find()
    assert provider.search_products.await_count == 1
    await find("b")
    await find(page=2)
    await find(size=4)
    await find(force_refresh=True)
    assert provider.search_products.await_count == 5
    now[0] += timedelta(minutes=5)
    await find()
    assert provider.search_products.await_count == 6


@pytest.mark.asyncio
async def test_empty_results_expire_quickly_and_errors_are_never_cached():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    now = [datetime(2026, 11, 15, tzinfo=UTC)]
    provider = AsyncMock()
    provider.cache_context.return_value = 1
    provider.search_products.side_effect = [
        [ProviderSearchGroup(query="carrot", has_more=False, products=[])],
        TimeoutError(),
        [ProviderSearchGroup(query="carrot", has_more=False, products=[])],
    ]
    service = BasketMatchingService(FakeInventory([line()]), provider, clock=lambda: now[0])

    async def find():
        return await service.find_products("a", date(2026, 11, 16), date(2026, 11, 22))

    assert (await find()).needs[0].status == "no_results"
    await find()
    assert provider.search_products.await_count == 1
    now[0] += timedelta(seconds=20)
    assert (await find()).needs[0].status == "search_failed"
    assert (await find()).needs[0].status == "no_results"
    assert provider.search_products.await_count == 3


@pytest.mark.asyncio
async def test_connection_generation_changes_and_disconnect_prevent_cache_reuse():
    from unittest.mock import AsyncMock

    provider = AsyncMock()
    provider.cache_context.return_value = 1
    provider.search_products.return_value = [
        ProviderSearchGroup(query="carrot", has_more=False, products=[])
    ]
    service = BasketMatchingService(FakeInventory([line()]), provider)

    async def find():
        return await service.find_products("a", date(2026, 11, 16), date(2026, 11, 22))

    await find()
    provider.cache_context.return_value = 2
    await find()
    assert provider.search_products.await_count == 2
    provider.cache_context.return_value = None
    await find()
    await find()
    assert provider.search_products.await_count == 4


@pytest.mark.asyncio
async def test_generation_change_during_search_discards_result():
    from unittest.mock import AsyncMock

    provider = AsyncMock()
    provider.cache_context.return_value = 1

    async def changed(*args):
        provider.cache_context.return_value = 2
        return [ProviderSearchGroup(query="carrot", has_more=False, products=[])]

    provider.search_products.side_effect = changed
    service = BasketMatchingService(FakeInventory([line()]), provider)
    result = await service.find_products("a", date(2026, 11, 16), date(2026, 11, 22))
    assert result.needs[0].status == "search_failed"
    assert not service._search_cache


@pytest.mark.asyncio
async def test_concurrent_identical_searches_share_one_provider_read():
    import asyncio
    from unittest.mock import AsyncMock

    entered, release = asyncio.Event(), asyncio.Event()
    provider = AsyncMock()
    provider.cache_context.return_value = 1

    async def search(*args):
        entered.set()
        await release.wait()
        return [ProviderSearchGroup(query="carrot", has_more=False, products=[])]

    provider.search_products.side_effect = search
    service = BasketMatchingService(FakeInventory([line()]), provider)
    first = asyncio.create_task(service.find_products("a", date(2026, 11, 16), date(2026, 11, 22)))
    await entered.wait()
    second = asyncio.create_task(service.find_products("a", date(2026, 11, 16), date(2026, 11, 22)))
    # Wait until the second request has resolved its connection context.
    while provider.cache_context.await_count < 2:
        await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert provider.search_products.await_count == 1
    assert all(result.needs[0].status == "no_results" for result in results)


@pytest.mark.asyncio
async def test_progress_emits_success_while_another_batch_is_still_pending():
    import asyncio
    from unittest.mock import AsyncMock

    release, progressed = asyncio.Event(), asyncio.Event()
    provider = AsyncMock()
    provider.cache_context.return_value = 1

    async def search(actor, queries, page, size):
        if "slow" in queries:
            await release.wait()
            raise TimeoutError()
        return [ProviderSearchGroup(query=query, has_more=False, products=[]) for query in queries]

    async def progress(result):
        if result.name == "item 1":
            progressed.set()

    provider.search_products.side_effect = search
    service = BasketMatchingService(
        FakeInventory(
            [line(f"item {i}", index=i) for i in range(1, 11)] + [line("slow", index=11)]
        ),
        provider,
    )
    task = asyncio.create_task(
        service.find_products("a", date(2026, 11, 16), date(2026, 11, 22), progress=progress)
    )
    try:
        await asyncio.wait_for(progressed.wait(), 2)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert [need.status for need in result.needs[:10]] == ["no_results"] * 10
    assert result.needs[10].status == "search_failed"


@pytest.mark.asyncio
async def test_cache_is_bounded_and_provider_batches_remain_bounded(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    import home_kitchen.basket.matching as matching

    monkeypatch.setattr(matching, "SEARCH_CACHE_MAX_ENTRIES", 2)
    active = 0
    maximum_active = 0
    provider = AsyncMock()
    provider.cache_context.return_value = 1

    async def search(actor, queries, page, size):
        nonlocal active, maximum_active
        assert len(queries) <= 10
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return [ProviderSearchGroup(query=query, has_more=False, products=[]) for query in queries]

    provider.search_products.side_effect = search
    service = BasketMatchingService(
        FakeInventory([line(f"item {i}", index=i) for i in range(1, 42)]), provider
    )
    result = await service.find_products("a", date(2026, 11, 16), date(2026, 11, 22))
    assert len(result.needs) == 41
    assert len(service._search_cache) == 2
    assert maximum_active == 3
    assert not service._inflight


@pytest.mark.asyncio
async def test_media_enrichment_runs_across_needs_with_bounded_concurrency():
    import asyncio

    active = 0
    maximum_active = 0

    async def media(url):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return await _media("500 g")

    service = BasketMatchingService(
        FakeInventory([line(f"item {i}", index=i) for i in range(1, 6)]),
        FakeProvider(),
        media_fetcher=media,
    )
    result = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert maximum_active == 3
    assert all(need.candidates[0].recommended_packages == 2 for need in result.needs)


@pytest.mark.asyncio
async def test_adapter_reads_cache_context_in_threadpool():
    import threading
    from unittest.mock import Mock

    event_loop_thread = threading.get_ident()
    service = Mock()

    def context(actor):
        assert actor == "household-1"
        assert threading.get_ident() != event_loop_thread
        return 17

    service.search_cache_context.side_effect = context
    assert await OdaSearchAdapter(service).cache_context("household-1") == 17


@pytest.mark.asyncio
async def test_supplied_plan_snapshot_does_not_read_inventory_again():
    from unittest.mock import Mock

    inventory = Mock()
    snapshot = FakeInventory([line()]).plan(date(2026, 11, 16), date(2026, 11, 22))
    service = BasketMatchingService(
        inventory, FakeProvider(), media_fetcher=lambda _: _media("500 g")
    )
    result = await service.find_products(
        "household-1", date(2026, 11, 16), date(2026, 11, 22), plan=snapshot
    )
    inventory.plan.assert_not_called()
    assert result.needs[0].candidates[0].recommended_packages == 2
