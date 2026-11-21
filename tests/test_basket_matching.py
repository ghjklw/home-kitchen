from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from home_kitchen.basket.matching import (
    AvailabilityTarget,
    BasketMatchingService,
    OdaSearchAdapter,
    ProviderProduct,
    ProviderSearchGroup,
    _bokmal_ingredient_query,
    _explicit_pack,
    _identity_status,
    _oda_description_pack,
    _product_matches_ingredient,
    _query_expansions,
)
from home_kitchen.oda.media import ProductMedia

pytestmark = pytest.mark.mocked_provider


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
            "approximate": False,
            "provenance": None,
            "source_url": None,
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
    assert [candidate.recommended_packages for candidate in candidates] == [1, 1, 1, None]
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


async def test_oda_adapter_localizes_imported_ingredients_and_preserves_product_identity():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            assert queries == [
                "chicken breast, chopped",
                "hakket kyllingfilet",
                "fresh tomatoes",
                "ferske tomater",
                "creme fraiche",
                "crème fraîche",
            ]

            def product(product_id, name):
                return {
                    "id": product_id,
                    "name": name,
                    "description": "1 pk",
                    "brand": "Oda",
                    "price": "39.90",
                    "unitPrice": "39.90",
                    "unitName": "per pk",
                    "url": f"https://oda.com/no/products/{product_id}-item",
                }

            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [product(product_id, name)],
                    }
                    for query, product_id, name in [
                        ("chicken breast, chopped", 101, "Old El Paso Large tortillas"),
                        ("hakket kyllingfilet", 102, "Kyllingfilet, hakket"),
                        ("fresh tomatoes", 201, "Fresh fruit box"),
                        ("ferske tomater", 202, "Tomater, ferske"),
                        ("creme fraiche", 301, "Prior crème fraîche result"),
                        ("crème fraîche", 302, "Crème fraîche 300 g"),
                    ]
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products(
        "household-1",
        ["chicken breast, chopped", "fresh tomatoes", "creme fraiche"],
        1,
        5,
    )

    assert [[product.name for product in group.products] for group in groups] == [
        ["Kyllingfilet, hakket"],
        ["Tomater, ferske"],
        ["Prior crème fraîche result", "Crème fraîche 300 g"],
    ]
    assert [[product.product_id for product in group.products] for group in groups] == [
        ["102"],
        ["202"],
        ["301", "302"],
    ]


async def test_oda_adapter_rejects_bokmal_coffee_cream_and_tomato_puree():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            assert queries == ["kaffe", "Coffee Mate", "tomater", "tomatoes"]
            groups = {
                "kaffe": ["Kaffefløte", "Filterkaffe", "Fresh fruit box"],
                "Coffee Mate": ["Coffee Mate Original"],
                "tomater": ["Tomatpuré", "Tomater, ferske", "Fresh fruit box"],
                "tomatoes": ["Tomato paste", "Fresh tomatoes", "Fresh fruit box"],
            }
            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [
                            {
                                "id": product_id,
                                "name": name,
                                "description": name,
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": f"https://oda.com/no/products/{product_id}-item",
                            }
                            for product_id, name in enumerate(groups[query], start=1)
                        ],
                    }
                    for query in queries
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products(
        "household-1", ["kaffe", "Coffee Mate", "tomater", "tomatoes"], 1, 5
    )
    assert [product.name for product in groups[0].products] == ["Filterkaffe"]
    assert [product.name for product in groups[1].products] == ["Coffee Mate Original"]
    assert [product.name for product in groups[2].products] == ["Tomater, ferske"]
    assert [product.name for product in groups[3].products] == ["Fresh tomatoes"]


@pytest.mark.asyncio
async def test_oda_adapter_localizes_imported_ingredients_and_rejects_weak_identity_hits():
    class Oda:
        def __init__(self):
            self.calls = 0

        async def search_products(self, actor_id, queries, page, size):
            expected = [
                [
                    "all-purpose flour",
                    "hvetemel",
                    "large eggs, separated",
                    "egg L",
                    "unsalted butter, plus extra for greasing",
                    "usaltet smør",
                    "whole milk",
                    "helmelk",
                ],
                [
                    "vegetable oil",
                    "matolje",
                    "granulated sugar",
                    "sukker",
                    "vanilla extract",
                    "vaniljeekstrakt",
                    "cocoa powder, for dusting",
                    "kakaopulver",
                ],
            ]
            assert queries == expected[self.calls]
            self.calls += 1
            values = {
                "all-purpose flour": ["Loop No12 The Red S8 All White", "Almond flour"],
                "hvetemel": ["Siktet hvetemel"],
                "large eggs, separated": ["Old El Paso Large tortillas"],
                "egg L": ["Frittgående egg M/L"],
                "unsalted butter, plus extra for greasing": ["R Lev Vel diapers"],
                "usaltet smør": ["Usaltet smør"],
                "whole milk": ["Ritter Sport whole-hazelnut chocolate"],
                "helmelk": ["Helmelk"],
                "vegetable oil": ["Nivea shower oil"],
                "matolje": ["Rapsolje"],
                "granulated sugar": ["Dr. Oetker Unicorn Sugar Faces"],
                "sukker": ["Sukker"],
                "vanilla extract": ["Mövenpick vanilla ice cream"],
                "vaniljeekstrakt": ["Vaniljeekstrakt"],
                "cocoa powder, for dusting": ["Gluten-free spaghetti"],
                "kakaopulver": ["Kakaopulver"],
            }
            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [
                            {
                                "id": index * 10 + product_index,
                                "name": name,
                                "description": name,
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": f"https://oda.com/no/products/{index}-{product_index}-item",
                            }
                            for product_index, name in enumerate(values[query])
                        ],
                    }
                    for index, query in enumerate(queries)
                ]
            }

    names = [
        "all-purpose flour",
        "large eggs, separated",
        "unsalted butter, plus extra for greasing",
        "whole milk",
        "vegetable oil",
        "granulated sugar",
        "vanilla extract",
        "cocoa powder, for dusting",
    ]
    oda = Oda()
    adapter = OdaSearchAdapter(oda)
    groups = []
    for offset in range(0, len(names), 4):
        groups.extend(
            await adapter.search_products("household-1", names[offset : offset + 4], 1, 5)
        )
    assert [[product.name for product in group.products] for group in groups] == [
        ["Siktet hvetemel"],
        ["Frittgående egg M/L"],
        ["Usaltet smør"],
        ["Helmelk"],
        ["Rapsolje"],
        ["Sukker"],
        ["Vaniljeekstrakt"],
        ["Kakaopulver"],
    ]
    assert oda.calls == 2
    assert all(
        product.identity_status == "compatible" for group in groups for product in group.products
    )


