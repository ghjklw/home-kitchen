from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from home_kitchen.basket import (
    ProductSnapshot,
    PurchaseNeed,
    SelectedProduct,
    evaluate_basket,
)

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def need(quantity="16", unit="item", id=None):
    return PurchaseNeed(id=id or uuid4(), name="Yoghurt", quantity=quantity, unit=unit)


def product(product_id, name, *, image="https://images.example.test/yoghurt.jpg", **changes):
    fields = {
        "product_id": product_id,
        "name": name,
        "brand": name.split()[0],
        "url": f"https://oda.com/no/products/{product_id}",
        "image_url": image,
        "pack_quantity": 1,
        "pack_unit": "item",
        "price": "20.50",
        "currency": "nok",
        "available": True,
        "observed_at": NOW,
        "evidence_status": "provider_verified",
    }
    fields.update(changes)
    return ProductSnapshot(**fields)


def test_split_exact_need_across_exact_products_and_keep_alternatives():
    target = need()
    tine = product("tine-plain-100", "Tine yoghurt")
    yoplait = product("yoplait-plain-100", "Yoplait yoghurt")
    alternative = product("q-plain-100", "Q yoghurt", image="https://images.example.test/q.jpg")
    review = evaluate_basket(
        [target],
        [
            SelectedProduct(
                need_id=target.id, product=tine, packages=8, alternatives=[alternative]
            ),
            SelectedProduct(need_id=target.id, product=yoplait, packages=8),
        ],
        now=NOW,
    )

    assert review.ready_for_review is True
    assert review.total_cost == Decimal("328.00")
    assert review.currency == "NOK"
    status = review.needs[0]
    assert status.allocated_quantity == Decimal("16")
    assert status.remaining_quantity == status.overbuy_quantity == 0
    assert status.selections[0].alternatives[0].product_id == "q-plain-100"
    assert (
        str(status.selections[0].alternatives[0].image_url) == "https://images.example.test/q.jpg"
    )


def test_reports_shortage_overbuy_and_unverified_or_unavailable_products():
    target = need("10")
    review = evaluate_basket(
        [target],
        [
            SelectedProduct(
                need_id=target.id,
                product=product("single-pack", "Yoghurt", pack_quantity=3, available=False),
                packages=4,
            )
        ],
        now=NOW,
    )
    status = review.needs[0]
    assert status.allocated_quantity == Decimal("12")
    assert status.remaining_quantity == 0
    assert status.overbuy_quantity == Decimal("2")
    assert status.ready is False
    assert review.ready_for_review is False
    assert any("overbuy" in issue for issue in review.issues)
    assert any("availability" in issue for issue in review.issues)

    short = evaluate_basket(
        [target],
        [SelectedProduct(need_id=target.id, product=product("pack", "Yoghurt"), packages=2)],
        now=NOW,
    )
    assert short.needs[0].remaining_quantity == Decimal("8")
    assert short.ready_for_review is False


def test_unverified_and_stale_snapshots_cannot_be_review_ready():
    target = need("1")
    unverified = product("u", "Yoghurt", evidence_status="unverified")
    stale = product("s", "Yoghurt", observed_at=NOW - timedelta(hours=25))
    for snapshot in (unverified, stale):
        result = evaluate_basket(
            [target], [SelectedProduct(need_id=target.id, product=snapshot, packages=1)], now=NOW
        )
        assert result.ready_for_review is False
        assert result.needs[0].ready is False


def test_unknown_need_duplicate_needs_and_incompatible_units_are_rejected():
    target = need()
    with pytest.raises(ValueError, match="unknown need"):
        evaluate_basket(
            [target],
            [SelectedProduct(need_id=uuid4(), product=product("a", "Yoghurt"), packages=1)],
            now=NOW,
        )
    with pytest.raises(ValueError, match="duplicate need"):
        evaluate_basket([target, target], [], now=NOW)
    with pytest.raises(ValueError, match="incompatible units"):
        evaluate_basket(
            [need(id=target.id, unit="kg")],
            [SelectedProduct(need_id=target.id, product=product("a", "Yoghurt"), packages=1)],
            now=NOW,
        )


def test_explicit_mass_volume_packages_convert_only_for_basket_arithmetic():
    flour = need("1000", unit="g")
    kilo = product("flour-kilo", "Flour", pack_quantity="0.5", pack_unit="kg")
    flour_review = evaluate_basket(
        [flour], [SelectedProduct(need_id=flour.id, product=kilo, packages=2)], now=NOW
    )
    flour_status = flour_review.needs[0]
    assert flour_status.need.unit == "g"
    assert flour_status.need.quantity == Decimal("1000")
    assert flour_status.allocated_quantity == Decimal("1000")
    assert flour_status.remaining_quantity == Decimal("0")
    assert flour_status.selections[0].product.pack_quantity == Decimal("0.5")
    assert flour_status.selections[0].product.pack_unit == "kg"

    milk = need("1", unit="l")
    bottle = product("milk-half-litre", "Milk", pack_quantity="500", pack_unit="ml")
    milk_review = evaluate_basket(
        [milk], [SelectedProduct(need_id=milk.id, product=bottle, packages=2)], now=NOW
    )
    milk_status = milk_review.needs[0]
    assert milk_status.need.unit == "ml"
    assert milk_status.allocated_quantity == Decimal("1000")
    assert milk_status.remaining_quantity == Decimal("0")
    assert milk_status.selections[0].product.pack_quantity == Decimal("500")

    incompatible = need("1", unit="g")
    with pytest.raises(ValueError, match="incompatible units"):
        evaluate_basket(
            [incompatible],
            [SelectedProduct(need_id=incompatible.id, product=bottle, packages=2)],
            now=NOW,
        )


def test_product_snapshots_enforce_links_and_timezone():
    with pytest.raises(ValidationError, match="oda.com"):
        product("bad", "Imposter", url="https://fake-oda.com/product/1")
    with pytest.raises(ValidationError, match="timezone-aware"):
        product("bad-time", "Bad time", observed_at=datetime(2026, 11, 15))
    with pytest.raises(ValidationError, match="image URL"):
        product("bad-image", "Bad image", image="http://images.example.test/item.jpg")
