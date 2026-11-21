"""Unit tests for basket domain matching logic."""

from decimal import Decimal

from home_kitchen.basket.domain.matching_logic import (
    _clean_ingredient_query,
    _explicit_pack,
    _oda_description_pack,
    _package_recommendation,
    _query_expansions,
)


def test_clean_ingredient_query():
    assert _clean_ingredient_query("  hvetemel , divided ") == "hvetemel"
    assert _clean_ingredient_query("sukker, to taste") == "sukker"


def test_explicit_pack():
    res = _explicit_pack("1 kg")
    assert res is not None
    assert res[0] == Decimal("1000")
    assert res[1] == "g"

    multipack = _explicit_pack("3 x 500 g")
    assert multipack is not None
    assert multipack[0] == Decimal("1500")
    assert multipack[1] == "g"

    assert _explicit_pack("invalid") is None


def test_oda_description_pack():
    res = _oda_description_pack("Melk 1,5%, 1 l")
    assert res is not None
    assert res[0] == Decimal("1000")
    assert res[1] == "ml"

    assert _oda_description_pack("Some prose without size") is None


def test_package_recommendation():
    rec = _package_recommendation(Decimal("1500"), "g", (Decimal("1000"), "g"))
    assert rec is not None
    assert rec[0] == 2
    assert rec[1] == Decimal("2000")


def test_query_expansions():
    exp = _query_expansions("all-purpose flour")
    assert "hvetemel" in exp