@pytest.mark.asyncio
async def test_oda_adapter_keeps_manual_non_food_search_results():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            assert queries == ["dish soap"]
            return {
                "result": [
                    {
                        "query": "dish soap",
                        "hasMore": False,
                        "products": [
                            {
                                "id": 1,
                                "name": "Dish soap",
                                "description": "Dish soap",
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": "https://oda.com/no/products/1-dish-soap",
                            }
                        ],
                    }
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products("household-1", ["dish soap"], 1, 5)
    assert [product.name for product in groups[0].products] == ["Dish soap"]
    assert groups[0].products[0].identity_status == "unverified"


@pytest.mark.asyncio
async def test_explicit_manual_product_intent_overrides_ingredient_word_overlap():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            assert queries == ["olive oil shower gel"]
            return {
                "result": [
                    {
                        "query": queries[0],
                        "hasMore": False,
                        "products": [
                            {
                                "id": 55,
                                "name": "Olive Oil Shower Gel",
                                "description": "Shower gel",
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": "https://oda.com/no/products/55-shower-gel",
                            }
                        ],
                    }
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products(
        "household-1", ["olive oil shower gel"], 1, 5
    )
    assert groups[0].query == "olive oil shower gel"
    assert groups[0].products[0].name == "Olive Oil Shower Gel"
    assert groups[0].products[0].identity_status == "unverified"


@pytest.mark.asyncio
async def test_oda_adapter_rejects_tonic_as_hot_water_but_keeps_original_query():
    class Oda:
        async def search_products(self, actor_id, queries, page, size):
            assert queries == ["hot water", "varmt vann"]
            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [
                            {
                                "id": index,
                                "name": "Raspberry tonic",
                                "description": "Raspberry tonic",
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": f"https://oda.com/no/products/{index}-tonic",
                            }
                        ],
                    }
                    for index, query in enumerate(queries)
                ]
            }

    groups = await OdaSearchAdapter(Oda()).search_products("household-1", ["hot water"], 1, 5)
    assert groups[0].query == "hot water"
    assert groups[0].products == []


@pytest.mark.parametrize(
    ("query", "product_name", "matches"),
    [
        ("sukker", "Unicorn Sugar Faces", False),
        ("hvetemel", "Loop No12 Red S8 All White", False),
        ("hvetemel", "Siktet hvetemel", True),
        ("usaltet smør", "R Lev Vel diapers", False),
        ("granulated sugar", "Sugar Decorations", False),
        ("whole milk", "Whole Milk Chocolate", False),
        ("olive oil", "Olive Oil Shower Gel", False),
        ("milk", "Tine Lettmelk", True),
        ("milk", "Tine Helmelk", True),
        ("whole milk", "Tine Lettmelk", False),
        ("unsalted butter", "Tine Meierismør Usaltet", True),
        ("butter", "Tine Meierismør Usaltet", True),
        ("rice", "Basmatiris", True),
        ("vegetable oil", "Oda Olivenolje", True),
        ("olive oil", "Rapsolje", False),
        ("matolje", "Nivea Shower Oil", False),
        ("rice", "Lakris", False),
        ("butter", "Peanøttsmør", False),
        ("flour", "Almond flour", True),
        ("all-purpose flour", "Almond flour", False),
        ("lettmelk", "Tine Lettmelk", True),
        ("basmatiris", "Basmatiris", True),
        ("basmati rice", "Jasminris", False),
        ("low-fat milk", "Tine Helmelk", False),
        ("caster sugar", "Caster Sugar", True),
        ("caster sugar", "Powdered Sugar", False),
        ("powdered sugar", "Powdered Sugar", True),
        ("powdered sugar", "Caster Sugar", False),
        ("sugar", "Caster Sugar", True),
        ("all-purpose flour", "Loop No12 The Red S8 All White", False),
        ("large eggs", "Old El Paso Large tortillas", False),
        ("large eggs, separated", "Old El Paso Large tortillas", False),
        ("unsalted butter, plus extra for greasing", "R Lev Vel diapers", False),
        ("whole milk", "Ritter Sport whole-hazelnut chocolate", False),
        ("vegetable oil", "Nivea shower oil", False),
        ("hot water", "Raspberry tonic water", False),
        ("granulated sugar", "Dr. Oetker Unicorn Sugar Faces", False),
        ("caster sugar", "Dr. Oetker Unicorn Sugar Faces", False),
        ("vanilla extract", "Mövenpick vanilla ice cream", False),
        ("cocoa powder, for dusting", "Gluten-free spaghetti", False),
        ("Baking powder", "Den Gode Baker Glutenfri Pepperkakedeig", False),
        ("Baking powder", "Freia Bakepulver", True),
        ("Vegetable oil", "Nivea Hawaii Flower & Oil Shower", False),
    ],
)
def test_ingredient_identity_handles_bokmal_compounds_and_product_classes(
    query, product_name, matches
):
    product = ProviderProduct(
        provider="oda", product_id="1", name=product_name, description=product_name
    )
    assert _product_matches_ingredient(query, product) is matches


def test_manual_and_unknown_searches_are_retained_as_unverified():
    shower_gel = ProviderProduct(
        provider="oda",
        product_id="gel",
        name="Olive Oil Shower Gel",
        description="",
    )
    unknown = ProviderProduct(
        provider="oda", product_id="unknown", name="Special pantry item", description=""
    )
    assert _bokmal_ingredient_query("olive oil shower gel") is None
    assert _identity_status("olive oil shower gel", shower_gel) == "unverified"
    assert _identity_status("unmapped pantry term", unknown) == "unverified"
    assert _bokmal_ingredient_query("almond flour") is None
    assert _identity_status("almond flour", unknown) == "unverified"
    assert _bokmal_ingredient_query("coconut milk") is None
    assert _identity_status("coconut milk", unknown) == "unverified"
    assert _bokmal_ingredient_query("gluten-free flour") is None
    assert _identity_status("gluten-free flour", unknown) == "unverified"
    assert (
        ProviderProduct(
            provider="oda", product_id="missing", name="Unknown", description=""
        ).identity_status
        == "unverified"
    )


def test_identity_table_localizes_supported_aliases():
    assert _bokmal_ingredient_query("unsalted butter") == "usaltet smør"
    assert _bokmal_ingredient_query("whole milk") == "helmelk"
    assert _bokmal_ingredient_query("olive oil") == "olivenolje"
    assert _bokmal_ingredient_query("basmati rice") == "basmatiris"


@pytest.mark.parametrize(
    ("query", "localized", "product_name"),
    [
        ("fresh tomatoes", "ferske tomater", "Tomater, ferske"),
        ("chopped tomatoes", "hakket tomater", "Tomater, ferske"),
        ("diced tomatoes", "terninger tomater", "Tomater, ferske"),
        ("minced tomatoes", "finhakket tomater", "Tomater, ferske"),
        ("ground coffee", "malt kaffe", "Filterkaffe"),
        ("whole milk", "helmelk", "Tine Helmelk"),
    ],
)
def test_declared_supported_form_qualifiers_share_query_and_product_identity(
    query, localized, product_name
):
    product = ProviderProduct(
        provider="oda", product_id="form", name=product_name, description=product_name
    )
    assert _bokmal_ingredient_query(query) == localized
    assert _identity_status(query, product) == "compatible"
    assert _query_expansions(query)[0] == query


@pytest.mark.parametrize(
    "query",
    [
        "malt coffee",  # unsupported localization is deliberately not inferred
        "frozen tomatoes",
        "canned tomatoes",
        "dried tomatoes",
        "smoked tomatoes",
        "salted tomatoes",
    ],
)
def test_unmodeled_semantic_form_qualifiers_stay_unverified_and_untranslated(query):
    product = ProviderProduct(
        provider="oda", product_id="fresh", name="Tomater, ferske", description=""
    )
    assert _bokmal_ingredient_query(query) is None
    assert _identity_status(query, product) == "unverified"
    assert _query_expansions(query) == (query,)


@pytest.mark.parametrize(
    ("query", "product_name", "expected"),
    [
        ("coffee", "Coffee Whitener", None),
        ("ground coffee", "Kaffebønner", None),
        ("ground coffee", "Filterkaffe", "compatible"),
        ("malt kaffe", "Kaffebønner", None),
        ("dried tomatoes", "Tomater, ferske", "unverified"),
        ("canned tomatoes", "Tomater, ferske", "unverified"),
    ],
)
def test_semantic_forms_do_not_collapse_to_generic_family(query, product_name, expected):
    product = ProviderProduct(
        provider="oda", product_id="form", name=product_name, description=product_name
    )
    assert _identity_status(query, product) == expected


@pytest.mark.parametrize(
    ("query", "localized", "product_name"),
    [
        ("all-purpose flour", "hvetemel", "Siktet hvetemel"),
        ("large eggs", "egg L", "Frittgående egg M/L"),
        ("unsalted butter", "usaltet smør", "Tine Meierismør Usaltet"),
        ("whole milk", "helmelk", "Tine Helmelk"),
        ("granulated sugar", "sukker", "Sukker"),
        ("vegetable oil", "matolje", "Rapsolje"),
        ("olive oil", "olivenolje", "Olivenolje"),
        ("basmati rice", "basmatiris", "Basmatiris"),
        ("tomato", "tomat", "Tomater, ferske"),
        ("coffee", "kaffe", "Filterkaffe"),
        ("vanilla extract", "vaniljeekstrakt", "Vaniljeekstrakt"),
        ("cocoa powder", "kakaopulver", "Kakaopulver"),
        ("baking powder", "bakepulver", "Freia Bakepulver"),
        ("hot water", "varmt vann", "Varmt vann"),
        ("chicken breast", "kyllingfilet", "Kyllingfilet"),
        ("creme fraiche", "crème fraîche", "Crème fraîche 300 g"),
    ],
)
def test_identity_table_aliases_expand_and_match_the_same_family(query, localized, product_name):
    product = ProviderProduct(
        provider="oda", product_id="1", name=product_name, description=product_name
    )
    assert _bokmal_ingredient_query(query) == localized
    assert _identity_status(query, product) == "compatible"


@pytest.mark.asyncio
async def test_search_cache_only_reuses_semantically_eligible_oda_products():
    class Oda:
        def __init__(self):
            self.calls = 0

        def search_cache_context(self, actor_id):
            assert actor_id == "household-1"
            return 1

        async def search_products(self, actor_id, queries, page, size):
            self.calls += 1
            assert queries == ["large eggs, separated", "egg L"]
            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [
                            {
                                "id": index * 10 + product_index,
                                "name": name,
                                "description": name,
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": f"https://oda.com/no/products/{index}-{product_index}-item",
                            }
                            for product_index, name in enumerate(names)
                        ],
                    }
                    for index, (query, names) in enumerate(
                        [
                            (
                                "large eggs, separated",
                                ["Old El Paso Large tortillas", "Mystery Pantry Item"],
                            ),
                            ("egg L", ["Frittgående egg M/L"]),
                        ]
                    )
                ]
            }

    oda = Oda()
    service = BasketMatchingService(
        FakeInventory([line("large eggs, separated", unit="item")]),
        OdaSearchAdapter(oda),
        media_fetcher=lambda _: _media(None),
    )
    first = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    second = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))

    for result in (first, second):
        assert [candidate.name for candidate in result.needs[0].candidates] == [
            "Mystery Pantry Item",
            "Frittgående egg M/L",
        ]
        assert result.needs[0].candidates[0].identity_status == "unverified"
        assert result.needs[0].candidates[1].identity_status == "compatible"
    assert oda.calls == 1


