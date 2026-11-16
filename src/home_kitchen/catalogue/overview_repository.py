"""Persistence repositories for canonical product overview read model and preferences."""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.identity import CanonicalProduct, normalize_identity_text
from home_kitchen.catalogue.overview_models import (
    ExcludedOdaSku,
    PreferredOdaSku,
    ProductAliasItem,
    ProductStockOverview,
    PurchaseEvidenceRecord,
    StockItemDetail,
)
from home_kitchen.shopping import normalize_need_text


class ProductOverviewRepository(Protocol):
    def list_products(self, query: str | None = None) -> list[CanonicalProduct]: ...
    def get_product(self, product_id: UUID) -> CanonicalProduct | None: ...
    def get_aliases_for_products(
        self, product_ids: list[UUID]
    ) -> dict[UUID, list[ProductAliasItem]]: ...
    def get_preferences(
        self, household_id: UUID, product_id: UUID
    ) -> tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]: ...
    def get_all_preferences(
        self, household_id: UUID
    ) -> dict[UUID, tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]]: ...
    def set_preferences(
        self,
        household_id: UUID,
        product_id: UUID,
        preferred: list[PreferredOdaSku],
        excluded: list[ExcludedOdaSku],
        actor_id: str = "",
    ) -> None: ...
    def get_stock_for_product(
        self, product: CanonicalProduct, aliases: list[ProductAliasItem]
    ) -> ProductStockOverview: ...
    def get_purchase_evidence_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PurchaseEvidenceRecord]: ...


class PostgresProductOverviewRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def list_products(self, query: str | None = None) -> list[CanonicalProduct]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, canonical_name, variant FROM kitchen_canonical_products "
                    "ORDER BY canonical_name, variant, id"
                )
            ).mappings()
            return [CanonicalProduct.model_validate(dict(r)) for r in rows]

    def get_product(self, product_id: UUID) -> CanonicalProduct | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, canonical_name, variant FROM kitchen_canonical_products "
                        "WHERE id = :id"
                    ),
                    {"id": product_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            return CanonicalProduct.model_validate(dict(row))

    def get_aliases_for_products(
        self, product_ids: list[UUID]
    ) -> dict[UUID, list[ProductAliasItem]]:
        if not product_ids:
            return {}
        result: dict[UUID, list[ProductAliasItem]] = defaultdict(list)
        with self.engine.connect() as connection:
            # Check if aliases table exists
            rows = connection.execute(
                text(
                    "SELECT product_id, locale, context, alias, reviewed "
                    "FROM kitchen_product_aliases "
                    "WHERE product_id = ANY(:ids) "
                    "ORDER BY locale, reviewed DESC, alias"
                    if self.engine.dialect.name == "postgresql"
                    else "SELECT product_id, locale, context, alias, reviewed "
                    "FROM kitchen_product_aliases "
                    "ORDER BY locale, reviewed DESC, alias"
                ),
                {"ids": product_ids} if self.engine.dialect.name == "postgresql" else {},
            ).mappings()
            for r in rows:
                pid = r["product_id"]
                if pid in product_ids:
                    result[pid].append(
                        ProductAliasItem(
                            alias=r["alias"],
                            locale=r["locale"],
                            context=r["context"],
                            reviewed=bool(r["reviewed"]),
                        )
                    )
        return dict(result)

    def get_preferences(
        self, household_id: UUID, product_id: UUID
    ) -> tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]:
        preferred: list[PreferredOdaSku] = []
        excluded: list[ExcludedOdaSku] = []
        with self.engine.connect() as connection:
            try:
                pref_rows = connection.execute(
                    text(
                        "SELECT oda_product_id, preference_order, payload "
                        "FROM kitchen_canonical_product_oda_preferences "
                        "WHERE household_id = :hid AND product_id = :pid "
                        "ORDER BY preference_order ASC, created_at ASC"
                    ),
                    {"hid": household_id, "pid": product_id},
                ).mappings()
                for r in pref_rows:
                    payload = r["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    payload["oda_product_id"] = r["oda_product_id"]
                    payload["rank"] = r["preference_order"]
                    preferred.append(PreferredOdaSku.model_validate(payload))

                exc_rows = connection.execute(
                    text(
                        "SELECT oda_product_id, reason, payload, created_at "
                        "FROM kitchen_canonical_product_oda_exclusions "
                        "WHERE household_id = :hid AND product_id = :pid "
                        "ORDER BY created_at ASC"
                    ),
                    {"hid": household_id, "pid": product_id},
                ).mappings()
                for r in exc_rows:
                    payload = r["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    payload["oda_product_id"] = r["oda_product_id"]
                    payload["reason"] = r["reason"]
                    payload["excluded_at"] = r["created_at"]
                    excluded.append(ExcludedOdaSku.model_validate(payload))
            except Exception:
                # Table might not exist yet if unmigrated
                pass
        return preferred, excluded

    def get_all_preferences(
        self, household_id: UUID
    ) -> dict[UUID, tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]]:
        result: dict[UUID, tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]] = {}
        with self.engine.connect() as connection:
            try:
                pref_rows = connection.execute(
                    text(
                        "SELECT product_id, oda_product_id, preference_order, payload "
                        "FROM kitchen_canonical_product_oda_preferences "
                        "WHERE household_id = :hid "
                        "ORDER BY product_id, preference_order ASC, created_at ASC"
                    ),
                    {"hid": household_id},
                ).mappings()
                prefs_by_pid: dict[UUID, list[PreferredOdaSku]] = defaultdict(list)
                for r in pref_rows:
                    payload = r["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    payload["oda_product_id"] = r["oda_product_id"]
                    payload["rank"] = r["preference_order"]
                    prefs_by_pid[r["product_id"]].append(PreferredOdaSku.model_validate(payload))

                exc_rows = connection.execute(
                    text(
                        "SELECT product_id, oda_product_id, reason, payload, created_at "
                        "FROM kitchen_canonical_product_oda_exclusions "
                        "WHERE household_id = :hid "
                        "ORDER BY product_id, created_at ASC"
                    ),
                    {"hid": household_id},
                ).mappings()
                excs_by_pid: dict[UUID, list[ExcludedOdaSku]] = defaultdict(list)
                for r in exc_rows:
                    payload = r["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    payload["oda_product_id"] = r["oda_product_id"]
                    payload["reason"] = r["reason"]
                    payload["excluded_at"] = r["created_at"]
                    excs_by_pid[r["product_id"]].append(ExcludedOdaSku.model_validate(payload))

                all_pids = set(prefs_by_pid.keys()) | set(excs_by_pid.keys())
                for pid in all_pids:
                    result[pid] = (prefs_by_pid.get(pid, []), excs_by_pid.get(pid, []))
            except Exception:
                pass
        return result

    def set_preferences(
        self,
        household_id: UUID,
        product_id: UUID,
        preferred: list[PreferredOdaSku],
        excluded: list[ExcludedOdaSku],
        actor_id: str = "",
    ) -> None:
        is_sqlite = self.engine.dialect.name == "sqlite"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM kitchen_canonical_product_oda_preferences "
                    "WHERE household_id = :hid AND product_id = :pid"
                ),
                {"hid": household_id, "pid": product_id},
            )
            connection.execute(
                text(
                    "DELETE FROM kitchen_canonical_product_oda_exclusions "
                    "WHERE household_id = :hid AND product_id = :pid"
                ),
                {"hid": household_id, "pid": product_id},
            )
            for pref in preferred:
                payload_json = pref.model_dump_json()
                stmt = (
                    "INSERT INTO kitchen_canonical_product_oda_preferences "
                    "(household_id, product_id, oda_product_id, preference_order, payload, created_at) "
                    "VALUES (:hid, :pid, :sku, :rank, :payload, :created_at)"
                    if is_sqlite
                    else "INSERT INTO kitchen_canonical_product_oda_preferences "
                    "(household_id, product_id, oda_product_id, preference_order, payload, created_at) "
                    "VALUES (:hid, :pid, :sku, :rank, CAST(:payload AS jsonb), :created_at)"
                )
                connection.execute(
                    text(stmt),
                    {
                        "hid": household_id,
                        "pid": product_id,
                        "sku": pref.oda_product_id,
                        "rank": pref.rank,
                        "payload": payload_json,
                        "created_at": datetime.now(UTC),
                    },
                )
            for exc in excluded:
                payload_json = exc.model_dump_json()
                stmt = (
                    "INSERT INTO kitchen_canonical_product_oda_exclusions "
                    "(household_id, product_id, oda_product_id, reason, payload, created_at) "
                    "VALUES (:hid, :pid, :sku, :reason, :payload, :created_at)"
                    if is_sqlite
                    else "INSERT INTO kitchen_canonical_product_oda_exclusions "
                    "(household_id, product_id, oda_product_id, reason, payload, created_at) "
                    "VALUES (:hid, :pid, :sku, :reason, CAST(:payload AS jsonb), :created_at)"
                )
                connection.execute(
                    text(stmt),
                    {
                        "hid": household_id,
                        "pid": product_id,
                        "sku": exc.oda_product_id,
                        "reason": exc.reason,
                        "payload": payload_json,
                        "created_at": exc.excluded_at or datetime.now(UTC),
                    },
                )

    def get_stock_for_product(
        self, product: CanonicalProduct, aliases: list[ProductAliasItem]
    ) -> ProductStockOverview:
        match_names = {normalize_identity_text(product.canonical_name)}
        if product.variant:
            match_names.add(normalize_identity_text(f"{product.canonical_name} {product.variant}"))
        for a in aliases:
            match_names.add(normalize_identity_text(a.alias))

        matching_items: list[StockItemDetail] = []
        with self.engine.connect() as connection:
            try:
                rows = connection.execute(
                    text(
                        "SELECT id, name, unit, quantity, category, note "
                        "FROM kitchen_inventory "
                        "ORDER BY category, normalized_name"
                    )
                ).mappings()
                for r in rows:
                    norm = normalize_identity_text(r["name"])
                    norm_need = normalize_need_text(r["name"])
                    if norm in match_names or norm_need in match_names:
                        matching_items.append(
                            StockItemDetail(
                                inventory_id=r["id"],
                                name=r["name"],
                                quantity=Decimal(str(r["quantity"])),
                                unit=r["unit"],
                                category=r["category"],
                                note=r["note"],
                                provenance="kitchen_inventory",
                            )
                        )
            except Exception:
                pass

        if not matching_items:
            return ProductStockOverview(
                in_stock=False,
                total_quantity=Decimal(0),
                unit=None,
                items=[],
                provenance="kitchen_inventory",
            )

        total_qty = sum((item.quantity for item in matching_items), Decimal(0))
        unit = matching_items[0].unit if matching_items else None
        in_stock = any(item.quantity > Decimal(0) for item in matching_items)
        return ProductStockOverview(
            in_stock=in_stock,
            total_quantity=total_qty,
            unit=unit,
            items=matching_items,
            provenance="kitchen_inventory",
        )

    def get_purchase_evidence_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PurchaseEvidenceRecord]:
        evidence: list[PurchaseEvidenceRecord] = []
        match_names = {normalize_identity_text(product.canonical_name)}
        if product.variant:
            match_names.add(normalize_identity_text(f"{product.canonical_name} {product.variant}"))
        for a in aliases:
            match_names.add(normalize_identity_text(a.alias))

        preferred_sku_ids = {s.oda_product_id for s in preferred_skus}

        with self.engine.connect() as connection:
            # 1. kitchen_catalogue_sku_purchases
            try:
                sku_rows = connection.execute(
                    text(
                        "SELECT p.id, p.purchased_at, p.quantity, p.quantity_unit, p.evidence_kind, "
                        "p.evidence_ref, o.product_id AS oda_sku "
                        "FROM kitchen_catalogue_sku_purchases p "
                        "JOIN kitchen_catalogue_sku_options o ON o.id = p.option_id "
                        "WHERE p.household_id = :hid "
                        "ORDER BY p.purchased_at ASC"
                    ),
                    {"hid": household_id},
                ).mappings()
                for r in sku_rows:
                    sku_id = str(r["oda_sku"])
                    if sku_id in preferred_sku_ids:
                        prov = (
                            "provider_verified"
                            if r["evidence_kind"] in ("receipt", "retailer_order")
                            else "user_confirmed"
                        )
                        evidence.append(
                            PurchaseEvidenceRecord(
                                evidence_id=str(r["id"]),
                                purchased_at=r["purchased_at"],
                                provenance=prov,
                                evidence_source=r["evidence_kind"],
                                evidence_ref=r["evidence_ref"],
                                quantity=Decimal(str(r["quantity"]))
                                if r["quantity"] is not None
                                else None,
                                unit=r["quantity_unit"],
                                oda_product_id=sku_id,
                            )
                        )
            except Exception:
                pass

            # 2. kitchen_history (trip completions / verified purchases)
            try:
                hist_rows = connection.execute(
                    text(
                        "SELECT id, occurred_at, structured "
                        "FROM kitchen_history "
                        "WHERE kind = 'purchase' "
                        "ORDER BY occurred_at ASC"
                    )
                ).mappings()
                for r in hist_rows:
                    structured = r["structured"]
                    if isinstance(structured, str):
                        structured = json.loads(structured)
                    need = structured.get("need") or {}
                    need_name = need.get("name") or structured.get("product_name") or ""
                    norm_need = normalize_identity_text(need_name)
                    if norm_need in match_names:
                        qty = structured.get("actual_quantity")
                        unit = structured.get("actual_unit") or need.get("unit")
                        evidence.append(
                            PurchaseEvidenceRecord(
                                evidence_id=str(r["id"]),
                                purchased_at=r["occurred_at"],
                                provenance="user_confirmed",
                                evidence_source="kitchen_history",
                                evidence_ref=structured.get("trip_id"),
                                quantity=Decimal(str(qty)) if qty is not None else None,
                                unit=unit,
                                product_name=need_name,
                            )
                        )
            except Exception:
                pass

            # 3. provider_order_sources (Trumf, Coop, Oda)
            try:
                source_rows = connection.execute(
                    text(
                        "SELECT id, provider, payload, observed_at "
                        "FROM provider_order_sources "
                        "ORDER BY observed_at ASC"
                    )
                ).mappings()
                for r in source_rows:
                    provider = r["provider"]
                    payload = r["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    observed_at = r["observed_at"]

                    # Trumf format
                    if provider == "trumf":
                        lines = payload.get("varelinjer") or []
                        has_receipt = bool(
                            payload.get("hasReceipt")
                            or payload.get("harKvittering")
                            or payload.get("kvitteringsId")
                        )
                        tx_time_str = payload.get("transaksjonsTidspunkt") or payload.get("dato")
                        tx_time = (
                            datetime.fromisoformat(tx_time_str.replace("Z", "+00:00"))
                            if tx_time_str
                            else observed_at
                        )
                        for line in lines:
                            name = line.get("produktBeskrivelse") or line.get("vareTekst") or ""
                            if any(
                                pattern in normalize_identity_text(name) for pattern in match_names
                            ):
                                prov = "provider_verified" if has_receipt else "uncertain"
                                qty = line.get("antall")
                                evidence.append(
                                    PurchaseEvidenceRecord(
                                        evidence_id=str(r["id"]),
                                        purchased_at=tx_time,
                                        provenance=prov,
                                        evidence_source="trumf",
                                        evidence_ref=payload.get("kvitteringsId")
                                        or payload.get("batchId"),
                                        quantity=Decimal(str(qty)) if qty is not None else None,
                                        unit=line.get("enhetsType"),
                                        product_name=name,
                                    )
                                )

                    # Coop format
                    elif provider == "coop":
                        summary = payload.get("summary") or payload
                        lines = summary.get("lines") or []
                        p_date_str = summary.get("purchaseDate")
                        p_date = datetime.fromisoformat(p_date_str) if p_date_str else observed_at
                        has_receipt = bool(summary.get("receiptId") and lines)
                        for line in lines:
                            name = line.get("productName") or ""
                            if any(
                                pattern in normalize_identity_text(name) for pattern in match_names
                            ):
                                prov = "provider_verified" if has_receipt else "uncertain"
                                qty = line.get("quantity")
                                evidence.append(
                                    PurchaseEvidenceRecord(
                                        evidence_id=str(r["id"]),
                                        purchased_at=p_date,
                                        provenance=prov,
                                        evidence_source="coop",
                                        evidence_ref=summary.get("receiptId")
                                        or summary.get("summaryId"),
                                        quantity=Decimal(str(qty)) if qty is not None else None,
                                        unit="stk",
                                        product_name=name,
                                    )
                                )

                    # Oda format
                    elif provider == "oda":
                        lines = payload.get("lines") or []
                        status = payload.get("status") or ""
                        lifecycle = payload.get("lifecycle") or ""
                        has_receipt = status == "delivered" or lifecycle == "completed"
                        order_time_str = payload.get("orderedAt") or payload.get("deliveryDate")
                        order_time = (
                            datetime.fromisoformat(order_time_str)
                            if order_time_str
                            else observed_at
                        )
                        for line in lines:
                            name = line.get("sourceName") or line.get("name") or ""
                            if any(
                                pattern in normalize_identity_text(name) for pattern in match_names
                            ):
                                prov = "provider_verified" if has_receipt else "uncertain"
                                qty = line.get("quantity")
                                evidence.append(
                                    PurchaseEvidenceRecord(
                                        evidence_id=str(r["id"]),
                                        purchased_at=order_time,
                                        provenance=prov,
                                        evidence_source="oda",
                                        evidence_ref=payload.get("orderId"),
                                        quantity=Decimal(str(qty)) if qty is not None else None,
                                        unit=line.get("unit"),
                                        product_name=name,
                                    )
                                )
            except Exception:
                pass

        return evidence


class MemoryProductOverviewRepository:
    """Fast, network-free in-memory repository for isolated unit testing."""

    def __init__(
        self,
        products: list[CanonicalProduct] | None = None,
        aliases: list[ProductAliasItem] | None = None,
        identity_repo: Any = None,
    ):
        self.products: dict[UUID, CanonicalProduct] = {}
        self.aliases: dict[UUID, list[ProductAliasItem]] = defaultdict(list)
        self.preferences: dict[tuple[UUID, UUID], list[PreferredOdaSku]] = {}
        self.exclusions: dict[tuple[UUID, UUID], list[ExcludedOdaSku]] = {}
        self.stock_items: dict[UUID, list[StockItemDetail]] = defaultdict(list)
        self.purchase_evidence: dict[UUID, list[PurchaseEvidenceRecord]] = defaultdict(list)
        self.identity_repo = identity_repo

        if products:
            for p in products:
                self.add_product(p)
        elif identity_repo is not None:
            self._sync_from_identity_repo()

    def _sync_from_identity_repo(self) -> None:
        if self.identity_repo is None:
            return
        # Extract products
        product_candidates = getattr(self.identity_repo, "_products", None) or getattr(
            self.identity_repo, "products", None
        )
        if product_candidates:
            for p in product_candidates.values():
                self.products[p.id] = deepcopy(p)

        # Extract aliases
        alias_dict = getattr(self.identity_repo, "_aliases", None) or getattr(
            self.identity_repo, "aliases", None
        )
        reviewed_set = getattr(self.identity_repo, "reviewed", set())
        if alias_dict:
            for key, pids in alias_dict.items():
                # key is (kind, locale, context, alias)
                if len(key) == 4 and key[0] == "product":
                    _, loc, ctx, al = key
                    is_rev = key in reviewed_set
                    for pid in pids:
                        self.aliases[pid].append(
                            ProductAliasItem(
                                alias=al,
                                locale=loc,
                                context=ctx,
                                reviewed=is_rev,
                            )
                        )

    def add_product(self, product: CanonicalProduct) -> CanonicalProduct:
        self.products[product.id] = deepcopy(product)
        return self.products[product.id]

    def add_alias(
        self,
        product_id: UUID,
        alias: str,
        locale: str = "nb",
        context: str = "recipe",
        reviewed: bool = True,
    ) -> None:
        self.aliases[product_id].append(
            ProductAliasItem(
                alias=alias,
                locale=locale,
                context=context,
                reviewed=reviewed,
            )
        )

    def add_stock_item(
        self,
        product_id: UUID,
        name: str,
        quantity: Decimal,
        unit: str,
        category: str = "Other",
        note: str | None = None,
        inventory_id: UUID | None = None,
    ) -> StockItemDetail:
        detail = StockItemDetail(
            inventory_id=inventory_id,
            name=name,
            quantity=quantity,
            unit=unit,
            category=category,
            note=note,
            provenance="kitchen_inventory",
        )
        self.stock_items[product_id].append(detail)
        return detail

    def record_purchase_evidence(
        self,
        product_id: UUID,
        purchased_at: datetime,
        provenance: Literal["provider_verified", "user_confirmed", "uncertain"],
        evidence_source: str,
        evidence_ref: str | None = None,
        quantity: Decimal | None = None,
        unit: str | None = None,
        product_name: str | None = None,
        oda_product_id: str | None = None,
    ) -> PurchaseEvidenceRecord:
        record = PurchaseEvidenceRecord(
            purchased_at=purchased_at,
            provenance=provenance,
            evidence_source=evidence_source,
            evidence_ref=evidence_ref,
            quantity=quantity,
            unit=unit,
            product_name=product_name,
            oda_product_id=oda_product_id,
        )
        self.purchase_evidence[product_id].append(record)
        return record

    def list_products(self, query: str | None = None) -> list[CanonicalProduct]:
        if self.identity_repo is not None:
            self._sync_from_identity_repo()
        prods = list(self.products.values())
        if not query:
            return sorted(prods, key=lambda p: (p.canonical_name, p.variant, p.id))
        q = normalize_identity_text(query)
        matching = []
        for p in prods:
            names = {
                normalize_identity_text(p.canonical_name),
                normalize_identity_text(p.variant),
            }
            for a in self.aliases.get(p.id, []):
                names.add(normalize_identity_text(a.alias))
            if any(q in n for n in names if n):
                matching.append(p)
        return sorted(matching, key=lambda p: (p.canonical_name, p.variant, p.id))

    def get_product(self, product_id: UUID) -> CanonicalProduct | None:
        if self.identity_repo is not None:
            self._sync_from_identity_repo()
        prod = self.products.get(product_id)
        return deepcopy(prod) if prod else None

    def get_aliases_for_products(
        self, product_ids: list[UUID]
    ) -> dict[UUID, list[ProductAliasItem]]:
        if self.identity_repo is not None:
            self._sync_from_identity_repo()
        return {pid: deepcopy(self.aliases.get(pid, [])) for pid in product_ids}

    def get_preferences(
        self, household_id: UUID, product_id: UUID
    ) -> tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]:
        key = (household_id, product_id)
        prefs = deepcopy(self.preferences.get(key, []))
        excs = deepcopy(self.exclusions.get(key, []))
        return prefs, excs

    def get_all_preferences(
        self, household_id: UUID
    ) -> dict[UUID, tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]]:
        res: dict[UUID, tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]] = {}
        for (hid, pid), prefs in self.preferences.items():
            if hid == household_id:
                excs = self.exclusions.get((hid, pid), [])
                res[pid] = (deepcopy(prefs), deepcopy(excs))
        for (hid, pid), excs in self.exclusions.items():
            if hid == household_id and pid not in res:
                prefs = self.preferences.get((hid, pid), [])
                res[pid] = (deepcopy(prefs), deepcopy(excs))
        return res

    def set_preferences(
        self,
        household_id: UUID,
        product_id: UUID,
        preferred: list[PreferredOdaSku],
        excluded: list[ExcludedOdaSku],
        actor_id: str = "",
    ) -> None:
        key = (household_id, product_id)
        self.preferences[key] = deepcopy(preferred)
        self.exclusions[key] = deepcopy(excluded)

    def get_stock_for_product(
        self, product: CanonicalProduct, aliases: list[ProductAliasItem]
    ) -> ProductStockOverview:
        items = self.stock_items.get(product.id, [])
        if not items:
            return ProductStockOverview(
                in_stock=False,
                total_quantity=Decimal(0),
                unit=None,
                items=[],
                provenance="kitchen_inventory",
            )
        total_qty = sum((item.quantity for item in items), Decimal(0))
        unit = items[0].unit if items else None
        in_stock = any(item.quantity > Decimal(0) for item in items)
        return ProductStockOverview(
            in_stock=in_stock,
            total_quantity=total_qty,
            unit=unit,
            items=deepcopy(items),
            provenance="kitchen_inventory",
        )

    def get_purchase_evidence_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PurchaseEvidenceRecord]:
        return deepcopy(self.purchase_evidence.get(product.id, []))
