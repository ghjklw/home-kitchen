"""Authenticated request context and bounded bought-before household context."""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from home_kitchen.purchase_frequency import (
    DEFAULT_HALF_LIFE_DAYS,
    PROVIDER_RETENTION_MONTHS,
    ProviderObservationWindow,
    compute_recency_weighted_frequency,
    normalize_provider_key,
)

HistoryChannel = Literal["app", "api", "mcp"]


@dataclass(frozen=True)
class HistoryContext:
    actor_id: str | None = None
    channel: HistoryChannel = "api"


_DEFAULT_CONTEXT = HistoryContext()
history_context: ContextVar[HistoryContext] = ContextVar(
    "history_context", default=_DEFAULT_CONTEXT
)

BOUGHT_BEFORE_BOUNDARY_NOTE = (
    "Synced purchase history shows items bought before only (recency-weighted lower bounds over "
    "each provider's observed window); never assert current stock, that a recipe was cooked, "
    "or an explicit preference."
)


class BoughtBeforeItem(BaseModel):
    """Summary of an item previously purchased in verified provider order/receipt history."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    product_name: str
    canonical_product_id: str | None = None
    store_name: str | None = None
    last_purchased_at: datetime | None = None
    purchase_count: int = Field(default=1, ge=1)
    recency_score: float | None = None
    weighted_purchases_per_30_days: float | None = None
    average_interval_days: float | None = None
    provider_coverage_days: int | None = None
    is_lower_bound: bool = True
    last_quantity: str | None = None
    unit: str | None = None
    provenance: Literal["provider_verified", "user_confirmed"] = "provider_verified"
    evidence_refs: list[str] = Field(default_factory=list)


class PurchaseHistoryContext(BaseModel):
    """Provider-neutral 'bought before' summary for meal planning and AI recipe adaptation."""

    model_config = ConfigDict(extra="forbid")

    bought_before: list[BoughtBeforeItem] = Field(default_factory=list)
    source_providers: list[str] = Field(default_factory=list)
    boundary_note: str = BOUGHT_BEFORE_BOUNDARY_NOTE

    def to_prompt_payload(self) -> dict[str, Any]:
        """Serialize compact bought-before context with explicit boundary metadata."""
        return {
            "boundary_note": self.boundary_note,
            "source_providers": self.source_providers,
            "bought_before": [
                item.model_dump(mode="json", exclude_none=True) for item in self.bought_before
            ],
        }


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _normalize_item_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _is_positive_decimal(value: Any) -> bool:
    if value is None:
        return True
    try:
        return Decimal(str(value)) > 0
    except Exception:
        return True


def build_purchase_history_context(
    effective_orders: Sequence[Any],
    *,
    limit: int = 25,
    as_of: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> PurchaseHistoryContext:
    """Build bounded 'bought before' context from active resolved provider orders/receipts."""
    if limit <= 0:
        return PurchaseHistoryContext()

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    item_events: dict[tuple[str, str], list[SimpleNamespace]] = {}
    provider_windows: dict[str, ProviderObservationWindow] = {}
    providers_seen: list[str] = []

    for order in effective_orders:
        prov_raw = str(getattr(order, "provider", "") or "").strip().lower()
        tx_at = _to_utc(
            getattr(order, "transaction_at", None) or getattr(order, "observed_at", None)
        )
        if prov_raw and tx_at is not None:
            norm_prov = normalize_provider_key(prov_raw)
            is_stub = getattr(order, "lifecycle", None) == "non_receipt_stub"
            existing_win = provider_windows.get(norm_prov)
            if existing_win is None:
                provider_windows[norm_prov] = ProviderObservationWindow(
                    provider=norm_prov,
                    start=tx_at,
                    end=tx_at,
                    total_records=1,
                    retained_months=PROVIDER_RETENTION_MONTHS.get(norm_prov),
                    non_receipt_stub_count=1 if is_stub else 0,
                    incomplete_history=True,
                )
            else:
                provider_windows[norm_prov] = existing_win.model_copy(
                    update={
                        "start": min(existing_win.start, tx_at) if existing_win.start else tx_at,
                        "end": max(existing_win.end, tx_at) if existing_win.end else tx_at,
                        "total_records": existing_win.total_records + 1,
                        "non_receipt_stub_count": existing_win.non_receipt_stub_count
                        + (1 if is_stub else 0),
                    }
                )

        if not getattr(order, "is_active_purchase", False):
            continue
        if getattr(order, "is_return", False) or getattr(order, "is_cancellation", False):
            continue
        provenance = getattr(order, "provenance", "uncertain")
        if provenance not in ("provider_verified", "user_confirmed"):
            continue

        provider = prov_raw
        if not provider:
            continue
        external_id = str(getattr(order, "external_id", "") or "").strip()
        store_name = getattr(order, "store_name", None)
        purchased_at = tx_at

        lines = getattr(order, "lines", None) or []
        order_has_active_line = False
        for line in lines:
            if isinstance(line, dict):
                is_return = bool(line.get("is_return"))
                name = str(line.get("name") or "").strip()
                qty = line.get("quantity")
                amt = line.get("amount")
                unit = line.get("unit")
                canon_pid = line.get("canonical_product_id")
            else:
                is_return = bool(getattr(line, "is_return", False))
                name = str(getattr(line, "name", "") or "").strip()
                qty = getattr(line, "quantity", None)
                amt = getattr(line, "amount", None)
                unit = getattr(line, "unit", None)
                canon_pid = getattr(line, "canonical_product_id", None)

            if is_return or not name:
                continue
            if qty is not None and not _is_positive_decimal(qty):
                continue
            if amt is not None:
                try:
                    if Decimal(str(amt)) < 0:
                        continue
                except Exception:
                    pass

            order_has_active_line = True
            canon_str = str(canon_pid).strip() if canon_pid else None
            dedupe_id = canon_str or _normalize_item_key(name)
            key = (provider, dedupe_id)
            qty_str = str(qty) if qty is not None else None
            unit_str = str(unit).strip() if isinstance(unit, str) and unit.strip() else None

            if purchased_at is not None:
                item_events.setdefault(key, []).append(
                    SimpleNamespace(
                        purchased_at=purchased_at,
                        provenance=provenance,
                        provider=provider,
                    )
                )

            existing = grouped.get(key)
            if existing is None:
                grouped[key] = {
                    "provider": provider,
                    "product_name": name,
                    "canonical_product_id": canon_str,
                    "store_name": store_name,
                    "last_purchased_at": purchased_at,
                    "purchase_count": 1,
                    "last_quantity": qty_str,
                    "unit": unit_str,
                    "provenance": provenance,
                    "evidence_refs": [external_id] if external_id else [],
                }
            else:
                if external_id and external_id not in existing["evidence_refs"]:
                    existing["evidence_refs"].append(external_id)
                existing["purchase_count"] += 1
                prev_at = existing["last_purchased_at"]
                if purchased_at is not None and (prev_at is None or purchased_at > prev_at):
                    existing["last_purchased_at"] = purchased_at
                    existing["product_name"] = name
                    if qty_str is not None:
                        existing["last_quantity"] = qty_str
                    if unit_str is not None:
                        existing["unit"] = unit_str
                    if store_name:
                        existing["store_name"] = store_name
                if canon_str and not existing["canonical_product_id"]:
                    existing["canonical_product_id"] = canon_str
                if provenance == "provider_verified":
                    existing["provenance"] = "provider_verified"

        if order_has_active_line and provider not in providers_seen:
            providers_seen.append(provider)

    items: list[BoughtBeforeItem] = []
    for key, entry in grouped.items():
        events = item_events.get(key, [])
        prov = normalize_provider_key(entry["provider"])
        win = provider_windows.get(prov)
        scoped_windows = {prov: win} if win is not None else None
        if events:
            analysis = compute_recency_weighted_frequency(
                events,
                provider_windows=scoped_windows,
                as_of=as_of,
                half_life_days=half_life_days,
            )
            entry["recency_score"] = analysis.recency_score
            entry["weighted_purchases_per_30_days"] = analysis.weighted_purchases_per_30_days
            entry["average_interval_days"] = analysis.average_interval_days
            entry["provider_coverage_days"] = analysis.coverage_days
            entry["is_lower_bound"] = analysis.is_lower_bound
        items.append(BoughtBeforeItem.model_validate(entry))

    items.sort(
        key=lambda item: (
            -(item.recency_score or 0.0),
            -(item.last_purchased_at.timestamp()) if item.last_purchased_at else 0.0,
            -item.purchase_count,
            item.product_name.casefold(),
            item.provider,
        )
    )
    return PurchaseHistoryContext(
        bought_before=items[:limit],
        source_providers=providers_seen,
    )