@pytest.mark.asyncio
async def test_offline_synthetic_uat_failure_regression_pack():
    """Verify all 9 observed UAT failures, positive Norwegian queries, coffee/tomato variants, and non-food searches."""

    class FakeOdaBackend:
        def __init__(self):
            self.calls = 0

        async def search_products(self, actor_id, queries, page, size):
            self.calls += 1
            # Mock provider returns synthetic products for each query
            product_catalog = {
                "all-purpose flour": ["Loop No12 The Red S8 All White", "Almond flour"],
                "hvetemel": ["Siktet hvetemel"],
                "large eggs": ["Old El Paso Large tortillas"],
                "large eggs, separated": ["Old El Paso Large tortillas"],
                "egg L": ["Frittgående egg M/L"],
                "unsalted butter, plus extra for greasing": ["R Lev Vel diapers"],
                "usaltet smør": ["Usaltet smør"],
                "whole milk": ["Ritter Sport whole-hazelnut chocolate"],
                "helmelk": ["Helmelk"],
                "vegetable oil": ["Nivea shower oil"],
                "matolje": ["Rapsolje"],
                "hot water": ["Raspberry tonic water"],
                "varmt vann": ["Raspberry tonic water"],
                "granulated sugar": ["Dr. Oetker Unicorn Sugar Faces"],
                "caster sugar": ["Dr. Oetker Unicorn Sugar Faces", "Caster sugar"],
                "sukker": ["Sukker"],
                "vanilla extract": ["Mövenpick vanilla ice cream"],
                "vaniljeekstrakt": ["Vaniljeekstrakt"],
                "cocoa powder, for dusting": ["Gluten-free spaghetti"],
                "kakaopulver": ["Kakaopulver"],
                "coffee": ["Filterkaffe", "Coffee Whitener", "Coffee Mate"],
                "fresh tomatoes": ["Tomater, ferske", "Tomatpuré", "Fresh fruit box"],
                "tomato paste": ["Tomatpuré", "Tomater, ferske"],
                "dish soap": ["Dish soap"],
                "olive oil shower gel": ["Olive Oil Shower Gel"],
            }
            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [
                            {
                                "id": hash((query, name)) % 100000,
                                "name": name,
                                "description": name,
                                "price": "10",
                                "unitPrice": "10",
                                "unitName": "per item",
                                "url": f"https://oda.com/no/products/{hash((query, name)) % 100000}-item",
                            }
                            for name in product_catalog.get(query, [])
                        ],
                    }
                    for query in queries
                ]
            }

    backend = FakeOdaBackend()
    adapter = OdaSearchAdapter(backend)

    # 1. Test all 9 observed UAT failure queries against adapter (in bounded batches of at most adapter.max_queries)
    uat_queries = [
        "all-purpose flour",
        "large eggs, separated",
        "unsalted butter, plus extra for greasing",
        "whole milk",
        "vegetable oil",
        "hot water",
        "granulated sugar",
        "vanilla extract",
        "cocoa powder, for dusting",
    ]
    groups = []
    for offset in range(0, len(uat_queries), adapter.max_queries):
        groups.extend(
            await adapter.search_products(
                "household-1", uat_queries[offset : offset + adapter.max_queries], 1, 5
            )
        )
    results_by_query = {group.query: [p.name for p in group.products] for group in groups}

    # 1: All-purpose flour must reject snus and almond flour, keep hvetemel
    assert results_by_query["all-purpose flour"] == ["Siktet hvetemel"]
    # 2: Large eggs, separated must reject tortillas, keep Frittgående egg M/L
    assert results_by_query["large eggs, separated"] == ["Frittgående egg M/L"]
    # 3: Unsalted butter, plus extra for greasing must reject diapers, keep Usaltet smør
    assert results_by_query["unsalted butter, plus extra for greasing"] == ["Usaltet smør"]
    # 4: Whole milk must reject hazelnut chocolate, keep Helmelk
    assert results_by_query["whole milk"] == ["Helmelk"]
    # 5: Vegetable oil must reject shower oil, keep Rapsolje
    assert results_by_query["vegetable oil"] == ["Rapsolje"]
    # 6: Hot water must reject raspberry tonic water and remain honestly unresolved (empty)
    assert results_by_query["hot water"] == []
    # 7: Granulated sugar must reject sugar faces, keep Sukker
    assert results_by_query["granulated sugar"] == ["Sukker"]
    # 8: Vanilla extract must reject vanilla ice cream, keep Vaniljeekstrakt
    assert results_by_query["vanilla extract"] == ["Vaniljeekstrakt"]
    # 9: Cocoa powder, for dusting must reject spaghetti, keep Kakaopulver
    assert results_by_query["cocoa powder, for dusting"] == ["Kakaopulver"]

    # Also verify Large eggs (without separated) and caster sugar variants
    extra_uat = await adapter.search_products("household-1", ["large eggs", "caster sugar"], 1, 5)
    extra_results = {group.query: [p.name for p in group.products] for group in extra_uat}
    assert extra_results["large eggs"] == ["Frittgående egg M/L"]
    assert extra_results["caster sugar"] == ["Caster sugar"]

    # 2. Positive Norwegian queries/matches
    no_queries = [
        "hvetemel",
        "egg L",
        "usaltet smør",
        "helmelk",
        "matolje",
        "sukker",
        "vaniljeekstrakt",
        "kakaopulver",
    ]
    no_groups = []
    for offset in range(0, len(no_queries), adapter.max_queries):
        no_groups.extend(
            await adapter.search_products(
                "household-1", no_queries[offset : offset + adapter.max_queries], 1, 5
            )
        )
    no_results = {g.query: [p.name for p in g.products] for g in no_groups}
    assert no_results["hvetemel"] == ["Siktet hvetemel"]
    assert no_results["egg L"] == ["Frittgående egg M/L"]
    assert no_results["usaltet smør"] == ["Usaltet smør"]
    assert no_results["helmelk"] == ["Helmelk"]
    assert no_results["matolje"] == ["Rapsolje"]
    assert no_results["sukker"] == ["Sukker"]
    assert no_results["vaniljeekstrakt"] == ["Vaniljeekstrakt"]
    assert no_results["kakaopulver"] == ["Kakaopulver"]

    # 3. Prior coffee and tomato cases
    prior_groups = await adapter.search_products(
        "household-1", ["coffee", "fresh tomatoes", "tomato paste"], 1, 5
    )
    prior_results = {g.query: [p.name for p in g.products] for g in prior_groups}
    assert prior_results["coffee"] == ["Filterkaffe"]
    assert prior_results["fresh tomatoes"] == ["Tomater, ferske"]
    assert prior_results["tomato paste"] == ["Tomatpuré"]

    # 4. Valid manual non-food searches
    non_food_groups = await adapter.search_products(
        "household-1", ["dish soap", "olive oil shower gel"], 1, 5
    )
    non_food_results = {g.query: [p.name for p in g.products] for g in non_food_groups}
    assert non_food_results["dish soap"] == ["Dish soap"]
    assert non_food_results["olive oil shower gel"] == ["Olive Oil Shower Gel"]
    assert all(p.identity_status == "unverified" for g in non_food_groups for p in g.products)


