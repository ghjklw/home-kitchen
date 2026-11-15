"""Tests for the shared generic-food catalogue and its isolated migration seed."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from home_kitchen.catalogue.models import (
    MATVARETABELLEN_ATTRIBUTION,
    MATVARETABELLEN_URL,
    FoodCatalogueItem,
    food_catalogue_id,
)
from home_kitchen.catalogue.repository import PostgresFoodCatalogueRepository
from home_kitchen.catalogue.service import FoodCatalogueService


def item(
    source_item_id: str,
    name: str,
    *,
    keywords: list[str] | None = None,
    display_name: str | None = None,
) -> FoodCatalogueItem:
    return FoodCatalogueItem(
        id=food_catalogue_id(source_item_id),
        source="matvaretabellen",
        source_item_id=source_item_id,
        source_name=name,
        display_name=display_name or name,
        food_group_id="1.1",
        category="Dairy",
        search_keywords=keywords or [],
        source_version="2026",
        source_url=MATVARETABELLEN_URL,
        source_attribution=MATVARETABELLEN_ATTRIBUTION,
    )


class MemoryCatalogue:
    def __init__(self, items: list[FoodCatalogueItem]):
        self.items = items

    def list_all(self) -> list[FoodCatalogueItem]:
        return self.items.copy()


def test_autocomplete_finds_norwegian_names_and_official_search_keywords():
    service = FoodCatalogueService(
        MemoryCatalogue(
            [
                item("01.291", "Melk, uspesifisert", keywords=["mjølk"]),
                item("01.272", "Helmelk, laktosefri", keywords=["heilmjølk"]),
                item("05.010", "Hvetemel, siktet", keywords=["kveitemjøl"]),
            ]
        )
    )

    milk_matches = [entry.source_item_id for entry in service.autocomplete("MELK")]
    assert milk_matches[0] == "01.291"
    assert set(milk_matches) == {"01.291", "01.272"}
    assert [entry.source_item_id for entry in service.autocomplete("  mjølk  ")][0] == "01.291"
    assert [entry.source_item_id for entry in service.autocomplete("kveitemjøl")] == ["05.010"]


def test_variants_remain_separate_search_results():
    entries = [
        item("01.011", "Yoghurt, naturell"),
        item("01.275", "Yoghurt, naturell, laktosefri"),
        item("06.036", "Gulrot, norsk, rå"),
        item("06.743", "Gulrot, importert, fryst"),
        item("06.069", "Tomat, norsk, rå"),
        item("06.070", "Tomat, hermetisk"),
    ]
    service = FoodCatalogueService(MemoryCatalogue(entries))

    lactose_free = service.autocomplete("laktosefri")
    assert [entry.source_item_id for entry in lactose_free] == ["01.275"]
    assert {entry.source_item_id for entry in service.autocomplete("gulrot")} == {
        "06.036",
        "06.743",
    }
    assert {entry.source_item_id for entry in service.autocomplete("tomat")} == {
        "06.069",
        "06.070",
    }


def test_autocomplete_is_bounded_deterministic_and_does_not_return_all_for_blank_query():
    entries = [item(f"06.{index:03}", f"Gulrot variant {index}") for index in range(1, 5)]
    service = FoodCatalogueService(MemoryCatalogue(entries))

    assert service.autocomplete("  ") == []
    first_result = [entry.id for entry in service.autocomplete("gulrot")]
    assert [entry.id for entry in service.autocomplete("gulrot")] == first_result
    assert len(first_result) == len(entries)
    assert len(service.autocomplete("gulrot", limit=2)) == 2
    with pytest.raises(ValueError, match="limit"):
        service.autocomplete("gulrot", limit=0)


def test_food_ids_are_stable_per_exact_upstream_identifier():
    assert food_catalogue_id("06.036") == food_catalogue_id("06.036")
    assert food_catalogue_id("06.036") != food_catalogue_id("06.743")


@pytest.mark.skipif(
    not os.environ.get("KITCHEN_TEST_DATABASE_URL"),
    reason="requires the explicitly configured disposable PostgreSQL test database",
)
def test_migration_seeds_shared_catalogue_without_household_or_purchase_data(test_database_url):
    engine = create_engine(test_database_url)
    try:
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id, source, source_item_id, source_name, display_name, locale, "
                        "food_group_id, category, "
                        "default_unit, search_keywords, source_version, source_url, source_attribution "
                        "FROM kitchen_food_catalogue ORDER BY source_item_id"
                    )
                )
                .mappings()
                .all()
            )
            assert len(rows) == 37
            assert {row["source"] for row in rows} == {"matvaretabellen"}
            assert {row["locale"] for row in rows} == {"nb"}
            assert {row["source_version"] for row in rows} == {"2026"}
            assert {row["source_url"] for row in rows} == {MATVARETABELLEN_URL}
            assert {row["source_attribution"] for row in rows} == {MATVARETABELLEN_ATTRIBUTION}
            assert all(row["default_unit"] is None for row in rows)
            assert all(row["id"] == food_catalogue_id(row["source_item_id"]) for row in rows)
            source_to_display = {
                "02.001": ("Egg, rå", "Egg"),
                "01.291": ("Melk, uspesifisert", "Melk"),
                "01.272": ("Helmelk, 3,5 % fett, laktosefri", "Laktosefri helmelk"),
                "01.011": ("Yoghurt, naturell", "Naturell yoghurt"),
                "01.275": ("Yoghurt, naturell, laktosefri", "Laktosefri naturell yoghurt"),
                "05.010": ("Hvetemel, siktet", "Siktet hvetemel"),
                "05.021": (
                    "Ris, upolert, naturris, langkornet, tørr",
                    "Naturris, langkornet",
                ),
                "05.332": ("Pasta, fullkorn, tørr", "Fullkornspasta"),
                "06.070": ("Tomat, hermetisk", "Hermetiske tomater"),
                "06.740": ("Brokkoli, importert, fryst", "Frossen brokkoli"),
                "06.747": ("Brokkoli, importert, rå", "Brokkoli"),
                "06.133": (
                    "Kidneybønner, røde, hermetisk",
                    "Røde kidneybønner på boks",
                ),
                "03.205": ("Kylling, filet, uten skinn, rå", "Kyllingfilet"),
            }
            assert {
                row["source_item_id"]: (row["source_name"], row["display_name"])
                for row in rows
                if row["source_item_id"] in source_to_display
            } == source_to_display
            categories = {row["source_item_id"]: row["category"] for row in rows}
            assert categories["06.036"] == "Produce"
            assert categories["06.743"] == "Frozen"
            assert categories["06.070"] == "Pantry"
            assert connection.execute(text("SELECT count(*) FROM recipes")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM meals")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM items")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM regulars")).scalar_one() == 0
            assert (
                connection.execute(text("SELECT count(*) FROM kitchen_inventory")).scalar_one() == 0
            )
            assert (
                connection.execute(text("SELECT count(*) FROM kitchen_store_trip")).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM kitchen_basket_selections")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(text("SELECT count(*) FROM kitchen_history")).scalar_one() == 0
            )

        duplicate_id = str(uuid4())
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO kitchen_food_catalogue "
                        "(id,source,source_item_id,source_name,display_name,locale,food_group_id,category,"
                        "search_keywords,source_version,source_url,source_attribution) "
                        "VALUES (:id,'matvaretabellen','02.001','Duplicate','Duplicate','nb','2','Dairy',"
                        "'[]'::jsonb,'2026',:url,:attribution)"
                    ),
                    {
                        "id": duplicate_id,
                        "url": MATVARETABELLEN_URL,
                        "attribution": MATVARETABELLEN_ATTRIBUTION,
                    },
                )
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("KITCHEN_TEST_DATABASE_URL"),
    reason="requires the explicitly configured disposable PostgreSQL test database",
)
def test_postgres_repository_autocomplete_uses_display_name_and_keeps_variants(test_database_url):
    engine = create_engine(test_database_url)
    try:
        service = FoodCatalogueService(PostgresFoodCatalogueRepository(engine))

        matches = service.autocomplete("laktosefri")

        assert {entry.display_name for entry in matches} == {
            "Laktosefri helmelk",
            "Laktosefri naturell yoghurt",
        }
        assert len({entry.id for entry in matches}) == 2
    finally:
        engine.dispose()
