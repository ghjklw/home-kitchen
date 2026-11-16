from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from home_kitchen.models import ShoppingSourceQuantity
from home_kitchen.purchasing_reconciliation import (
    PurchasedLineCreate,
    SourceClosureCreate,
    TripCompletionCreate,
    TripCompletionService,
    TripReviewLine,
)


def trip_request(*, purchases=None, closures=None):
    today = date.today()
    return TripCompletionCreate(
        start=today,
        end=today,
        review_token="reviewed-token",
        reviewed=True,
        idempotency_key=uuid4(),
        purchased_on=today,
        purchases=purchases or [],
        source_closures=closures or [],
    )


def test_trip_can_record_unknown_purchase_and_separate_non_purchase_resolution():
    need_id, capture_id = uuid4(), uuid4()
    purchased = PurchasedLineCreate(need_id=need_id)
    closure = SourceClosureCreate(
        need_id=need_id,
        source_kind="capture",
        source_id=capture_id,
        resolution="already_have",
    )

    result = trip_request(purchases=[purchased], closures=[closure])

    assert result.purchases[0].quantity is None
    assert result.purchases[0].unit is None
    assert result.source_closures[0].resolution == "already_have"


def test_trip_rejects_unpaired_actual_quantity_and_duplicate_source_resolution():
    need_id, capture_id = uuid4(), uuid4()
    with pytest.raises(ValidationError, match="supplied together"):
        PurchasedLineCreate(need_id=need_id, quantity=1)

    with pytest.raises(ValidationError, match="resolved only once"):
        trip_request(
            purchases=[PurchasedLineCreate(need_id=need_id, close_capture_ids=[capture_id])],
            closures=[
                SourceClosureCreate(
                    need_id=need_id,
                    source_kind="capture",
                    source_id=capture_id,
                    resolution="not_needed",
                )
            ],
        )


def test_actual_purchase_must_cover_only_the_selected_due_source_quantity():
    meal_id, capture_id = uuid4(), uuid4()
    need = TripReviewLine(
        id=uuid4(),
        name="Milk",
        unit="ml",
        required_quantity=Decimal(6),
        available_quantity=Decimal(3),
        covered_quantity=Decimal(3),
        to_buy_quantity=Decimal(3),
        origins=[f"meal:{meal_id}", f"item:{capture_id}"],
        source_quantities=[
            ShoppingSourceQuantity(kind="meal", source_id=meal_id, quantity=4, unit="ml"),
            ShoppingSourceQuantity(kind="capture", source_id=capture_id, quantity=2, unit="ml"),
        ],
    )

    TripCompletionService._validate_purchase_sources(
        PurchasedLineCreate(need_id=need.id, quantity=3, unit="ml", close_meal_ids=[meal_id]),
        need,
        actual_quantity=Decimal(3),
        actual_unit="ml",
    )
    with pytest.raises(ValueError, match="fully cover"):
        TripCompletionService._validate_purchase_sources(
            PurchasedLineCreate(
                need_id=need.id, quantity=1, unit="ml", close_capture_ids=[capture_id]
            ),
            need,
            actual_quantity=Decimal(1),
            actual_unit="ml",
        )