def test_issue_135_baking_powder_and_flower_oil_shower_suppression():
    pepperkakedeig = ProviderProduct(
        provider="oda",
        product_id="dough-1",
        name="Den Gode Baker Glutenfri Pepperkakedeig",
        description="Glutenfri pepperkakedeig",
    )
    bakepulver = ProviderProduct(
        provider="oda",
        product_id="bp-1",
        name="Freia Bakepulver",
        description="Bakepulver",
    )
    flower_oil_shower = ProviderProduct(
        provider="oda",
        product_id="shower-1",
        name="Nivea Hawaii Flower & Oil Shower",
        description="Dusjsåpe",
    )

    assert _bokmal_ingredient_query("Baking powder") == "bakepulver"
    assert _query_expansions("Baking powder") == ("Baking powder", "bakepulver")
    assert _identity_status("Baking powder", pepperkakedeig) is None
    assert _identity_status("Baking powder", bakepulver) == "compatible"
    assert _identity_status("bakepulver", pepperkakedeig) is None
    assert _identity_status("bakepulver", bakepulver) == "compatible"
    assert _identity_status("Vegetable oil", flower_oil_shower) is None


@pytest.mark.asyncio
async def test_search_cache_version_invalidation(monkeypatch):
    from unittest.mock import AsyncMock

    import home_kitchen.basket.matching as matching

    provider = AsyncMock()
    provider.cache_context.return_value = 1
    provider.search_products.return_value = [
        ProviderSearchGroup(
            query="all-purpose flour",
            has_more=False,
            products=[
                ProviderProduct(
                    provider="oda",
                    product_id="1",
                    name="Siktet hvetemel",
                    description="",
                    identity_status="compatible",
                )
            ],
        )
    ]
    service = BasketMatchingService(
        FakeInventory([line("all-purpose flour")]),
        provider,
        media_fetcher=lambda _: _media(None),
    )

    # First call fills the cache under current SEARCH_CACHE_VERSION
    await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert provider.search_products.await_count == 1

    # Second identical call hits the cache
    await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert provider.search_products.await_count == 1

    # Simulating a version bump invalidates prior cache entries
    monkeypatch.setattr(matching, "SEARCH_CACHE_VERSION", matching.SEARCH_CACHE_VERSION + 1)
    await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert provider.search_products.await_count == 2


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


