"""Application use cases for reviewed shopping basket product selections."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from home_kitchen.history import HistoryChannel

from .models import BasketReview, ProductSnapshot, SelectedProduct

if TYPE_CHECKING:
    from .matching import BasketCandidate, NeedMatchResult

MAX_BATCH_SELECTIONS = 100


class BatchSelectionValidationError(ValueError):
    """Input validation failure on a batch selection request."""


class BatchSelectionInput(BaseModel):
    """Validated candidate selection item for batch basket persistence."""

    model_config = ConfigDict(extra="forbid")

    need_id: UUID
    candidate_id: str = Field(min_length=1, max_length=200)
    packages: int = Field(gt=0, le=99)
    confirm_identity: bool = False


class BasketCandidateResolver(Protocol):
    async def resolve_batch_candidates(
        self,
        actor: str,
        job_id: str,
        start: date,
        end: date,
        references: list[tuple[UUID, str]],
        identity_confirmations: set[tuple[UUID, str]] | frozenset[tuple[UUID, str]] = ...,
    ) -> dict[tuple[UUID, str], tuple[NeedMatchResult, BasketCandidate, datetime]]: ...


class BasketBatchSelector(Protocol):
    def select_batch(
        self,
        start: date,
        end: date,
        selections: list[SelectedProduct],
        evidence: dict[UUID, NeedMatchResult],
        *,
        channel: HistoryChannel = ...,
    ) -> BasketReview: ...


async def select_basket_products_batch(
    basket_service: BasketBatchSelector,
    basket_jobs: BasketCandidateResolver,
    *,
    start: date,
    end: date,
    job_id: str,
    selections: Sequence[BatchSelectionInput],
    actor_id: str,
    channel: HistoryChannel = "api",
) -> BasketReview:
    """Resolve matched job candidates and atomically persist a reviewed basket batch."""
    if not actor_id or not actor_id.strip():
        raise ValueError("An authenticated actor is required")
    if end < start:
        raise BatchSelectionValidationError("end date must be on or after start date")
    if not job_id or not job_id.strip():
        raise BatchSelectionValidationError("job_id is required")
    if not selections:
        raise BatchSelectionValidationError("Select between 1 and 100 products to save")
    if len(selections) > MAX_BATCH_SELECTIONS:
        raise BatchSelectionValidationError("Select no more than 100 products to save")

    references = [(item.need_id, item.candidate_id) for item in selections]
    if len({need_id for need_id, _ in references}) != len(references):
        raise BatchSelectionValidationError("Select at most one product for each purchase need")
    if len(set(references)) != len(references):
        raise BatchSelectionValidationError("Do not select a product more than once for a need")

    identity_confirmations = {
        (item.need_id, item.candidate_id) for item in selections if item.confirm_identity
    }
    resolve = basket_jobs.resolve_batch_candidates
    if identity_confirmations:
        resolved = await resolve(
            actor_id,
            job_id,
            start,
            end,
            references,
            identity_confirmations=identity_confirmations,
        )
    else:
        resolved = await resolve(actor_id, job_id, start, end, references)

    selected_products: list[SelectedProduct] = []
    evidence: dict[UUID, NeedMatchResult] = {}
    for item in selections:
        matched_need, candidate, observed_at = resolved[(item.need_id, item.candidate_id)]
        if (
            candidate.pack_quantity is None
            or candidate.pack_unit is None
            or candidate.pack_metadata_status != "unverified"
        ):
            raise ValueError("A selected product has unknown package details.")
        price: Decimal | None = None
        if candidate.price is not None:
            try:
                price = Decimal(candidate.price)
            except InvalidOperation:
                price = None
        selected_products.append(
            SelectedProduct(
                need_id=item.need_id,
                product=ProductSnapshot(
                    provider=candidate.provider,
                    product_id=candidate.product_id,
                    name=candidate.name,
                    brand=candidate.brand,
                    url=candidate.product_url,
                    image_url=candidate.media.image_url if candidate.media else None,
                    pack_quantity=candidate.pack_quantity,
                    pack_unit=candidate.pack_unit,
                    price=price,
                    currency="NOK",
                    available=candidate.available,
                    observed_at=observed_at,
                ),
                packages=item.packages,
            )
        )
        evidence[item.need_id] = matched_need

    return await asyncio.to_thread(
        basket_service.select_batch,
        start,
        end,
        selected_products,
        evidence,
        channel=channel,
    )
