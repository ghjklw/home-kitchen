"""Deterministic arithmetic and readiness checks for a proposed basket."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from home_kitchen.units import add_exact, canonicalize_quantity, multiply_integer_exact

from .models import BasketNeedStatus, BasketReview, PurchaseNeed, SelectedProduct


def evaluate_basket(
    needs: list[PurchaseNeed],
    selections: list[SelectedProduct],
    now: datetime | None = None,
    freshness_hours: int = 24,
) -> BasketReview:
    """Evaluate package allocation and evidence without placing an order."""
    if freshness_hours < 0:
        raise ValueError("freshness_hours cannot be negative")
    now = now or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    normalized_needs = []
    for need in needs:
        quantity, unit = canonicalize_quantity(need.quantity, need.unit)
        normalized_needs.append(need.model_copy(update={"quantity": quantity, "unit": unit}))

    by_id: dict[object, PurchaseNeed] = {}
    for need in normalized_needs:
        if need.id in by_id:
            raise ValueError(f"duplicate need id: {need.id}")
        by_id[need.id] = need

    grouped: dict[object, list[SelectedProduct]] = {need_id: [] for need_id in by_id}
    for selection in selections:
        if selection.need_id not in grouped:
            raise ValueError(f"selection references unknown need: {selection.need_id}")
        need = by_id[selection.need_id]
        _, pack_unit = canonicalize_quantity(
            selection.product.pack_quantity, selection.product.pack_unit
        )
        if pack_unit != need.unit:
            raise ValueError(
                f"incompatible units for {selection.product.name}: "
                f"{selection.product.pack_unit} cannot satisfy {need.unit}"
            )
        grouped[selection.need_id].append(selection)

    issues: list[str] = []
    statuses: list[BasketNeedStatus] = []
    total_cost = Decimal(0)
    currencies: set[str] = set()
    all_prices_known = True
    all_products_ready = True
    freshness = timedelta(hours=freshness_hours)

    for need in normalized_needs:
        selected = grouped[need.id]
        allocated = Decimal(0)
        for entry in selected:
            pack_quantity, _ = canonicalize_quantity(
                entry.product.pack_quantity, entry.product.pack_unit
            )
            allocated = add_exact(allocated, multiply_integer_exact(pack_quantity, entry.packages))
        remaining = max(add_exact(need.quantity, -allocated), Decimal(0))
        overbuy = max(add_exact(allocated, -need.quantity), Decimal(0))
        need_ready = remaining == 0
        for entry in selected:
            product = entry.product
            fresh = timedelta(0) <= now - product.observed_at <= freshness
            verified = product.evidence_status == "provider_verified"
            available = product.available is True
            if product.price is None:
                all_prices_known = False
            else:
                total_cost = add_exact(
                    total_cost, multiply_integer_exact(product.price, entry.packages)
                )
            currencies.add(product.currency)
            if not verified:
                issues.append(f"{product.name}: product evidence is unverified")
            if not fresh:
                issues.append(f"{product.name}: product information is stale or future-dated")
            if not available:
                issues.append(f"{product.name}: availability is not confirmed")
            need_ready = need_ready and verified and fresh and available

        if remaining > 0:
            issues.append(f"{need.name}: {remaining} {need.unit} still needed")
        if overbuy > 0:
            issues.append(f"{need.name}: overbuy of {overbuy} {need.unit}")
        all_products_ready = all_products_ready and need_ready
        statuses.append(
            BasketNeedStatus(
                need=need,
                selections=selected,
                allocated_quantity=allocated,
                remaining_quantity=remaining,
                overbuy_quantity=overbuy,
                ready=need_ready,
            )
        )

    price_complete = bool(selections) and all_prices_known and len(currencies) == 1
    if selections and len(currencies) != 1:
        raise ValueError("Cannot total selected products in different currencies")
    if selections and not all_prices_known:
        issues.append("one or more selected product prices are unknown")
    currency = next(iter(currencies)) if len(currencies) == 1 else "NOK"
    ready_for_review = bool(needs) and all_products_ready and price_complete
    return BasketReview(
        needs=statuses,
        total_cost=total_cost,
        price_complete=price_complete,
        currency=currency,
        ready_for_review=ready_for_review,
        issues=issues,
    )