@pytest.mark.asyncio
async def test_pack_count_recommends_whole_sku_packages_and_preserves_item_estimate_evidence():
    coffee = line("coffee", quantity="1.2", unit="pack", index=1)
    tomato = line("tomato", quantity="1.333", unit="item", index=2)
    tomato.approximate = True
    tomato.provenance = "recipe-derived estimate"
    tomato.source_url = "https://example.test/recipe/tomato"
    service = BasketMatchingService(
        FakeInventory([coffee, tomato]),
        FakeProvider(),
        media_fetcher=lambda _: _media("500 g"),
    )

    result = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))

    coffee_match, tomato_match = result.needs
    coffee_candidate = coffee_match.candidates[0]
    tomato_candidate = tomato_match.candidates[0]
    assert coffee_candidate.pack_quantity == Decimal("500")
    assert coffee_candidate.pack_unit == "g"
    assert coffee_candidate.recommended_packages == 2
    assert coffee_candidate.recommended_fulfillment_quantity == Decimal("2")
    assert tomato_candidate.pack_quantity == Decimal("500")
    assert tomato_candidate.pack_unit == "g"
    assert tomato_candidate.recommended_packages is None
    assert tomato_match.approximate is True
    assert tomato_match.provenance == "recipe-derived estimate"
    assert str(tomato_match.source_url) == "https://example.test/recipe/tomato"


