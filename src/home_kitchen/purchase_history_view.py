"""Read model and aggregation service for the multi-view Past Purchases UI."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from home_kitchen.catalogue.overview_models import (
    ProvenanceCounts,
    PurchaseEvidenceRecord,
    PurchaseFrequencyOverview,
)
from home_kitchen.catalogue.seeds import V1_PRODUCTS, get_reference_category
from home_kitchen.product_mapping import source_line_metadata
from home_kitchen.purchase_frequency import (
    DEFAULT_HALF_LIFE_DAYS,
    PROVIDER_RETENTION_MONTHS,
    ProviderObservationWindow,
    compute_recency_weighted_frequency,
    normalize_provider_key,
)

PROVIDER_LABELS: dict[str, str] = {
    "oda": "Oda",
    "coop": "Coop",
    "trumf": "Trumf",
    "rema1000": "REMA 1000",
    "kitchen_history": "Manuell handletur",
    "store": "Manuell handletur",
}

CATEGORY_LABELS_NB: dict[str, str] = {
    "Produce": "Frukt og grønt",
    "Bakery": "Bakeri",
    "Meat": "Kjøtt",
    "Seafood": "Sjømat",
    "Fish": "Fisk",
    "Dairy": "Meieri",
    "Chilled": "Kjølevarer",
    "Pantry": "Tørrvarer",
    "Spices": "Krydder",
    "Drinks": "Drikke",
    "Household": "Husholdning",
    "Frozen": "Frysevarer",
    "Other": "Annet",
}

_SEED_CATEGORY_BY_PID: dict[str, str] = {str(prod.id): prod.category for prod in V1_PRODUCTS}
_SEED_NAME_BY_PID: dict[str, str] = {str(prod.id): prod.canonical_name for prod in V1_PRODUCTS}

_EXTRA_CATEGORY_ALIASES: dict[str, str] = {
    "vegetables": "Produce",
    "fruit": "Produce",
    "fruits": "Produce",
    "grønnsaker": "Produce",
    "frukt": "Produce",
    "bakeri og konditori": "Bakery",
    "brød": "Bakery",
    "baking": "Pantry",
    "bakevarer": "Pantry",
    "beverages": "Drinks",
    "drikkevarer": "Drinks",
    "meieri og egg": "Dairy",
    "dairy & eggs": "Dairy",
    "kjøtt og fjærkre": "Meat",
    "fisk og sjømat": "Fish",
    "tørrvarer og hermetikk": "Pantry",
    "husholdning og rengjøring": "Household",
}

BOUNDARY_NOTE_NB = (
    "Vist kjøpshistorikk og frekvens er en nedre grense basert på tilgjengelige kvitteringer "
    "og registrerte handleturer (vektet mot nyere kjøp). Kjøpshistorikk bekrefter ikke "
    "nåværende lagerbeholdning hjemme."
)


def format_provider_label(provider: str) -> str:
    norm = normalize_provider_key(provider)
    return PROVIDER_LABELS.get(
        norm, provider.strip().capitalize() if provider.strip() else "Butikk"
    )


def category_label_nb(category_key: str | None) -> str:
    cleaned = str(category_key or "").strip()
    if not cleaned:
        return CATEGORY_LABELS_NB["Other"]
    if cleaned in CATEGORY_LABELS_NB:
        return CATEGORY_LABELS_NB[cleaned]
    ref = get_reference_category(cleaned)
    if ref is not None:
        return ref.display_name_nb
    mapped = _EXTRA_CATEGORY_ALIASES.get(cleaned.casefold())
    if mapped is not None:
        return CATEGORY_LABELS_NB[mapped]
    return cleaned


def resolve_purchase_category(
    *,
    raw_category: str | None = None,
    product_name: str = "",
    canonical_product_id: str | None = None,
    canonical_category_map: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve a canonical category key and Norwegian Bokmål display label."""
    if raw_category and raw_category.strip():
        cleaned = raw_category.strip()
        ref = get_reference_category(cleaned)
        if ref is not None:
            return ref.key, ref.display_name_nb
        mapped = _EXTRA_CATEGORY_ALIASES.get(cleaned.casefold())
        if mapped is not None:
            return mapped, CATEGORY_LABELS_NB[mapped]
        if cleaned in CATEGORY_LABELS_NB:
            return cleaned, CATEGORY_LABELS_NB[cleaned]
        return cleaned, cleaned

    if canonical_product_id:
        pid = str(canonical_product_id).strip()
        if canonical_category_map and pid in canonical_category_map:
            key = canonical_category_map[pid]
            return key, category_label_nb(key)
        if pid in _SEED_CATEGORY_BY_PID:
            key = _SEED_CATEGORY_BY_PID[pid]
            return key, category_label_nb(key)

    return "Other", CATEGORY_LABELS_NB["Other"]