@pytest.mark.asyncio
async def test_query_overrides_refine_targeted_need_without_changing_other_needs():
    carrot = line("carrot", quantity="1", unit="kg", index=1)
    milk = line("milk", quantity="1", unit="l", index=2)
    provider = FakeProvider()
    service = BasketMatchingService(
        FakeInventory([carrot, milk]),
        provider,
        media_fetcher=lambda _: _media("1 l"),
    )

    result = await service.find_products(
        "household-1",
        date(2026, 11, 16),
        date(2026, 11, 22),
        query_overrides={milk.id: "Tine Helmelk 1l"},
    )

    carrot_match, milk_match = result.needs
    assert carrot_match.search_query == "carrot"
    assert carrot_match.candidates[0].match_query == "carrot"
    assert milk_match.search_query == "Tine Helmelk 1l"
    assert milk_match.candidates[0].match_query == "Tine Helmelk 1l"


@pytest.mark.asyncio
async def test_slow_or_failing_batch_does_not_block_fast_needs_progress():
    import asyncio

    carrot = line("carrot", quantity="1", unit="kg", index=1)
    milk = line("milk", quantity="1", unit="l", index=2)
    eggs = line("eggs", quantity="6", unit="item", index=3)

    milk_gate = asyncio.Event()
    progressed: list[tuple[UUID, str]] = []

    class PartialProvider:
        max_queries = 1

        async def search_products(self, actor_id, queries, page, size):
            query = queries[0]
            if query == "milk":
                await milk_gate.wait()
                raise TimeoutError("milk timed out")
            return [
                ProviderSearchGroup(
                    query=query,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id=f"id-{query}",
                            name=f"{query} product",
                            description="1 kg",
                            price="29.90",
                            available=True,
                        )
                    ],
                )
            ]

    async def on_progress(need):
        progressed.append((need.need_id, need.status))
        if len(progressed) == 2:
            milk_gate.set()

    service = BasketMatchingService(
        FakeInventory([carrot, milk, eggs]),
        PartialProvider(),
        media_fetcher=lambda _: _media("1 kg"),
    )
    result = await asyncio.wait_for(
        service.find_products(
            "household-1",
            date(2026, 11, 16),
            date(2026, 11, 22),
            progress=on_progress,
        ),
        timeout=2,
    )

    assert progressed[:2] == [(carrot.id, "matched"), (eggs.id, "matched")]
    assert progressed[2] == (milk.id, "search_failed")
    assert [n.status for n in result.needs] == ["matched", "search_failed", "matched"]


@pytest.mark.asyncio
async def test_check_products_availability_surfaces_unavailable_and_price_changed_items():
    class LiveProvider:
        def __init__(self):
            self.calls = 0

        async def search_products(self, actor_id, queries, page, size):
            self.calls += 1
            return [
                ProviderSearchGroup(
                    query=q,
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="101",
                            name="Tine Helmelk 1L",
                            description="1 l",
                            price="32.50",
                            available=True,
                        ),
                        ProviderProduct(
                            provider="oda",
                            product_id="202",
                            name="Prior Egg 12pk",
                            description="12 stk",
                            price="49.90",
                            available=False,
                        ),
                    ],
                )
                for q in queries
            ]

    provider = LiveProvider()
    service = BasketMatchingService(
        FakeInventory([]),
        provider,
        clock=lambda: datetime(2026, 11, 20, 12, 0, tzinfo=UTC),
    )
    targets = [
        AvailabilityTarget(
            need_id=UUID(int=1),
            need_name="Melk",
            provider="oda",
            product_id="101",
            product_name="Tine Helmelk 1L",
            previous_available=True,
            previous_price="29.90",
        ),
        AvailabilityTarget(
            need_id=UUID(int=2),
            need_name="Egg",
            provider="oda",
            product_id="202",
            product_name="Prior Egg 12pk",
            previous_available=True,
            previous_price="49.90",
        ),
        AvailabilityTarget(
            need_id=UUID(int=3),
            need_name="Smør",
            provider="oda",
            product_id="303",
            product_name="Savnet Smør",
            previous_available=True,
            previous_price="54.90",
        ),
    ]

    outcomes = await service.check_products_availability("household-1", targets)
    assert len(outcomes) == 3
    assert outcomes[0].availability_status == "available"
    assert outcomes[0].price_changed is True
    assert outcomes[0].current_price == "32.50"
    assert outcomes[1].availability_status == "unavailable"
    assert outcomes[1].available is False
    assert outcomes[2].availability_status == "unknown"
    assert outcomes[2].available is None
    # Calling again must bypass cache and query provider freshly.
    await service.check_products_availability("household-1", targets[:1])
    assert provider.calls >= 3


@pytest.mark.asyncio
async def test_issue_135_uat_examples_and_bilingual_matches_through_service():
    """Verify all six Issue #135 UAT examples, extended negatives, and bilingual matches end-to-end."""

    class DeterministicOdaService:
        def search_cache_context(self, actor_id):
            return 1

        async def search_products(self, actor_id, queries, page, size):
            catalog = {
                "Hot water": ["Tundra Tonic Water Raspberry", "Farris Bris Mango"],
                "varmt vann": ["Imsdal Vann", "Nivea Micellar Water"],
                "unsalted butter, plus extra for greasing": [
                    "Rummo Glutenfri spaghetti",
                    "Tine Meierismør Saltet",
                    "Body Butter Shea",
                ],
                "usaltet smør": ["Tine Meierismør Usaltet"],
                "Vegetable oil": ["Nivea Hawaii Flower & Oil Shower", "Neutral Babyolje"],
                "matolje": ["Eldorado Rapsolje"],
                "All-purpose flour": ["LOOP nicotine pouches", "Møllerens Rugmel", "Potetmel"],
                "hvetemel": ["Møllerens Siktet Hvetemel"],
                "Baking powder": [
                    "Den Gode Baker Glutenfri Pepperkakedeig",
                    "Pepperkaker",
                    "Vaskepulver",
                ],
                "bakepulver": ["Freia Bakepulver"],
                "Large eggs": ["Old El Paso tortillas", "Prior Egg 12 stk M", "Kinderegg"],
                "egg L": ["Prior Egg 12 stk L"],
                "chicken breast": ["Kyllinglår", "Kyllingbuljong"],
                "kyllingfilet": ["Prior Kyllingfilet 400 g"],
                "butter": ["Peanøttsmør", "Kakaosmør"],
                "smør": ["Tine Meierismør"],
            }
            result = [
                {
                    "query": query,
                    "hasMore": False,
                    "products": [
                        {
                            "id": (abs(hash((query, name))) % 900000) + 1000,
                            "name": name,
                            "description": "500 g",
                            "price": "39.90",
                            "unitPrice": "79.80",
                            "unitName": "per kg",
                            "available": True,
                            "url": f"https://oda.com/no/products/{abs(hash((query, name))) % 900000 + 1000}-item",
                        }
                        for name in catalog.get(query, [])
                    ],
                }
                for query in queries
            ]
            # Include an unrequested provider group whenever room remains under Oda's 10-group limit.
            if len(result) < 10:
                result.append(
                    {
                        "query": "leaked unrelated query",
                        "hasMore": False,
                        "products": [
                            {
                                "id": 999999,
                                "name": "Leaked Unrelated Product",
                                "description": "500 g",
                                "price": "19.90",
                                "unitPrice": "39.80",
                                "unitName": "per kg",
                                "available": True,
                                "url": "https://oda.com/no/products/999999-leaked",
                            }
                        ],
                    }
                )
            return {"result": result}

    needs = [
        line("Hot water", "200", "ml", index=1),
        line("unsalted butter, plus extra for greasing", "100", "g", index=2),
        line("Vegetable oil", "100", "ml", index=3),
        line("All-purpose flour", "250", "g", index=4),
        line("Baking powder", "10", "g", index=5),
        line("Large eggs", "2", "item", index=6),
        line("chicken breast", "400", "g", index=7),
        line("kyllingfilet", "400", "g", index=8),
        line("butter", "100", "g", index=9),
        line("smør", "100", "g", index=10),
    ]
    service = BasketMatchingService(
        FakeInventory(needs),
        OdaSearchAdapter(DeterministicOdaService()),
        media_fetcher=lambda _: _media(None),
    )

    result = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    by_name = {need.name: need for need in result.needs}

    # Hot water has no matching grocery item -> remains honestly unresolved (no_results)
    assert by_name["Hot water"].status == "no_results"
    assert by_name["Hot water"].candidates == []

    # The remaining 5 UAT failure cases reject irrelevant hits and retain valid localized matches
    assert [c.name for c in by_name["unsalted butter, plus extra for greasing"].candidates] == [
        "Tine Meierismør Usaltet"
    ]
    assert [c.name for c in by_name["Vegetable oil"].candidates] == ["Eldorado Rapsolje"]
    assert [c.name for c in by_name["All-purpose flour"].candidates] == [
        "Møllerens Siktet Hvetemel"
    ]
    assert [c.name for c in by_name["Baking powder"].candidates] == ["Freia Bakepulver"]
    assert [c.name for c in by_name["Large eggs"].candidates] == ["Prior Egg 12 stk L"]

    # Positive bilingual matches (chicken breast / kyllingfilet and butter / smør)
    assert [c.name for c in by_name["chicken breast"].candidates] == ["Prior Kyllingfilet 400 g"]
    assert [c.name for c in by_name["kyllingfilet"].candidates] == ["Prior Kyllingfilet 400 g"]
    assert [c.name for c in by_name["butter"].candidates] == ["Tine Meierismør"]
    assert [c.name for c in by_name["smør"].candidates] == ["Tine Meierismør"]

    for need_name in (
        "unsalted butter, plus extra for greasing",
        "Vegetable oil",
        "All-purpose flour",
        "Baking powder",
        "Large eggs",
        "chicken breast",
        "kyllingfilet",
        "butter",
        "smør",
    ):
        need_result = by_name[need_name]
        assert need_result.status == "matched"
        assert need_result.candidates[0].identity_status == "compatible"
        assert need_result.candidates[0].match_query == need_name
        assert need_result.candidates[0].pack_metadata_status in {"unknown", "unverified"}

    # Ensure leaked product never enters any need
    all_candidate_names = [c.name for n in result.needs for c in n.candidates]
    assert "Leaked Unrelated Product" not in all_candidate_names