def _to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _order_purchase_time(order: Any) -> datetime | None:
    """Use ingestion time as a fallback except for Coop, whose scan revisits history."""
    transaction_at = getattr(order, "transaction_at", None)
    if transaction_at is not None:
        return transaction_at
    if normalize_provider_key(getattr(order, "provider", None)) == "coop":
        return None
    return getattr(order, "observed_at", None)


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        dec = Decimal(str(val))
    except Exception:
        return None
    return dec if dec.is_finite() else None


class PurchaseLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    row_id: str | None = None
    name: str
    raw_name: str | None = None
    retailer_product_id: str | None = None
    canonical_product_id: str | None = None
    canonical_product_name: str | None = None
    base_product_id: str | None = None
    base_product_name: str | None = None
    suggested_product_name: str | None = None
    suggested_category: str | None = None
    product_evidence: str | None = None
    category_evidence: str | None = None
    mapping_id: str | None = None
    mapping_source: str | None = None
    mapping_confidence: float | None = None
    mapping_review_status: Literal["pending", "confirmed", "ignored"] = "pending"
    category_source: str | None = None
    category_confidence: float | None = None
    category_review_status: Literal["pending", "confirmed", "ignored"] = "pending"
    package_size: Decimal | None = None
    package_unit: str | None = None
    package_inference_source: str | None = None
    package_confidence: float | None = None
    package_evidence: str | None = None
    mapping_match: Literal["exact", "similar", "none"] = "none"
    mapping_match_source: (
        Literal["retailer_product_id", "ean", "exact_description", "opted_in_exact_description"]
        | None
    ) = None
    category: str = "Other"
    category_label: str = "Annet"
    quantity: Decimal | None = None
    unit: str | None = None
    amount: Decimal | None = None
    ean: str | None = None
    oda_product_id: str | None = None


class PurchaseBasketSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider: str
    provider_label: str
    store_name: str | None = None
    purchased_at: AwareDatetime | None = None
    total_amount: Decimal | None = None
    currency: str = "NOK"
    item_count: int = 0
    provenance: Literal["provider_verified", "user_confirmed"] = "provider_verified"
    is_correction: bool = False
    revision_count: int = 1
    evidence_ref: str | None = None
    lines: list[PurchaseLineItem] = Field(default_factory=list)


class PurchaseRecentSnippet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchased_at: AwareDatetime | None = None
    provider: str
    provider_label: str
    store_name: str | None = None
    product_name: str
    quantity: Decimal | None = None
    unit: str | None = None
    amount: Decimal | None = None


class PurchaseProductSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    product_name: str
    canonical_product_id: str | None = None
    category: str = "Other"
    category_label: str = "Annet"
    last_purchased_at: AwareDatetime | None = None
    first_purchased_at: AwareDatetime | None = None
    purchase_count: int = 0
    total_quantity: Decimal | None = None
    unit: str | None = None
    total_spent: Decimal | None = None
    providers: list[str] = Field(default_factory=list)
    provider_labels: list[str] = Field(default_factory=list)
    stores: list[str] = Field(default_factory=list)
    frequency: PurchaseFrequencyOverview
    recent_items: list[PurchaseRecentSnippet] = Field(default_factory=list)


class PurchaseTableRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    row_id: str | None = None
    basket_id: str
    purchased_at: AwareDatetime | None = None
    provider: str
    provider_label: str
    store_name: str | None = None
    product_name: str
    raw_name: str | None = None
    retailer_product_id: str | None = None
    canonical_product_id: str | None = None
    canonical_product_name: str | None = None
    base_product_id: str | None = None
    base_product_name: str | None = None
    suggested_product_name: str | None = None
    suggested_category: str | None = None
    product_evidence: str | None = None
    category_evidence: str | None = None
    mapping_id: str | None = None
    mapping_source: str | None = None
    mapping_confidence: float | None = None
    mapping_review_status: Literal["pending", "confirmed", "ignored"] = "pending"
    category_source: str | None = None
    category_confidence: float | None = None
    category_review_status: Literal["pending", "confirmed", "ignored"] = "pending"
    package_size: Decimal | None = None
    package_unit: str | None = None
    package_inference_source: str | None = None
    package_confidence: float | None = None
    package_evidence: str | None = None
    mapping_match: Literal["exact", "similar", "none"] = "none"
    mapping_match_source: (
        Literal["retailer_product_id", "ean", "exact_description", "opted_in_exact_description"]
        | None
    ) = None
    category: str = "Other"
    category_label: str = "Annet"
    quantity: Decimal | None = None
    unit: str | None = None
    amount: Decimal | None = None
    ean: str | None = None
    provenance: Literal["provider_verified", "user_confirmed"] = "provider_verified"
    evidence_ref: str | None = None


class PurchaseCategoryOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str


class PurchaseProviderOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str


class PurchaseHistoryBrowseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baskets: list[PurchaseBasketSummary] = Field(default_factory=list)
    products: list[PurchaseProductSummary] = Field(default_factory=list)
    rows: list[PurchaseTableRow] = Field(default_factory=list)
    providers: list[PurchaseProviderOption] = Field(default_factory=list)
    stores: list[str] = Field(default_factory=list)
    categories: list[PurchaseCategoryOption] = Field(default_factory=list)
    provider_windows: dict[str, ProviderObservationWindow] = Field(default_factory=dict)
    total_baskets: int = 0
    total_products: int = 0
    total_rows: int = 0
    boundary_note: str = BOUNDARY_NOTE_NB