@pytest.mark.asyncio
async def test_oda_adapter_deduplicates_case_variants_and_service_rejects_mismatched_group_query():
    class CaseOda:
        def __init__(self):
            self.seen_queries = []

        async def search_products(self, actor_id, queries, page, size):
            self.seen_queries.append(list(queries))
            return {
                "result": [
                    {
                        "query": query,
                        "hasMore": False,
                        "products": [
                            {
                                "id": 1,
                                "name": "Tine Meierismør",
                                "description": "500 g",
                                "price": "45.00",
                                "unitPrice": "90.00",
                                "unitName": "per kg",
                                "url": "https://oda.com/no/products/1-smor",
                            }
                        ],
                    }
                    for query in queries
                ]
            }

    oda = CaseOda()
    groups = await OdaSearchAdapter(oda).search_products("household-1", ["Butter", "butter"], 1, 5)
    assert oda.seen_queries == [["Butter", "smør"]]
    assert [g.query for g in groups] == ["Butter", "butter"]
    assert all([p.name for p in g.products] == ["Tine Meierismør"] for g in groups)

    # Also verify BasketMatchingService filters incompatible products even if provider skips adapter
    class UnfilteredProvider:
        async def search_products(self, actor_id, queries, page, size):
            return [
                ProviderSearchGroup(
                    query=queries[0],
                    has_more=False,
                    products=[
                        ProviderProduct(
                            provider="oda",
                            product_id="bad-1",
                            name="Nivea Hawaii Flower & Oil Shower",
                            description="Title 500 ml should not infer pack",
                            unit_price="50.00",
                            unit_name="per l",
                        )
                    ],
                )
            ]

    service = BasketMatchingService(
        FakeInventory([line("Vegetable oil", "100", "ml")]),
        UnfilteredProvider(),
        media_fetcher=lambda _: _media(None),
    )
    res = await service.find_products("household-1", date(2026, 11, 16), date(2026, 11, 22))
    assert res.needs[0].status == "no_results"
    assert res.needs[0].candidates == []


@pytest.mark.parametrize(
    ("query", "expected_localized", "bad_product", "good_product"),
    [
        (
            "unsalted butter, melted and cooled",
            "usaltet smør",
            "Rummo Glutenfri spaghetti",
            "Tine Meierismør Usaltet",
        ),
        (
            "all-purpose flour, sifted",
            "hvetemel",
            "LOOP nicotine pouches",
            "Møllerens Siktet Hvetemel",
        ),
        (
            "large eggs, lightly beaten",
            "egg L",
            "Old El Paso tortillas",
            "Prior Egg 12 stk L",
        ),
        (
            "baking powder, sifted",
            "bakepulver",
            "Den Gode Baker Glutenfri Pepperkakedeig",
            "Freia Bakepulver",
        ),
    ],
)
def test_recipe_prep_suffixes_normalize_and_filter_unrelated_products(
    query, expected_localized, bad_product, good_product
):
    assert _bokmal_ingredient_query(query) == expected_localized
    assert (
        _identity_status(
            query,
            ProviderProduct(provider="oda", product_id="bad", name=bad_product, description=""),
        )
        is None
    )
    assert (
        _identity_status(
            query,
            ProviderProduct(provider="oda", product_id="good", name=good_product, description=""),
        )
        == "compatible"
    )