class PurchaseHistoryBrowseService:
    """Aggregates synced receipts and manual purchases for the 3-mode Past Purchases UI."""

    def __init__(
        self,
        *,
        purchase_sync: Any = None,
        history: Any = None,
        product_overview_repo: Any = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    ) -> None:
        self.purchase_sync = purchase_sync
        self.history = history
        self.product_overview_repo = product_overview_repo
        self.half_life_days = half_life_days

    def _canonical_product_metadata(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return (display_names_by_pid, categories_by_pid) if repository is available."""
        names: dict[str, str] = dict(_SEED_NAME_BY_PID)
        cats: dict[str, str] = dict(_SEED_CATEGORY_BY_PID)
        repo = self.product_overview_repo
        if repo is None:
            return names, cats
        try:
            products = repo.list_products()
            if not products:
                return names, cats
            aliases_map = repo.get_aliases_for_products([p.id for p in products])
            for prod in products:
                pid = str(prod.id)
                prod_aliases = aliases_map.get(prod.id, [])
                nb_aliases = [
                    a
                    for a in prod_aliases
                    if a.locale.casefold().strip() == "nb" and a.alias.strip()
                ]
                if nb_aliases:
                    nb_aliases.sort(key=lambda a: (not a.reviewed, a.context != "recipe"))
                    names[pid] = nb_aliases[0].alias
                else:
                    names.setdefault(pid, prod.canonical_name)
                if pid not in cats:
                    stock = repo.get_stock_for_product(prod, prod_aliases)
                    for st in stock.items:
                        if st.category and st.category != "Other":
                            cats[pid] = st.category
                            break
        except Exception:
            pass
        return names, cats

    def _collect_all_baskets(
        self,
    ) -> tuple[list[PurchaseBasketSummary], dict[str, ProviderObservationWindow]]:
        canon_names, canon_cats = self._canonical_product_metadata()
        baskets: list[PurchaseBasketSummary] = []
        provider_windows: dict[str, ProviderObservationWindow] = {}

        def _update_window(
            provider: str,
            tx_at: datetime | None,
            *,
            stub: bool = False,
        ) -> None:
            if tx_at is None:
                return
            prov = normalize_provider_key(provider)
            existing = provider_windows.get(prov)
            if existing is None:
                provider_windows[prov] = ProviderObservationWindow(
                    provider=prov,
                    start=tx_at,
                    end=tx_at,
                    total_records=1,
                    retained_months=PROVIDER_RETENTION_MONTHS.get(prov),
                    non_receipt_stub_count=1 if stub else 0,
                    incomplete_history=True,
                )
            else:
                provider_windows[prov] = existing.model_copy(
                    update={
                        "start": min(existing.start, tx_at) if existing.start else tx_at,
                        "end": max(existing.end, tx_at) if existing.end else tx_at,
                        "total_records": existing.total_records + 1,
                        "non_receipt_stub_count": existing.non_receipt_stub_count
                        + (1 if stub else 0),
                    }
                )

        if self.purchase_sync is not None:
            get_orders = getattr(self.purchase_sync, "get_effective_orders", None)
            effective_orders = get_orders(include_inactive=True) if callable(get_orders) else []
            for order in effective_orders:
                prov = normalize_provider_key(getattr(order, "provider", "unknown"))
                tx_at = _to_utc(_order_purchase_time(order))
                if not getattr(order, "is_cancellation", False) and not getattr(
                    order, "is_return", False
                ):
                    _update_window(
                        prov,
                        tx_at,
                        stub=(getattr(order, "lifecycle", None) == "non_receipt_stub"),
                    )
                if not getattr(order, "is_active_purchase", False):
                    continue
                if getattr(order, "is_return", False) or getattr(order, "is_cancellation", False):
                    continue
                provenance = getattr(order, "provenance", "uncertain")
                if provenance not in ("provider_verified", "user_confirmed"):
                    continue

                ext_id = str(getattr(order, "external_id", "") or "").strip()
                basket_id = f"{prov}:{ext_id}" if ext_id else f"{prov}:{len(baskets) + 1}"
                store_name = getattr(order, "store_name", None) or format_provider_label(prov)
                lines_out: list[PurchaseLineItem] = []
                computed_amount = Decimal("0")
                has_line_amount = False

                for idx, line in enumerate(getattr(order, "lines", None) or []):
                    if getattr(line, "is_return", False):
                        continue
                    name = str(getattr(line, "name", "") or "").strip()
                    if not name:
                        continue
                    raw_name = getattr(line, "raw_name", None)
                    canon_pid = getattr(line, "canonical_product_id", None)
                    canon_pid_str = str(canon_pid).strip() if canon_pid else None
                    canon_display = canon_names.get(canon_pid_str) if canon_pid_str else None
                    qty = _to_decimal(getattr(line, "quantity", None))
                    amt = _to_decimal(getattr(line, "amount", None))
                    if amt is not None:
                        computed_amount += amt
                        has_line_amount = True
                    cat_key, cat_nb = resolve_purchase_category(
                        raw_category=getattr(line, "category", None),
                        product_name=canon_display or name,
                        canonical_product_id=canon_pid_str,
                        canonical_category_map=canon_cats,
                    )
                    source_meta = source_line_metadata(
                        prov,
                        getattr(order, "payload", {})
                        if isinstance(getattr(order, "payload", {}), dict)
                        else {},
                        idx,
                    )
                    line_id = f"{basket_id}:line:{idx}"
                    lines_out.append(
                        PurchaseLineItem(
                            id=line_id,
                            row_id=line_id,
                            name=name,
                            raw_name=raw_name,
                            retailer_product_id=source_meta.get("retailer_product_id"),
                            canonical_product_id=canon_pid_str,
                            canonical_product_name=canon_display,
                            base_product_id=None,
                            base_product_name=None,
                            category=cat_key,
                            category_label=cat_nb,
                            quantity=qty,
                            unit=getattr(line, "unit", None),
                            amount=amt,
                            ean=getattr(line, "ean", None) or source_meta.get("ean"),
                            oda_product_id=getattr(line, "oda_product_id", None),
                        )
                    )

                if not lines_out:
                    continue
                total_amt = _to_decimal(getattr(order, "total_amount", None))
                if total_amt is None and has_line_amount:
                    total_amt = computed_amount

                baskets.append(
                    PurchaseBasketSummary(
                        id=basket_id,
                        provider=prov,
                        provider_label=format_provider_label(prov),
                        store_name=store_name,
                        purchased_at=tx_at,
                        total_amount=total_amt,
                        currency="NOK",
                        item_count=len(lines_out),
                        provenance=provenance,
                        is_correction=bool(getattr(order, "is_correction", False)),
                        revision_count=int(getattr(order, "revision_count", 1) or 1),
                        evidence_ref=getattr(order, "evidence_ref", None),
                        lines=lines_out,
                    )
                )

        if self.history is not None:
            list_purchases = getattr(self.history, "list_purchase_entries", None)
            hist_entries = list_purchases(limit=1000) if callable(list_purchases) else []
            grouped_trips: dict[str, list[Any]] = defaultdict(list)
            for entry in hist_entries:
                structured = entry.structured if isinstance(entry.structured, dict) else {}
                trip_id = str(structured.get("trip_id") or "").strip()
                group_key = trip_id or f"entry:{entry.id}"
                grouped_trips[group_key].append(entry)

            for trip_key, entries in grouped_trips.items():
                entries.sort(key=lambda e: e.occurred_at)
                first = entries[0]
                structured_first = first.structured if isinstance(first.structured, dict) else {}
                purchased_on = structured_first.get("purchased_on")
                purchased_at = _to_utc(first.occurred_at)
                if isinstance(purchased_on, str) and purchased_on.strip():
                    try:
                        d = date.fromisoformat(purchased_on.strip())
                        purchased_at = datetime(d.year, d.month, d.day, 12, 0, tzinfo=UTC)
                    except ValueError:
                        pass

                lines_out = []
                basket_prov = "store"
                basket_store = "Butikk (manuell)"
                for idx, entry in enumerate(entries):
                    s = entry.structured if isinstance(entry.structured, dict) else {}
                    raw_need = s.get("need")
                    need: dict[str, Any] = raw_need if isinstance(raw_need, dict) else {}
                    name = str(
                        need.get("name")
                        or s.get("product_name")
                        or s.get("item")
                        or entry.message
                        or ""
                    ).strip()
                    if not name:
                        continue
                    raw_snap = s.get("product_snapshot")
                    snap: dict[str, Any] = raw_snap if isinstance(raw_snap, dict) else {}
                    raw_purchase = s.get("purchase")
                    p_prod = raw_purchase.get("product") if isinstance(raw_purchase, dict) else {}
                    p_prod_dict: dict[str, Any] = p_prod if isinstance(p_prod, dict) else {}
                    prov = normalize_provider_key(
                        str(snap.get("provider") or p_prod_dict.get("provider") or "store")
                    )
                    if idx == 0 and prov != "store":
                        basket_prov = prov
                        basket_store = format_provider_label(prov)
                    qty = _to_decimal(s.get("actual_quantity") or s.get("quantity"))
                    unit = s.get("actual_unit") or need.get("unit")
                    amt = _to_decimal(s.get("amount") or s.get("price"))
                    canon_pid = s.get("canonical_product_id")
                    canon_pid_str = str(canon_pid).strip() if canon_pid else None
                    canon_display = canon_names.get(canon_pid_str) if canon_pid_str else None
                    cat_key, cat_nb = resolve_purchase_category(
                        raw_category=s.get("category") or need.get("category"),
                        product_name=canon_display or name,
                        canonical_product_id=canon_pid_str,
                        canonical_category_map=canon_cats,
                    )
                    lines_out.append(
                        PurchaseLineItem(
                            id=f"trip:{trip_key}:line:{idx}",
                            row_id=f"trip:{trip_key}:line:{idx}",
                            name=name,
                            canonical_product_id=canon_pid_str,
                            canonical_product_name=canon_display,
                            category=cat_key,
                            category_label=cat_nb,
                            quantity=qty,
                            unit=str(unit).strip()
                            if isinstance(unit, str) and unit.strip()
                            else None,
                            amount=amt,
                        )
                    )

                if not lines_out:
                    continue
                _update_window(basket_prov, purchased_at)
                baskets.append(
                    PurchaseBasketSummary(
                        id=f"trip:{trip_key}",
                        provider=basket_prov,
                        provider_label=format_provider_label(basket_prov),
                        store_name=basket_store,
                        purchased_at=purchased_at,
                        total_amount=None,
                        currency="NOK",
                        item_count=len(lines_out),
                        provenance="user_confirmed",
                        evidence_ref=trip_key,
                        lines=lines_out,
                    )
                )

        baskets.sort(
            key=lambda b: (
                -(b.purchased_at.timestamp()) if b.purchased_at else 0.0,
                b.provider,
                b.id,
            )
        )
        return baskets, provider_windows

    def browse(
        self,
        *,
        q: str | None = None,
        provider: str | None = None,
        store: str | None = None,
        category: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 500,
        as_of: datetime | None = None,
    ) -> PurchaseHistoryBrowseResponse:
        if date_from is not None and date_to is not None and date_to < date_from:
            raise ValueError("date_to must be on or after date_from")

        all_baskets, provider_windows = self._collect_all_baskets()

        # Collect unfiltered facet options so dropdowns stay complete when filtering
        provider_opts_map: dict[str, str] = {}
        stores_set: set[str] = set()
        cat_opts_map: dict[str, str] = {}
        for basket in all_baskets:
            provider_opts_map[basket.provider] = basket.provider_label
            if basket.store_name:
                stores_set.add(basket.store_name)
            for line in basket.lines:
                cat_opts_map[line.category] = line.category_label

        q_norm = " ".join((q or "").casefold().split())
        prov_filter = normalize_provider_key(provider) if provider and provider.strip() else None
        store_filter = store.strip().casefold() if store and store.strip() else None
        cat_filter = category.strip().casefold() if category and category.strip() else None

        filtered_baskets: list[PurchaseBasketSummary] = []
        filtered_rows: list[PurchaseTableRow] = []

        for basket in all_baskets:
            if prov_filter and basket.provider != prov_filter:
                continue
            if store_filter and (basket.store_name or "").casefold() != store_filter:
                continue
            b_date = basket.purchased_at.astimezone(UTC).date() if basket.purchased_at else None
            if date_from is not None and (b_date is None or b_date < date_from):
                continue
            if date_to is not None and (b_date is None or b_date > date_to):
                continue

            matching_lines: list[PurchaseLineItem] = []
            for line in basket.lines:
                if cat_filter and (
                    line.category.casefold() != cat_filter
                    and line.category_label.casefold() != cat_filter
                ):
                    continue
                if q_norm:
                    haystack = " ".join(
                        part
                        for part in (
                            line.name,
                            line.raw_name or "",
                            line.canonical_product_name or "",
                            line.category_label,
                            line.ean or "",
                            basket.store_name or "",
                            basket.provider_label,
                        )
                        if part
                    ).casefold()
                    if q_norm not in haystack:
                        continue
                matching_lines.append(line)

            if not matching_lines:
                continue

            # Keep matching lines in basket view when item-level filters (q/category) are active
            visible_lines = matching_lines if (q_norm or cat_filter) else basket.lines
            filtered_baskets.append(
                basket.model_copy(
                    update={
                        "lines": visible_lines,
                        "item_count": len(visible_lines),
                    }
                )
            )
            for line in matching_lines:
                filtered_rows.append(
                    PurchaseTableRow(
                        id=line.id,
                        row_id=line.row_id or line.id,
                        basket_id=basket.id,
                        purchased_at=basket.purchased_at,
                        provider=basket.provider,
                        provider_label=basket.provider_label,
                        store_name=basket.store_name,
                        product_name=line.canonical_product_name or line.name,
                        canonical_product_name=line.canonical_product_name,
                        raw_name=line.raw_name
                        or (line.name if line.canonical_product_name else None),
                        canonical_product_id=line.canonical_product_id,
                        retailer_product_id=line.retailer_product_id,
                        base_product_id=line.base_product_id,
                        base_product_name=line.base_product_name,
                        suggested_product_name=line.suggested_product_name,
                        suggested_category=line.suggested_category,
                        product_evidence=line.product_evidence,
                        category_evidence=line.category_evidence,
                        mapping_id=line.mapping_id,
                        mapping_source=line.mapping_source,
                        mapping_confidence=line.mapping_confidence,
                        mapping_review_status=line.mapping_review_status,
                        category_source=line.category_source,
                        category_confidence=line.category_confidence,
                        category_review_status=line.category_review_status,
                        package_size=line.package_size,
                        package_unit=line.package_unit,
                        package_inference_source=line.package_inference_source,
                        package_confidence=line.package_confidence,
                        package_evidence=line.package_evidence,
                        mapping_match=line.mapping_match,
                        mapping_match_source=line.mapping_match_source,
                        category=line.category,
                        category_label=line.category_label,
                        quantity=line.quantity,
                        unit=line.unit,
                        amount=line.amount,
                        ean=line.ean,
                        provenance=basket.provenance,
                        evidence_ref=basket.evidence_ref,
                    )
                )

        # Group matching rows into product summaries with recency-weighted frequency
        product_groups: dict[str, list[PurchaseTableRow]] = defaultdict(list)
        for row in filtered_rows:
            group_key = (
                f"pid:{row.canonical_product_id}"
                if row.canonical_product_id
                else f"name:{' '.join(row.product_name.casefold().split())}"
            )
            product_groups[group_key].append(row)

        products: list[PurchaseProductSummary] = []
        for group_key, group_rows in product_groups.items():
            group_rows.sort(
                key=lambda r: r.purchased_at.timestamp() if r.purchased_at else 0.0,
            )
            latest = group_rows[-1]
            earliest = group_rows[0]

            events: list[PurchaseEvidenceRecord] = []
            distinct_baskets: set[str] = set()
            provs: list[str] = []
            stores: list[str] = []
            units_seen: set[str] = set()
            total_qty = Decimal("0")
            qty_all_present = True
            total_spent = Decimal("0")
            has_spent = False

            for r in group_rows:
                distinct_baskets.add(r.basket_id)
                if r.provider not in provs:
                    provs.append(r.provider)
                if r.store_name and r.store_name not in stores:
                    stores.append(r.store_name)
                if r.unit:
                    units_seen.add(r.unit.strip().casefold())
                if r.quantity is not None:
                    total_qty += r.quantity
                else:
                    qty_all_present = False
                if r.amount is not None:
                    total_spent += r.amount
                    has_spent = True
                if r.purchased_at is not None:
                    events.append(
                        PurchaseEvidenceRecord(
                            evidence_id=r.id,
                            purchased_at=r.purchased_at,
                            provenance=r.provenance,
                            evidence_source=r.provider,
                            provider=r.provider,
                            store_name=r.store_name,
                            evidence_ref=r.evidence_ref,
                            quantity=r.quantity,
                            unit=r.unit,
                            product_name=r.product_name,
                        )
                    )

            analysis = compute_recency_weighted_frequency(
                events,
                provider_windows=provider_windows or None,
                as_of=as_of,
                half_life_days=self.half_life_days,
            )
            freq_overview = PurchaseFrequencyOverview(
                status=analysis.status,
                average_interval_days=analysis.average_interval_days,
                recency_weighted_interval_days=analysis.recency_weighted_interval_days,
                weighted_purchases_per_30_days=analysis.weighted_purchases_per_30_days,
                recency_score=analysis.recency_score,
                sample_size=analysis.sample_size,
                first_purchased_at=analysis.first_purchased_at,
                last_purchased_at=analysis.last_purchased_at,
                date_range_days=analysis.date_range_days,
                coverage_days=analysis.coverage_days,
                half_life_days=analysis.half_life_days,
                is_lower_bound=analysis.is_lower_bound,
                confidence=analysis.confidence,
                provider_windows=analysis.provider_windows,
                coverage_note=analysis.coverage_note,
                frequency_display=analysis.frequency_display,
                counts_by_provenance=ProvenanceCounts(
                    provider_verified=analysis.provider_verified_count,
                    user_confirmed=analysis.user_confirmed_count,
                    uncertain=analysis.uncertain_count,
                ),
                evidence_history=[],
            )

            consistent_unit = (
                next(r.unit for r in reversed(group_rows) if r.unit)
                if len(units_seen) == 1
                else None
            )
            recent_snippets = [
                PurchaseRecentSnippet(
                    purchased_at=r.purchased_at,
                    provider=r.provider,
                    provider_label=r.provider_label,
                    store_name=r.store_name,
                    product_name=r.raw_name or r.product_name,
                    quantity=r.quantity,
                    unit=r.unit,
                    amount=r.amount,
                )
                for r in reversed(group_rows[-5:])
            ]

            products.append(
                PurchaseProductSummary(
                    key=group_key,
                    product_name=latest.product_name,
                    canonical_product_id=latest.canonical_product_id,
                    category=latest.category,
                    category_label=latest.category_label,
                    last_purchased_at=latest.purchased_at,
                    first_purchased_at=earliest.purchased_at,
                    purchase_count=len(distinct_baskets),
                    total_quantity=total_qty
                    if (qty_all_present and len(units_seen) <= 1)
                    else None,
                    unit=consistent_unit,
                    total_spent=total_spent if has_spent else None,
                    providers=provs,
                    provider_labels=[format_provider_label(p) for p in provs],
                    stores=stores,
                    frequency=freq_overview,
                    recent_items=recent_snippets,
                )
            )

        products.sort(
            key=lambda p: (
                -(p.frequency.recency_score or 0.0),
                -(p.last_purchased_at.timestamp()) if p.last_purchased_at else 0.0,
                -p.purchase_count,
                p.product_name.casefold(),
            )
        )

        limited_baskets = filtered_baskets[:limit]
        limited_rows = filtered_rows[: limit * 20]

        provider_options = [
            PurchaseProviderOption(key=k, label=provider_opts_map[k])
            for k in sorted(provider_opts_map)
        ]
        category_options = [
            PurchaseCategoryOption(key=k, label=cat_opts_map[k])
            for k in sorted(cat_opts_map, key=lambda key: cat_opts_map[key].casefold())
        ]

        return PurchaseHistoryBrowseResponse(
            baskets=limited_baskets,
            products=products,
            rows=limited_rows,
            providers=provider_options,
            stores=sorted(stores_set, key=lambda s: s.casefold()),
            categories=category_options,
            provider_windows=provider_windows,
            total_baskets=len(filtered_baskets),
            total_products=len(products),
            total_rows=len(filtered_rows),
        )


def filter_orders_by_date(
    orders: Sequence[Any],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Any]:
    """Utility to filter orders by transaction date range."""
    results: list[Any] = []
    for order in orders:
        tx_at = _to_utc(_order_purchase_time(order))
        if tx_at is None:
            continue
        d = tx_at.date()
        if date_from is not None and d < date_from:
            continue
        if date_to is not None and d > date_to:
            continue
        results.append(order)
    return results
