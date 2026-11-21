"""Persistence repositories for canonical product overview read model and preferences."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.identity import (
    AliasSourceType,
    CanonicalProduct,
    normalize_identity_text,
)
from home_kitchen.catalogue.overview_models import (
    ExcludedOdaSku,
    PreferredOdaSku,
    PreviouslyPurchasedSku,
    ProductAliasItem,
    ProductMergePreview,
    ProductStockOverview,
    ProductStockUpdate,
    PurchaseEvidenceRecord,
    RememberedOdaSku,
    StockItemDetail,
)
from home_kitchen.shopping import normalize_need_text
from home_kitchen.units import add_exact, canonicalize_quantity, compatible_units, normalize_unit


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("https://") or cleaned.startswith("http://"):
        return cleaned
    return None


def _product_match_names(
    product: CanonicalProduct,
    aliases: list[ProductAliasItem],
) -> set[str]:
    names = {normalize_identity_text(product.canonical_name)}
    if product.variant:
        names.add(normalize_identity_text(f"{product.canonical_name} {product.variant}"))
        names.add(normalize_identity_text(f"{product.canonical_name} ({product.variant})"))
    for a in aliases:
        norm = normalize_identity_text(a.alias)
        if norm:
            names.add(norm)
            if product.variant:
                names.add(normalize_identity_text(f"{a.alias} ({product.variant})"))
    names.discard("")
    return names


def _line_matches_product(
    line: dict[str, Any],
    product_id: UUID,
    match_names: set[str],
    candidate_names: list[str],
) -> bool:
    explicit_pid = line.get("canonical_product_id")
    if explicit_pid is not None and str(explicit_pid).strip():
        return str(explicit_pid).strip() == str(product_id)
    for raw_name in candidate_names:
        norm = normalize_identity_text(raw_name)
        if not norm:
            continue
        if any(pattern in norm for pattern in match_names if pattern):
            return True
    return False


def _word_match_names(name: str, match_names: set[str]) -> bool:
    norm = normalize_identity_text(name)
    if not norm:
        return False
    if norm in match_names:
        return True
    return any(
        re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", norm) is not None
        for pattern in match_names
        if len(pattern) >= 2
    )


def _iter_receipt_lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = [payload]
    for parent_key in ("summary", "receipt", "receiptDetails"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            containers.append(parent)
    for container in containers:
        for key in ("lines", "varelinjer", "items", "receiptLines"):
            raw_list = container.get(key)
            if isinstance(raw_list, list):
                for line in raw_list:
                    if isinstance(line, dict):
                        lines.append(line)
    return lines


def _receipt_line_candidates(line: dict[str, Any]) -> list[str]:
    raw_name = (
        line.get("produktBeskrivelse")
        or line.get("vareTekst")
        or line.get("productName")
        or line.get("sourceName")
        or line.get("name")
        or ""
    )
    kassal_name = (line.get("kassal") or {}).get("name") or ""
    return [n for n in (kassal_name, raw_name) if isinstance(n, str)]


def _matches_source_only_candidates(
    candidates: list[str],
    source_match_names: set[str],
    target_match_names: set[str],
) -> bool:
    matched_src = any(
        any(pat in normalize_identity_text(n) for pat in source_match_names if pat)
        for n in candidates
    )
    if not matched_src:
        return False
    matched_tgt = any(
        any(pat in normalize_identity_text(n) for pat in target_match_names if pat)
        for n in candidates
    )
    return not matched_tgt


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
    def upsert_stock_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        stock_update: ProductStockUpdate,
    ) -> ProductStockOverview: ...
    def get_purchase_evidence_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PurchaseEvidenceRecord]: ...
    def get_remembered_skus_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        household_id: UUID,
    ) -> list[RememberedOdaSku]: ...
    def get_previously_purchased_skus_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PreviouslyPurchasedSku]: ...
    def preview_merge(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID,
    ) -> ProductMergePreview: ...
    def merge_products(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID,
        actor_id: str = "",
    ) -> tuple[CanonicalProduct, CanonicalProduct]: ...


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
            cols = {
                col["name"] for col in inspect(connection).get_columns("kitchen_product_aliases")
            }
            source_col = (
                "source_type" if "source_type" in cols else "'reviewed_correction' AS source_type"
            )
            rows = connection.execute(
                text(
                    f"SELECT product_id, locale, context, alias, reviewed, {source_col} "
                    "FROM kitchen_product_aliases "
                    "WHERE product_id = ANY(:ids) "
                    "ORDER BY locale, reviewed DESC, alias"
                    if self.engine.dialect.name == "postgresql"
                    else f"SELECT product_id, locale, context, alias, reviewed, {source_col} "
                    "FROM kitchen_product_aliases "
                    "ORDER BY locale, reviewed DESC, alias"
                ),
                {"ids": product_ids} if self.engine.dialect.name == "postgresql" else {},
            ).mappings()
            for r in rows:
                pid = UUID(str(r["product_id"]))
                if pid in product_ids:
                    result[pid].append(
                        ProductAliasItem(
                            alias=r["alias"],
                            locale=r["locale"],
                            context=r["context"],
                            reviewed=bool(r["reviewed"]),
                            source_type=r["source_type"] or "reviewed_correction",
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
                    prefs_by_pid[UUID(str(r["product_id"]))].append(
                        PreferredOdaSku.model_validate(payload)
                    )

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
                    excs_by_pid[UUID(str(r["product_id"]))].append(
                        ExcludedOdaSku.model_validate(payload)
                    )

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
        match_names = _product_match_names(product, aliases)

        matching_items: list[StockItemDetail] = []
        with self.engine.connect() as connection:
            if inspect(connection).has_table("kitchen_inventory"):
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
                                inventory_id=UUID(str(r["id"])),
                                name=r["name"],
                                quantity=Decimal(str(r["quantity"])),
                                unit=r["unit"],
                                category=r["category"],
                                note=r["note"],
                                provenance="kitchen_inventory",
                            )
                        )

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

    def upsert_stock_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        stock_update: ProductStockUpdate,
    ) -> ProductStockOverview:
        existing = self.get_stock_for_product(product, aliases)
        target_name = product.canonical_name
        if stock_update.inventory_id is not None:
            matched_item = next(
                (i for i in existing.items if i.inventory_id == stock_update.inventory_id), None
            )
            if matched_item is not None:
                target_name = matched_item.name
        elif existing.items:
            target_name = existing.items[0].name
        else:
            nb_aliases = [
                a for a in aliases if a.locale.casefold().strip() == "nb" and a.alias.strip()
            ]
            if nb_aliases:
                nb_aliases.sort(key=lambda a: (not a.reviewed, a.context != "recipe"))
                target_name = nb_aliases[0].alias
            elif product.variant:
                target_name = f"{product.canonical_name} {product.variant}"

        name_key = normalize_need_text(target_name)
        quantity, unit_key = canonicalize_quantity(stock_update.quantity, stock_update.unit)
        qty_param = str(quantity) if self.engine.dialect.name == "sqlite" else quantity
        item_id = uuid5(NAMESPACE_URL, f"kitchen:inventory:{name_key}:{unit_key}")

        with self.engine.begin() as connection:
            if stock_update.inventory_id is not None:
                old_item = next(
                    (i for i in existing.items if i.inventory_id == stock_update.inventory_id), None
                )
                if old_item is not None and normalize_unit(old_item.unit) not in compatible_units(
                    unit_key
                ):
                    connection.execute(
                        text("DELETE FROM kitchen_inventory WHERE id = :id"),
                        {"id": stock_update.inventory_id},
                    )
            elif len(existing.items) == 1 and normalize_unit(
                existing.items[0].unit
            ) not in compatible_units(unit_key):
                if existing.items[0].inventory_id is not None:
                    connection.execute(
                        text("DELETE FROM kitchen_inventory WHERE id = :id"),
                        {"id": existing.items[0].inventory_id},
                    )

            legacy_keys = sorted(compatible_units(unit_key))
            unit_params = {f"unit_{idx}": val for idx, val in enumerate(legacy_keys)}
            placeholders = ",".join(f":{name}" for name in unit_params)
            rows = (
                connection.execute(
                    text(
                        "SELECT id, normalized_unit FROM kitchen_inventory "
                        f"WHERE normalized_name = :normalized_name AND normalized_unit IN ({placeholders}) "
                        "ORDER BY CASE WHEN normalized_unit = :canonical_unit THEN 0 ELSE 1 END, id"
                    ),
                    {
                        "normalized_name": name_key,
                        "canonical_unit": unit_key,
                        **unit_params,
                    },
                )
                .mappings()
                .all()
            )
            if rows:
                keep_id = rows[0]["id"]
                for row in rows[1:]:
                    connection.execute(
                        text("UPDATE kitchen_inventory SET quantity = 0 WHERE id = :id"),
                        {"id": row["id"]},
                    )
                connection.execute(
                    text(
                        "UPDATE kitchen_inventory SET name = :name, normalized_name = :normalized_name, "
                        "unit = :unit, normalized_unit = :normalized_unit, quantity = :quantity, "
                        "category = :category, note = :note WHERE id = :id"
                    ),
                    {
                        "id": keep_id,
                        "name": target_name,
                        "normalized_name": name_key,
                        "unit": unit_key,
                        "normalized_unit": unit_key,
                        "quantity": qty_param,
                        "category": stock_update.category,
                        "note": stock_update.note,
                    },
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO kitchen_inventory "
                        "(id, name, normalized_name, unit, normalized_unit, quantity, category, note) "
                        "VALUES (:id, :name, :normalized_name, :unit, :normalized_unit, :quantity, :category, :note)"
                    ),
                    {
                        "id": item_id,
                        "name": target_name,
                        "normalized_name": name_key,
                        "unit": unit_key,
                        "normalized_unit": unit_key,
                        "quantity": qty_param,
                        "category": stock_update.category,
                        "note": stock_update.note,
                    },
                )

        return self.get_stock_for_product(product, aliases)

    def _collect_purchase_records(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> tuple[list[PurchaseEvidenceRecord], list[PreviouslyPurchasedSku]]:
        evidence: list[PurchaseEvidenceRecord] = []
        purchased_map: dict[tuple[str, str], dict[str, Any]] = {}
        match_names = _product_match_names(product, aliases)
        preferred_sku_ids = {s.oda_product_id for s in preferred_skus}

        def _record_purchased_sku(
            *,
            provider: str,
            sku_id: str,
            name: str | None,
            store_name: str | None,
            ean: str | None,
            purchased_at: datetime | None,
            provenance: Literal["provider_verified", "user_confirmed", "uncertain"],
        ) -> None:
            if provenance not in ("provider_verified", "user_confirmed"):
                return
            clean_sku = sku_id.strip()
            clean_provider = provider.strip().casefold()
            if not clean_sku or not clean_provider:
                return
            key = (clean_provider, clean_sku)
            entry = purchased_map.get(key)
            if entry is None:
                purchased_map[key] = {
                    "sku_id": clean_sku,
                    "provider": clean_provider,
                    "name": name,
                    "store_name": store_name,
                    "ean": ean,
                    "last_purchased_at": purchased_at,
                    "purchase_count": 1,
                    "provenance": provenance,
                }
            else:
                entry["purchase_count"] += 1
                if not entry["name"] and name:
                    entry["name"] = name
                if not entry["store_name"] and store_name:
                    entry["store_name"] = store_name
                if not entry["ean"] and ean:
                    entry["ean"] = ean
                if purchased_at is not None and (
                    entry["last_purchased_at"] is None or purchased_at > entry["last_purchased_at"]
                ):
                    entry["last_purchased_at"] = purchased_at
                    if name:
                        entry["name"] = name
                if provenance == "provider_verified":
                    entry["provenance"] = "provider_verified"

        with self.engine.connect() as connection:
            inspector = inspect(connection)

            # 1. kitchen_catalogue_sku_purchases
            if inspector.has_table("kitchen_catalogue_sku_purchases") and inspector.has_table(
                "kitchen_catalogue_sku_options"
            ):
                has_snapshots = inspector.has_table("kitchen_catalogue_sku_snapshots")
                has_foods = inspector.has_table("kitchen_food_catalogue")
                select_cols = (
                    "p.id, p.purchased_at, p.quantity, p.quantity_unit, p.evidence_kind, "
                    "p.evidence_ref, o.provider, o.product_id AS oda_sku"
                )
                joins = "JOIN kitchen_catalogue_sku_options o ON o.id = p.option_id "
                if has_snapshots:
                    select_cols += ", s.snapshot AS snapshot"
                    joins += "LEFT JOIN kitchen_catalogue_sku_snapshots s ON s.id = p.snapshot_id "
                if has_foods:
                    select_cols += (
                        ", f.display_name AS food_display_name, f.source_name AS food_source_name"
                    )
                    joins += "LEFT JOIN kitchen_food_catalogue f ON f.id = o.food_catalogue_id "

                sku_rows = connection.execute(
                    text(
                        f"SELECT {select_cols} FROM kitchen_catalogue_sku_purchases p "
                        f"{joins}"
                        "WHERE p.household_id = :hid "
                        "ORDER BY p.purchased_at ASC"
                    ),
                    {"hid": household_id},
                ).mappings()
                for r in sku_rows:
                    sku_id = str(r["oda_sku"])
                    snap = _parse_json(r.get("snapshot")) if has_snapshots else None
                    snap_name = snap.get("name") if isinstance(snap, dict) else None
                    food_names = [
                        n
                        for n in (
                            r.get("food_display_name"),
                            (r.get("food_source_name") or "").split(",")[0]
                            if r.get("food_source_name")
                            else None,
                            snap_name,
                        )
                        if isinstance(n, str) and n.strip()
                    ]
                    matched = sku_id in preferred_sku_ids or any(
                        _word_match_names(n, match_names) for n in food_names
                    )
                    if matched:
                        prov: Literal["provider_verified", "user_confirmed", "uncertain"] = (
                            "provider_verified"
                            if r["evidence_kind"] in ("receipt", "retailer_order")
                            else "user_confirmed"
                        )
                        p_at = _parse_dt(r["purchased_at"])
                        if p_at is not None:
                            evidence.append(
                                PurchaseEvidenceRecord(
                                    evidence_id=str(r["id"]),
                                    purchased_at=p_at,
                                    provenance=prov,
                                    evidence_source=r["evidence_kind"],
                                    evidence_ref=r["evidence_ref"],
                                    quantity=Decimal(str(r["quantity"]))
                                    if r["quantity"] is not None
                                    else None,
                                    unit=r["quantity_unit"],
                                    product_name=snap_name,
                                    oda_product_id=sku_id,
                                )
                            )
                        _record_purchased_sku(
                            provider=str(r["provider"] or "oda"),
                            sku_id=sku_id,
                            name=snap_name,
                            store_name="Oda" if str(r["provider"]).lower() == "oda" else None,
                            ean=None,
                            purchased_at=p_at,
                            provenance=prov,
                        )

            # 2. kitchen_history (trip completions / verified purchases)
            if inspector.has_table("kitchen_history"):
                hist_rows = connection.execute(
                    text(
                        "SELECT id, occurred_at, structured "
                        "FROM kitchen_history "
                        "WHERE kind = 'purchase' "
                        "ORDER BY occurred_at ASC"
                    )
                ).mappings()
                for r in hist_rows:
                    structured = _parse_json(r["structured"]) or {}
                    need = structured.get("need") or {}
                    need_name = need.get("name") or structured.get("product_name") or ""
                    norm_need = normalize_identity_text(need_name)
                    explicit_pid = structured.get("canonical_product_id")
                    matched = (
                        str(explicit_pid) == str(product.id)
                        if explicit_pid
                        else norm_need in match_names
                    )
                    if matched:
                        occurred_at = _parse_dt(r["occurred_at"])
                        qty = structured.get("actual_quantity")
                        unit = structured.get("actual_unit") or need.get("unit")
                        snap = structured.get("product_snapshot") or {}
                        purchase_prod = (structured.get("purchase") or {}).get("product") or {}
                        sku_id = (
                            snap.get("product_id")
                            or purchase_prod.get("product_id")
                            or structured.get("oda_product_id")
                        )
                        provider = snap.get("provider") or purchase_prod.get("provider") or "store"
                        if occurred_at is not None:
                            evidence.append(
                                PurchaseEvidenceRecord(
                                    evidence_id=str(r["id"]),
                                    purchased_at=occurred_at,
                                    provenance="user_confirmed",
                                    evidence_source="kitchen_history",
                                    evidence_ref=structured.get("trip_id"),
                                    quantity=Decimal(str(qty)) if qty is not None else None,
                                    unit=unit,
                                    product_name=need_name,
                                    oda_product_id=str(sku_id) if sku_id else None,
                                )
                            )
                        if sku_id:
                            _record_purchased_sku(
                                provider=str(provider),
                                sku_id=str(sku_id),
                                name=snap.get("name") or need_name or None,
                                store_name="Oda" if str(provider).lower() == "oda" else None,
                                ean=None,
                                purchased_at=occurred_at,
                                provenance="user_confirmed",
                            )

            # 3. provider_order_sources (Trumf, Coop, Rema 1000, Oda)
            if inspector.has_table("provider_order_sources"):
                from home_kitchen.purchase_sync import resolve_effective_orders

                cols = {c["name"] for c in inspector.get_columns("provider_order_sources")}
                select_list = ["id", "provider", "payload", "observed_at"]
                if "external_id" in cols:
                    select_list.append("external_id")
                if "revision_id" in cols:
                    select_list.append("revision_id")
                if "account_id" in cols:
                    select_list.append("account_id")
                if "source_updated_at" in cols:
                    select_list.append("source_updated_at")
                source_rows = list(
                    connection.execute(
                        text(
                            f"SELECT {', '.join(select_list)} "
                            "FROM provider_order_sources "
                            "ORDER BY observed_at ASC, id ASC"
                        )
                    ).mappings()
                )
                normalized_rows = [
                    {
                        "id": row["id"],
                        "provider": str(row["provider"]).casefold(),
                        "account_id": str(row.get("account_id") or "default"),
                        "external_id": str(row.get("external_id") or row["id"]),
                        "revision_id": str(row.get("revision_id") or row["id"]),
                        "source_updated_at": _parse_dt(row.get("source_updated_at")),
                        "observed_at": _parse_dt(row["observed_at"]),
                        "payload": _parse_json(row["payload"]) or {},
                    }
                    for row in source_rows
                ]
                for eff in resolve_effective_orders(normalized_rows):
                    if eff.is_cancellation or eff.is_return:
                        continue
                    purchased_at = eff.transaction_at or eff.observed_at
                    for line in eff.lines:
                        if line.is_return:
                            continue
                        cand_names = [
                            n for n in (line.name, line.raw_name) if isinstance(n, str) and n
                        ]
                        line_proxy = {"canonical_product_id": line.canonical_product_id}
                        if not _line_matches_product(
                            line_proxy, product.id, match_names, cand_names
                        ):
                            continue
                        display_name = line.raw_name or line.name
                        default_unit = "stk" if eff.provider in ("coop", "rema1000") else None
                        if purchased_at is not None:
                            evidence.append(
                                PurchaseEvidenceRecord(
                                    evidence_id=eff.source_id
                                    or f"{eff.provider}:{eff.external_id}",
                                    purchased_at=purchased_at,
                                    provenance=eff.provenance,
                                    evidence_source=eff.provider,
                                    evidence_ref=eff.evidence_ref,
                                    quantity=line.quantity,
                                    unit=line.unit or default_unit,
                                    product_name=display_name,
                                    oda_product_id=(
                                        line.oda_product_id if eff.provider == "oda" else None
                                    ),
                                )
                            )
                        sku_id = (
                            (line.oda_product_id or line.ean or line.name)
                            if eff.provider == "oda"
                            else (line.ean or line.oda_product_id or display_name)
                        )
                        if sku_id:
                            _record_purchased_sku(
                                provider=eff.provider,
                                sku_id=str(sku_id),
                                name=line.name or display_name or None,
                                store_name=eff.store_name,
                                ean=line.ean,
                                purchased_at=purchased_at,
                                provenance=eff.provenance,
                            )

        purchased_skus = [
            PreviouslyPurchasedSku.model_validate(item) for item in purchased_map.values()
        ]
        purchased_skus.sort(
            key=lambda item: (
                -(item.last_purchased_at.timestamp()) if item.last_purchased_at else 0.0,
                -item.purchase_count,
                item.provider,
                item.sku_id,
            )
        )
        return evidence, purchased_skus

    def get_purchase_evidence_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PurchaseEvidenceRecord]:
        evidence, _ = self._collect_purchase_records(product, aliases, preferred_skus, household_id)
        return evidence

    def get_previously_purchased_skus_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PreviouslyPurchasedSku]:
        _, purchased_skus = self._collect_purchase_records(
            product, aliases, preferred_skus, household_id
        )
        return purchased_skus

    def get_remembered_skus_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        household_id: UUID,
    ) -> list[RememberedOdaSku]:
        match_names = _product_match_names(product, aliases)
        remembered_map: dict[str, dict[str, Any]] = {}

        def _record_remembered(
            *,
            oda_product_id: str,
            name: str | None = None,
            brand: str | None = None,
            price: str | None = None,
            unit_name: str | None = None,
            url: str | None = None,
            image_url: str | None = None,
            selected_at: datetime | None = None,
            source: str = "basket_selection",
        ) -> None:
            sku = oda_product_id.strip()
            if not sku:
                return
            entry = remembered_map.get(sku)
            safe_url = _safe_http_url(url)
            safe_img = _safe_http_url(image_url)
            if entry is None:
                remembered_map[sku] = {
                    "oda_product_id": sku,
                    "provider": "oda",
                    "name": name,
                    "brand": brand,
                    "price": price,
                    "unit_name": unit_name,
                    "url": safe_url,
                    "image_url": safe_img,
                    "last_selected_at": selected_at,
                    "selection_count": 1,
                    "source": source,
                }
            else:
                entry["selection_count"] += 1
                if selected_at is not None and (
                    entry["last_selected_at"] is None or selected_at > entry["last_selected_at"]
                ):
                    entry["last_selected_at"] = selected_at
                    entry["source"] = source
                    if name:
                        entry["name"] = name
                    if brand:
                        entry["brand"] = brand
                    if price:
                        entry["price"] = price
                    if unit_name:
                        entry["unit_name"] = unit_name
                    if safe_url:
                        entry["url"] = safe_url
                    if safe_img:
                        entry["image_url"] = safe_img
                else:
                    if not entry["name"] and name:
                        entry["name"] = name
                    if not entry["brand"] and brand:
                        entry["brand"] = brand
                    if not entry["price"] and price:
                        entry["price"] = price
                    if not entry["unit_name"] and unit_name:
                        entry["unit_name"] = unit_name
                    if not entry["url"] and safe_url:
                        entry["url"] = safe_url
                    if not entry["image_url"] and safe_img:
                        entry["image_url"] = safe_img

        with self.engine.connect() as connection:
            inspector = inspect(connection)
            basket_event_by_sku: dict[
                str, list[tuple[str, str | None, datetime | None, str | None]]
            ] = defaultdict(list)
            if inspector.has_table("kitchen_history"):
                hist_rows = connection.execute(
                    text(
                        "SELECT occurred_at, message, structured "
                        "FROM kitchen_history "
                        "WHERE kind = 'domain_event' "
                        "ORDER BY occurred_at ASC"
                    )
                ).mappings()
                for r in hist_rows:
                    structured = _parse_json(r["structured"]) or {}
                    if structured.get("action") != "basket_product_selected":
                        continue
                    if str(structured.get("provider") or "").casefold() != "oda":
                        continue
                    sku_id = str(structured.get("product_id") or "").strip()
                    if not sku_id:
                        continue
                    msg = str(r["message"] or "")
                    need_name = structured.get("need_name") or ""
                    selected_prod_name = None
                    if msg.startswith("Selected ") and " for " in msg:
                        left, right = msg[len("Selected ") :].rsplit(" for ", 1)
                        selected_prod_name = left.strip() or None
                        if not need_name:
                            need_name = right.strip()
                    basket_event_by_sku[sku_id].append(
                        (
                            str(need_name),
                            str(structured["canonical_product_id"])
                            if structured.get("canonical_product_id")
                            else None,
                            _parse_dt(r["occurred_at"]),
                            selected_prod_name,
                        )
                    )

            seen_basket_skus: set[str] = set()
            if inspector.has_table("kitchen_basket_selections"):
                sel_rows = connection.execute(
                    text(
                        "SELECT provider, product_id, payload, updated_at "
                        "FROM kitchen_basket_selections "
                        "WHERE provider = 'oda' "
                        "ORDER BY updated_at ASC"
                    )
                ).mappings()
                for r in sel_rows:
                    sku_id = str(r["product_id"] or "").strip()
                    payload = _parse_json(r["payload"]) or {}
                    prod_snap = payload.get("product") or {}
                    prod_name = str(prod_snap.get("name") or "")
                    explicit_pid = payload.get("canonical_product_id") or prod_snap.get(
                        "canonical_product_id"
                    )
                    matched = False
                    if explicit_pid:
                        matched = str(explicit_pid) == str(product.id)
                    else:
                        events = basket_event_by_sku.get(sku_id, [])
                        if any(
                            (ev_pid == str(product.id))
                            or (not ev_pid and normalize_identity_text(ev_need) in match_names)
                            for ev_need, ev_pid, _, _ in events
                        ):
                            matched = True
                        elif _word_match_names(prod_name, match_names):
                            matched = True

                    if matched and sku_id:
                        seen_basket_skus.add(sku_id)
                        price_val = prod_snap.get("price")
                        _record_remembered(
                            oda_product_id=sku_id,
                            name=prod_name or None,
                            brand=prod_snap.get("brand"),
                            price=str(price_val) if price_val is not None else None,
                            unit_name=prod_snap.get("pack_unit"),
                            url=prod_snap.get("url"),
                            image_url=prod_snap.get("image_url"),
                            selected_at=_parse_dt(r["updated_at"])
                            or _parse_dt(prod_snap.get("observed_at")),
                            source="basket_selection",
                        )

            # Include historical basket selections from kitchen_history even if cleared from active week
            for sku_id, events in basket_event_by_sku.items():
                if sku_id in seen_basket_skus:
                    continue
                for ev_need, ev_pid, ev_at, ev_prod_name in events:
                    matched = (
                        ev_pid == str(product.id)
                        if ev_pid
                        else normalize_identity_text(ev_need) in match_names
                    )
                    if matched:
                        _record_remembered(
                            oda_product_id=sku_id,
                            name=ev_prod_name,
                            selected_at=ev_at,
                            source="basket_selection",
                        )

            # Remembered catalogue SKU selections (kitchen_catalogue_sku_options/selections)
            if (
                inspector.has_table("kitchen_catalogue_sku_options")
                and inspector.has_table("kitchen_catalogue_sku_selections")
                and inspector.has_table("kitchen_catalogue_sku_snapshots")
            ):
                has_foods = inspector.has_table("kitchen_food_catalogue")
                food_cols = (
                    ", f.display_name AS food_display_name, f.source_name AS food_source_name"
                    if has_foods
                    else ""
                )
                food_join = (
                    "LEFT JOIN kitchen_food_catalogue f ON f.id = o.food_catalogue_id "
                    if has_foods
                    else ""
                )
                cat_rows = connection.execute(
                    text(
                        f"SELECT o.product_id AS oda_sku, s.snapshot, sel.selected_at {food_cols} "
                        "FROM kitchen_catalogue_sku_selections sel "
                        "JOIN kitchen_catalogue_sku_options o ON o.id = sel.option_id "
                        "JOIN kitchen_catalogue_sku_snapshots s ON s.id = sel.snapshot_id "
                        f"{food_join}"
                        "WHERE o.household_id = :hid AND lower(o.provider) = 'oda' "
                        "ORDER BY sel.selected_at ASC"
                    ),
                    {"hid": household_id},
                ).mappings()
                for r in cat_rows:
                    snap = _parse_json(r["snapshot"]) or {}
                    snap_name = str(snap.get("name") or "")
                    cand_names = [
                        n
                        for n in (
                            r.get("food_display_name"),
                            (r.get("food_source_name") or "").split(",")[0]
                            if r.get("food_source_name")
                            else None,
                            snap_name,
                        )
                        if isinstance(n, str) and n.strip()
                    ]
                    if any(_word_match_names(n, match_names) for n in cand_names):
                        _record_remembered(
                            oda_product_id=str(r["oda_sku"]),
                            name=snap_name or None,
                            unit_name=snap.get("quantity_unit"),
                            url=snap.get("url"),
                            image_url=snap.get("image_url"),
                            selected_at=_parse_dt(r["selected_at"]),
                            source="catalogue_selection",
                        )

        result = [RememberedOdaSku.model_validate(item) for item in remembered_map.values()]
        result.sort(
            key=lambda item: (
                -(item.last_selected_at.timestamp()) if item.last_selected_at else 0.0,
                -item.selection_count,
                item.oda_product_id,
            )
        )
        return result

    def preview_merge(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID,
    ) -> ProductMergePreview:
        if target_product_id == source_product_id:
            raise ValueError("Cannot merge a canonical product into itself")

        with self.engine.connect() as connection:
            inspector = inspect(connection)
            target_row = (
                connection.execute(
                    text(
                        "SELECT id, canonical_name, variant FROM kitchen_canonical_products "
                        "WHERE id = :id"
                    ),
                    {"id": target_product_id},
                )
                .mappings()
                .first()
            )
            if target_row is None:
                raise KeyError(f"Canonical product {target_product_id} not found")

            source_row = (
                connection.execute(
                    text(
                        "SELECT id, canonical_name, variant FROM kitchen_canonical_products "
                        "WHERE id = :id"
                    ),
                    {"id": source_product_id},
                )
                .mappings()
                .first()
            )
            if source_row is None:
                raise KeyError(f"Canonical product {source_product_id} not found")

            target = CanonicalProduct.model_validate(dict(target_row))
            source = CanonicalProduct.model_validate(dict(source_row))

            source_aliases: list[ProductAliasItem] = []
            target_aliases: list[ProductAliasItem] = []
            if inspector.has_table("kitchen_product_aliases"):
                alias_rows = (
                    connection.execute(
                        text(
                            "SELECT locale, context, alias, product_id, reviewed "
                            "FROM kitchen_product_aliases "
                            "WHERE product_id IN (:target_id, :source_id)"
                        ),
                        {"target_id": target_product_id, "source_id": source_product_id},
                    )
                    .mappings()
                    .all()
                )
                for r in alias_rows:
                    item = ProductAliasItem(
                        alias=r["alias"],
                        locale=r["locale"],
                        context=r["context"],
                        reviewed=bool(r["reviewed"]),
                    )
                    if UUID(str(r["product_id"])) == target_product_id:
                        target_aliases.append(item)
                    else:
                        source_aliases.append(item)

            aliases_to_move = len(source_aliases)
            source_match_names = _product_match_names(source, source_aliases)
            target_match_names = _product_match_names(target, target_aliases)

            preferences_to_move = 0
            if inspector.has_table("kitchen_canonical_product_oda_preferences"):
                preferences_to_move = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM kitchen_canonical_product_oda_preferences "
                            "WHERE product_id = :sid"
                        ),
                        {"sid": source_product_id},
                    ).scalar_one()
                )

            exclusions_to_move = 0
            if inspector.has_table("kitchen_canonical_product_oda_exclusions"):
                exclusions_to_move = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM kitchen_canonical_product_oda_exclusions "
                            "WHERE product_id = :sid"
                        ),
                        {"sid": source_product_id},
                    ).scalar_one()
                )

            stock_rows_to_move = 0
            if inspector.has_table("kitchen_inventory"):
                inv_rows = connection.execute(text("SELECT name FROM kitchen_inventory")).mappings()
                for r in inv_rows:
                    norm = normalize_identity_text(r["name"])
                    norm_need = normalize_need_text(r["name"])
                    if (norm in source_match_names or norm_need in source_match_names) and not (
                        norm in target_match_names or norm_need in target_match_names
                    ):
                        stock_rows_to_move += 1

            recipe_links_to_move = 0
            for recipe_table in ("recipes", "kitchen_recipe_translations"):
                if not inspector.has_table(recipe_table):
                    continue
                rec_rows = connection.execute(
                    text(f"SELECT ingredients FROM {recipe_table}")
                ).mappings()
                for r in rec_rows:
                    ings = _parse_json(r["ingredients"])
                    if not isinstance(ings, list):
                        continue
                    for ing in ings:
                        if isinstance(ing, dict) and str(
                            ing.get("canonical_product_id") or ""
                        ) == str(source_product_id):
                            recipe_links_to_move += 1

            purchase_history_rows_to_move = 0
            if inspector.has_table("provider_order_sources"):
                src_rows = connection.execute(
                    text("SELECT payload FROM provider_order_sources")
                ).mappings()
                for r in src_rows:
                    payload = _parse_json(r["payload"])
                    if not isinstance(payload, dict):
                        continue
                    for line in _iter_receipt_lines(payload):
                        cpid = line.get("canonical_product_id")
                        if cpid is not None and str(cpid) == str(source_product_id):
                            purchase_history_rows_to_move += 1
                        elif cpid is None and _matches_source_only_candidates(
                            _receipt_line_candidates(line),
                            source_match_names,
                            target_match_names,
                        ):
                            purchase_history_rows_to_move += 1
                    order_obj = payload.get("order")
                    if isinstance(order_obj, dict) and isinstance(order_obj.get("products"), list):
                        for item in order_obj["products"]:
                            if not isinstance(item, dict):
                                continue
                            prod_obj = item.get("product") or {}
                            cpid = item.get("canonical_product_id") or prod_obj.get(
                                "canonical_product_id"
                            )
                            if cpid is not None and str(cpid) == str(source_product_id):
                                purchase_history_rows_to_move += 1
                            elif cpid is None:
                                name = str(prod_obj.get("name") or "")
                                if _matches_source_only_candidates(
                                    [name], source_match_names, target_match_names
                                ):
                                    purchase_history_rows_to_move += 1

            if inspector.has_table("kitchen_history"):
                hist_rows = connection.execute(
                    text("SELECT structured FROM kitchen_history WHERE kind = 'purchase'")
                ).mappings()
                for r in hist_rows:
                    structured = _parse_json(r["structured"])
                    if not isinstance(structured, dict):
                        continue
                    cpid = structured.get("canonical_product_id")
                    if cpid is not None and str(cpid) == str(source_product_id):
                        purchase_history_rows_to_move += 1
                    elif cpid is None:
                        need = structured.get("need") or {}
                        need_name = need.get("name") or structured.get("product_name") or ""
                        norm_need = normalize_identity_text(need_name)
                        if norm_need in source_match_names and norm_need not in target_match_names:
                            purchase_history_rows_to_move += 1

        variant_mismatch = normalize_identity_text(source.variant) != normalize_identity_text(
            target.variant
        )
        return ProductMergePreview(
            target_product_id=target.id,
            source_product_id=source.id,
            target_canonical_name=target.canonical_name,
            source_canonical_name=source.canonical_name,
            target_variant=target.variant,
            source_variant=source.variant,
            target_display_name=target.canonical_name,
            source_display_name=source.canonical_name,
            variant_mismatch=variant_mismatch,
            aliases_to_move=aliases_to_move,
            stock_rows_to_move=stock_rows_to_move,
            recipe_links_to_move=recipe_links_to_move,
            preferences_to_move=preferences_to_move,
            exclusions_to_move=exclusions_to_move,
            purchase_history_rows_to_move=purchase_history_rows_to_move,
        )

    def merge_products(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID,
        actor_id: str = "",
    ) -> tuple[CanonicalProduct, CanonicalProduct]:
        if target_product_id == source_product_id:
            raise ValueError("Cannot merge a canonical product into itself")

        is_sqlite = self.engine.dialect.name == "sqlite"

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            target_row = (
                connection.execute(
                    text(
                        "SELECT id, canonical_name, variant FROM kitchen_canonical_products "
                        "WHERE id = :id"
                    ),
                    {"id": target_product_id},
                )
                .mappings()
                .first()
            )
            if target_row is None:
                raise KeyError(f"Canonical product {target_product_id} not found")

            source_row = (
                connection.execute(
                    text(
                        "SELECT id, canonical_name, variant FROM kitchen_canonical_products "
                        "WHERE id = :id"
                    ),
                    {"id": source_product_id},
                )
                .mappings()
                .first()
            )
            if source_row is None:
                raise KeyError(f"Canonical product {source_product_id} not found")

            target = CanonicalProduct.model_validate(dict(target_row))
            source = CanonicalProduct.model_validate(dict(source_row))

            # Collect source & target aliases before repointing
            source_aliases: list[ProductAliasItem] = []
            target_aliases: list[ProductAliasItem] = []
            if inspector.has_table("kitchen_product_aliases"):
                alias_cols = {
                    col["name"] for col in inspector.get_columns("kitchen_product_aliases")
                }
                has_hh = "household_id" in alias_cols
                has_src = "source_type" in alias_cols
                hh_col = "household_id" if has_hh else "NULL AS household_id"
                src_col = "source_type" if has_src else "'reviewed_correction' AS source_type"
                alias_rows = (
                    connection.execute(
                        text(
                            f"SELECT {hh_col}, locale, context, normalized_alias, alias, product_id, reviewed, {src_col} "
                            "FROM kitchen_product_aliases "
                            "WHERE product_id IN (:target_id, :source_id)"
                        ),
                        {"target_id": target_product_id, "source_id": source_product_id},
                    )
                    .mappings()
                    .all()
                )
                target_alias_map: dict[tuple[Any, str, str, str], dict[str, Any]] = {}
                source_alias_rows: list[dict[str, Any]] = []
                for r in alias_rows:
                    pid = UUID(str(r["product_id"]))
                    row_dict = dict(r)
                    item = ProductAliasItem(
                        alias=r["alias"],
                        locale=r["locale"],
                        context=r["context"],
                        reviewed=bool(r["reviewed"]),
                        source_type=r["source_type"] or "reviewed_correction",
                    )
                    key = (
                        str(r["household_id"]) if r["household_id"] is not None else None,
                        r["locale"],
                        r["context"],
                        r["normalized_alias"],
                    )
                    if pid == target_product_id:
                        target_aliases.append(item)
                        target_alias_map[key] = row_dict
                    else:
                        source_aliases.append(item)
                        source_alias_rows.append(row_dict)

                for sr in source_alias_rows:
                    hh_val = sr["household_id"]
                    key = (
                        str(hh_val) if hh_val is not None else None,
                        sr["locale"],
                        sr["context"],
                        sr["normalized_alias"],
                    )
                    hh_clause = (
                        " AND household_id = :hh"
                        if has_hh and hh_val is not None
                        else (" AND household_id IS NULL" if has_hh else "")
                    )
                    base_where_params: dict[str, Any] = {
                        "loc": sr["locale"],
                        "ctx": sr["context"],
                        "norm": sr["normalized_alias"],
                    }
                    if has_hh and hh_val is not None:
                        base_where_params["hh"] = hh_val
                    existing_target = target_alias_map.get(key)
                    if existing_target is not None:
                        if bool(sr["reviewed"]) and not bool(existing_target["reviewed"]):
                            set_src = ", source_type = :src" if has_src else ""
                            update_params = {
                                **base_where_params,
                                "rev": True,
                                "pid": target_product_id,
                            }
                            if has_src:
                                update_params["src"] = sr["source_type"] or "reviewed_correction"
                            connection.execute(
                                text(
                                    f"UPDATE kitchen_product_aliases SET reviewed = :rev{set_src} "
                                    f"WHERE locale = :loc AND context = :ctx "
                                    f"AND normalized_alias = :norm AND product_id = :pid{hh_clause}"
                                ),
                                update_params,
                            )
                        connection.execute(
                            text(
                                "DELETE FROM kitchen_product_aliases "
                                "WHERE locale = :loc AND context = :ctx "
                                f"AND normalized_alias = :norm AND product_id = :pid{hh_clause}"
                            ),
                            {**base_where_params, "pid": source_product_id},
                        )
                    else:
                        connection.execute(
                            text(
                                "UPDATE kitchen_product_aliases SET product_id = :target_id "
                                "WHERE locale = :loc AND context = :ctx "
                                f"AND normalized_alias = :norm AND product_id = :source_id{hh_clause}"
                            ),
                            {
                                **base_where_params,
                                "target_id": target_product_id,
                                "source_id": source_product_id,
                            },
                        )
                        target_aliases.append(
                            ProductAliasItem(
                                alias=sr["alias"],
                                locale=sr["locale"],
                                context=sr["context"],
                                reviewed=bool(sr["reviewed"]),
                                source_type=sr["source_type"] or "reviewed_correction",
                            )
                        )

            source_match_names = _product_match_names(source, source_aliases)
            target_match_names_after = _product_match_names(target, target_aliases)

            # Reconcile preferred and excluded Oda SKUs across all households
            has_prefs = inspector.has_table("kitchen_canonical_product_oda_preferences")
            has_excls = inspector.has_table("kitchen_canonical_product_oda_exclusions")
            if has_prefs or has_excls:
                hids: set[UUID] = set()
                if has_prefs:
                    for hid_val in connection.execute(
                        text(
                            "SELECT DISTINCT household_id FROM kitchen_canonical_product_oda_preferences "
                            "WHERE product_id IN (:tid, :sid)"
                        ),
                        {"tid": target_product_id, "sid": source_product_id},
                    ).scalars():
                        hids.add(UUID(str(hid_val)))
                if has_excls:
                    for hid_val in connection.execute(
                        text(
                            "SELECT DISTINCT household_id FROM kitchen_canonical_product_oda_exclusions "
                            "WHERE product_id IN (:tid, :sid)"
                        ),
                        {"tid": target_product_id, "sid": source_product_id},
                    ).scalars():
                        hids.add(UUID(str(hid_val)))

                for hid in hids:
                    merged_exclusions: dict[str, ExcludedOdaSku] = {}
                    if has_excls:
                        for pid in (target_product_id, source_product_id):
                            exc_rows = connection.execute(
                                text(
                                    "SELECT oda_product_id, reason, payload, created_at "
                                    "FROM kitchen_canonical_product_oda_exclusions "
                                    "WHERE household_id = :hid AND product_id = :pid "
                                    "ORDER BY created_at ASC"
                                ),
                                {"hid": hid, "pid": pid},
                            ).mappings()
                            for r in exc_rows:
                                payload = _parse_json(r["payload"]) or {}
                                payload["oda_product_id"] = r["oda_product_id"]
                                payload["reason"] = r["reason"]
                                payload["excluded_at"] = _parse_dt(r["created_at"])
                                exc_obj = ExcludedOdaSku.model_validate(payload)
                                existing_exc = merged_exclusions.get(exc_obj.oda_product_id)
                                if existing_exc is None:
                                    merged_exclusions[exc_obj.oda_product_id] = exc_obj
                                else:
                                    if not existing_exc.reason and exc_obj.reason:
                                        existing_exc.reason = exc_obj.reason
                                    if not existing_exc.name and exc_obj.name:
                                        existing_exc.name = exc_obj.name

                    excluded_ids = set(merged_exclusions.keys())
                    merged_preferred: list[PreferredOdaSku] = []
                    seen_pref_ids: set[str] = set()
                    if has_prefs:
                        for pid in (target_product_id, source_product_id):
                            pref_rows = connection.execute(
                                text(
                                    "SELECT oda_product_id, preference_order, payload "
                                    "FROM kitchen_canonical_product_oda_preferences "
                                    "WHERE household_id = :hid AND product_id = :pid "
                                    "ORDER BY preference_order ASC, created_at ASC"
                                ),
                                {"hid": hid, "pid": pid},
                            ).mappings()
                            for r in pref_rows:
                                payload = _parse_json(r["payload"]) or {}
                                payload["oda_product_id"] = r["oda_product_id"]
                                payload["rank"] = r["preference_order"]
                                pref_obj = PreferredOdaSku.model_validate(payload)
                                if (
                                    pref_obj.oda_product_id in excluded_ids
                                    or pref_obj.oda_product_id in seen_pref_ids
                                ):
                                    continue
                                seen_pref_ids.add(pref_obj.oda_product_id)
                                merged_preferred.append(
                                    pref_obj.model_copy(update={"rank": len(merged_preferred) + 1})
                                )

                    if has_prefs:
                        connection.execute(
                            text(
                                "DELETE FROM kitchen_canonical_product_oda_preferences "
                                "WHERE household_id = :hid AND product_id IN (:tid, :sid)"
                            ),
                            {"hid": hid, "tid": target_product_id, "sid": source_product_id},
                        )
                        for pref in merged_preferred:
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
                                    "hid": hid,
                                    "pid": target_product_id,
                                    "sku": pref.oda_product_id,
                                    "rank": pref.rank,
                                    "payload": pref.model_dump_json(),
                                    "created_at": datetime.now(UTC),
                                },
                            )

                    if has_excls:
                        connection.execute(
                            text(
                                "DELETE FROM kitchen_canonical_product_oda_exclusions "
                                "WHERE household_id = :hid AND product_id IN (:tid, :sid)"
                            ),
                            {"hid": hid, "tid": target_product_id, "sid": source_product_id},
                        )
                        for exc in merged_exclusions.values():
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
                                    "hid": hid,
                                    "pid": target_product_id,
                                    "sku": exc.oda_product_id,
                                    "reason": exc.reason,
                                    "payload": exc.model_dump_json(),
                                    "created_at": exc.excluded_at or datetime.now(UTC),
                                },
                            )

            # Repoint / merge kitchen_inventory rows that matched source but wouldn't match target
            if inspector.has_table("kitchen_inventory"):
                inv_rows = (
                    connection.execute(
                        text(
                            "SELECT id, name, normalized_name, unit, normalized_unit, quantity "
                            "FROM kitchen_inventory"
                        )
                    )
                    .mappings()
                    .all()
                )
                target_inv_name = target.canonical_name
                nb_target_aliases = [
                    a
                    for a in target_aliases
                    if a.locale.casefold().strip() == "nb" and a.alias.strip()
                ]
                if nb_target_aliases:
                    nb_target_aliases.sort(key=lambda a: (not a.reviewed, a.context != "recipe"))
                    target_inv_name = nb_target_aliases[0].alias
                target_inv_norm = normalize_need_text(target_inv_name)
                by_key: dict[tuple[str, str], dict[str, Any]] = {
                    (r["normalized_name"], r["normalized_unit"]): dict(r) for r in inv_rows
                }
                for r in inv_rows:
                    norm = normalize_identity_text(r["name"])
                    norm_need = normalize_need_text(r["name"])
                    if (norm in source_match_names or norm_need in source_match_names) and not (
                        norm in target_match_names_after or norm_need in target_match_names_after
                    ):
                        existing_target_row = by_key.get((target_inv_norm, r["normalized_unit"]))
                        if existing_target_row is not None and existing_target_row["id"] != r["id"]:
                            combined_qty = add_exact(
                                Decimal(str(existing_target_row["quantity"])),
                                Decimal(str(r["quantity"])),
                            )
                            connection.execute(
                                text("UPDATE kitchen_inventory SET quantity = :qty WHERE id = :id"),
                                {
                                    "qty": str(combined_qty) if is_sqlite else combined_qty,
                                    "id": existing_target_row["id"],
                                },
                            )
                            connection.execute(
                                text("DELETE FROM kitchen_inventory WHERE id = :id"),
                                {"id": r["id"]},
                            )
                            existing_target_row["quantity"] = combined_qty
                        else:
                            connection.execute(
                                text(
                                    "UPDATE kitchen_inventory SET name = :name, "
                                    "normalized_name = :norm_name WHERE id = :id"
                                ),
                                {
                                    "name": target_inv_name,
                                    "norm_name": target_inv_norm,
                                    "id": r["id"],
                                },
                            )
                            by_key.pop((r["normalized_name"], r["normalized_unit"]), None)
                            updated_entry = dict(r)
                            updated_entry["name"] = target_inv_name
                            updated_entry["normalized_name"] = target_inv_norm
                            by_key[(target_inv_norm, r["normalized_unit"])] = updated_entry

            # Repoint recipe ingredient mappings
            for recipe_table in ("recipes", "kitchen_recipe_translations"):
                if not inspector.has_table(recipe_table):
                    continue
                rec_rows = (
                    connection.execute(text(f"SELECT id, ingredients FROM {recipe_table}"))
                    .mappings()
                    .all()
                )
                for r in rec_rows:
                    ings = _parse_json(r["ingredients"])
                    if not isinstance(ings, list):
                        continue
                    changed = False
                    for ing in ings:
                        if isinstance(ing, dict) and str(
                            ing.get("canonical_product_id") or ""
                        ) == str(source_product_id):
                            ing["canonical_product_id"] = str(target_product_id)
                            changed = True
                    if changed:
                        stmt = (
                            f"UPDATE {recipe_table} SET ingredients = :ings WHERE id = :id"
                            if is_sqlite
                            else f"UPDATE {recipe_table} SET ingredients = CAST(:ings AS jsonb) WHERE id = :id"
                        )
                        connection.execute(
                            text(stmt),
                            {"ings": json.dumps(ings, ensure_ascii=False), "id": r["id"]},
                        )

            # Repoint purchase history references in provider_order_sources
            if inspector.has_table("provider_order_sources"):
                src_rows = (
                    connection.execute(
                        text("SELECT id, provider, payload FROM provider_order_sources")
                    )
                    .mappings()
                    .all()
                )
                for r in src_rows:
                    payload = _parse_json(r["payload"])
                    if not isinstance(payload, dict):
                        continue
                    changed = False

                    for line in _iter_receipt_lines(payload):
                        cpid = line.get("canonical_product_id")
                        if cpid is not None and str(cpid) == str(source_product_id):
                            line["canonical_product_id"] = str(target_product_id)
                            changed = True
                        elif cpid is None and _matches_source_only_candidates(
                            _receipt_line_candidates(line),
                            source_match_names,
                            target_match_names_after,
                        ):
                            line["canonical_product_id"] = str(target_product_id)
                            changed = True
                    order_obj = payload.get("order")
                    if isinstance(order_obj, dict) and isinstance(order_obj.get("products"), list):
                        for item in order_obj["products"]:
                            if not isinstance(item, dict):
                                continue
                            prod_obj = item.get("product") or {}
                            cpid = item.get("canonical_product_id") or prod_obj.get(
                                "canonical_product_id"
                            )
                            if cpid is not None and str(cpid) == str(source_product_id):
                                item["canonical_product_id"] = str(target_product_id)
                                if "canonical_product_id" in prod_obj:
                                    prod_obj["canonical_product_id"] = str(target_product_id)
                                changed = True
                            elif cpid is None:
                                name = str(prod_obj.get("name") or "")
                                norm = normalize_identity_text(name)
                                if any(p in norm for p in source_match_names if p) and not any(
                                    p in norm for p in target_match_names_after if p
                                ):
                                    item["canonical_product_id"] = str(target_product_id)
                                    changed = True

                    if changed:
                        stmt = (
                            "UPDATE provider_order_sources SET payload = :payload WHERE id = :id"
                            if is_sqlite
                            else "UPDATE provider_order_sources SET payload = CAST(:payload AS jsonb) WHERE id = :id"
                        )
                        connection.execute(
                            text(stmt),
                            {"payload": json.dumps(payload, ensure_ascii=False), "id": r["id"]},
                        )

            # Repoint purchase history references in kitchen_history
            if inspector.has_table("kitchen_history"):
                hist_rows = (
                    connection.execute(
                        text("SELECT id, structured FROM kitchen_history WHERE kind = 'purchase'")
                    )
                    .mappings()
                    .all()
                )
                for r in hist_rows:
                    structured = _parse_json(r["structured"])
                    if not isinstance(structured, dict):
                        continue
                    changed = False
                    cpid = structured.get("canonical_product_id")
                    if cpid is not None and str(cpid) == str(source_product_id):
                        structured["canonical_product_id"] = str(target_product_id)
                        changed = True
                    elif cpid is None:
                        need = structured.get("need") or {}
                        need_name = need.get("name") or structured.get("product_name") or ""
                        norm_need = normalize_identity_text(need_name)
                        if (
                            norm_need in source_match_names
                            and norm_need not in target_match_names_after
                        ):
                            structured["canonical_product_id"] = str(target_product_id)
                            changed = True
                    if changed:
                        stmt = (
                            "UPDATE kitchen_history SET structured = :structured WHERE id = :id"
                            if is_sqlite
                            else "UPDATE kitchen_history SET structured = CAST(:structured AS jsonb) WHERE id = :id"
                        )
                        connection.execute(
                            text(stmt),
                            {
                                "structured": json.dumps(structured, ensure_ascii=False),
                                "id": r["id"],
                            },
                        )

            # Delete duplicate source canonical product
            connection.execute(
                text("DELETE FROM kitchen_canonical_products WHERE id = :id"),
                {"id": source_product_id},
            )

        return target, source


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
        self.remembered_skus: dict[UUID, list[RememberedOdaSku]] = defaultdict(list)
        self.previously_purchased_skus: dict[UUID, list[PreviouslyPurchasedSku]] = defaultdict(list)
        self.recipe_links: dict[UUID, int] = defaultdict(int)
        self._merged_source_ids: set[UUID] = set()
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
                if p.id not in self._merged_source_ids:
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
                        if pid in self._merged_source_ids:
                            continue
                        existing_keys = {
                            (a.locale, a.context, normalize_identity_text(a.alias))
                            for a in self.aliases[pid]
                        }
                        if (loc, ctx, normalize_identity_text(al)) not in existing_keys:
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
        source_type: AliasSourceType = "reviewed_correction",
    ) -> None:
        self.aliases[product_id].append(
            ProductAliasItem(
                alias=alias,
                locale=locale,
                context=context,
                reviewed=reviewed,
                source_type=source_type,
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
        if oda_product_id and provenance in ("provider_verified", "user_confirmed"):
            self.record_previously_purchased_sku(
                product_id=product_id,
                sku_id=oda_product_id,
                provider="oda" if evidence_source == "oda" else evidence_source,
                name=product_name,
                last_purchased_at=purchased_at,
                provenance=provenance,
            )
        return record

    def record_remembered_sku(
        self,
        product_id: UUID,
        oda_product_id: str,
        name: str | None = None,
        brand: str | None = None,
        price: str | None = None,
        unit_name: str | None = None,
        last_selected_at: datetime | None = None,
        source: str = "basket_selection",
    ) -> RememberedOdaSku:
        sku_clean = oda_product_id.strip()
        for idx, existing in enumerate(self.remembered_skus[product_id]):
            if existing.oda_product_id == sku_clean:
                updated = existing.model_copy(
                    update={
                        "selection_count": existing.selection_count + 1,
                        "last_selected_at": last_selected_at or existing.last_selected_at,
                        "name": name or existing.name,
                        "brand": brand or existing.brand,
                        "price": price or existing.price,
                        "unit_name": unit_name or existing.unit_name,
                        "source": source,
                    }
                )
                self.remembered_skus[product_id][idx] = updated
                return updated
        item = RememberedOdaSku(
            oda_product_id=sku_clean,
            provider="oda",
            name=name,
            brand=brand,
            price=price,
            unit_name=unit_name,
            last_selected_at=last_selected_at,
            selection_count=1,
            source=source,
        )
        self.remembered_skus[product_id].append(item)
        return item

    def record_previously_purchased_sku(
        self,
        product_id: UUID,
        sku_id: str,
        provider: str,
        name: str | None = None,
        store_name: str | None = None,
        ean: str | None = None,
        last_purchased_at: datetime | None = None,
        provenance: Literal[
            "provider_verified", "user_confirmed", "uncertain"
        ] = "provider_verified",
    ) -> PreviouslyPurchasedSku:
        sku_clean = sku_id.strip()
        prov_clean = provider.strip().casefold()
        for idx, existing in enumerate(self.previously_purchased_skus[product_id]):
            if existing.sku_id == sku_clean and existing.provider == prov_clean:
                updated = existing.model_copy(
                    update={
                        "purchase_count": existing.purchase_count + 1,
                        "last_purchased_at": last_purchased_at or existing.last_purchased_at,
                        "name": name or existing.name,
                        "store_name": store_name or existing.store_name,
                        "ean": ean or existing.ean,
                        "provenance": "provider_verified"
                        if provenance == "provider_verified"
                        or existing.provenance == "provider_verified"
                        else provenance,
                    }
                )
                self.previously_purchased_skus[product_id][idx] = updated
                return updated
        item = PreviouslyPurchasedSku(
            sku_id=sku_clean,
            provider=prov_clean,
            name=name,
            store_name=store_name,
            ean=ean,
            last_purchased_at=last_purchased_at,
            purchase_count=1,
            provenance=provenance,
        )
        self.previously_purchased_skus[product_id].append(item)
        return item

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

    def upsert_stock_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        stock_update: ProductStockUpdate,
    ) -> ProductStockOverview:
        items = self.stock_items[product.id]
        target_name = product.canonical_name
        if items:
            target_name = items[0].name
        else:
            nb_aliases = [
                a for a in aliases if a.locale.casefold().strip() == "nb" and a.alias.strip()
            ]
            if nb_aliases:
                target_name = nb_aliases[0].alias

        quantity, unit_key = canonicalize_quantity(stock_update.quantity, stock_update.unit)
        inv_id = stock_update.inventory_id or uuid5(
            NAMESPACE_URL, f"kitchen:inventory:{normalize_need_text(target_name)}:{unit_key}"
        )

        replaced = False
        new_items: list[StockItemDetail] = []
        for item in items:
            matches_id = (
                stock_update.inventory_id is not None
                and item.inventory_id == stock_update.inventory_id
            )
            matches_single = stock_update.inventory_id is None and len(items) == 1
            matches_compat = normalize_unit(item.unit) in compatible_units(unit_key)
            if not replaced and (matches_id or matches_single or matches_compat):
                new_items.append(
                    StockItemDetail(
                        inventory_id=item.inventory_id or inv_id,
                        name=item.name,
                        quantity=quantity,
                        unit=unit_key,
                        category=stock_update.category,
                        note=stock_update.note,
                        provenance="kitchen_inventory",
                    )
                )
                replaced = True
            else:
                new_items.append(item)

        if not replaced:
            new_items.append(
                StockItemDetail(
                    inventory_id=inv_id,
                    name=target_name,
                    quantity=quantity,
                    unit=unit_key,
                    category=stock_update.category,
                    note=stock_update.note,
                    provenance="kitchen_inventory",
                )
            )
        self.stock_items[product.id] = new_items
        return self.get_stock_for_product(product, aliases)

    def get_purchase_evidence_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PurchaseEvidenceRecord]:
        return deepcopy(self.purchase_evidence.get(product.id, []))

    def get_remembered_skus_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        household_id: UUID,
    ) -> list[RememberedOdaSku]:
        return deepcopy(self.remembered_skus.get(product.id, []))

    def get_previously_purchased_skus_for_product(
        self,
        product: CanonicalProduct,
        aliases: list[ProductAliasItem],
        preferred_skus: list[PreferredOdaSku],
        household_id: UUID,
    ) -> list[PreviouslyPurchasedSku]:
        return deepcopy(self.previously_purchased_skus.get(product.id, []))

    def record_recipe_link(self, product_id: UUID, count: int = 1) -> None:
        self.recipe_links[product_id] += count

    def preview_merge(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID,
    ) -> ProductMergePreview:
        if target_product_id == source_product_id:
            raise ValueError("Cannot merge a canonical product into itself")
        if self.identity_repo is not None:
            self._sync_from_identity_repo()
        target = self.products.get(target_product_id)
        if target is None:
            raise KeyError(f"Canonical product {target_product_id} not found")
        source = self.products.get(source_product_id)
        if source is None:
            raise KeyError(f"Canonical product {source_product_id} not found")

        aliases_to_move = len(self.aliases.get(source_product_id, []))
        stock_rows_to_move = len(self.stock_items.get(source_product_id, []))
        recipe_links_to_move = self.recipe_links.get(source_product_id, 0)
        preferences_to_move = sum(
            len(v) for (_hid, pid), v in self.preferences.items() if pid == source_product_id
        )
        exclusions_to_move = sum(
            len(v) for (_hid, pid), v in self.exclusions.items() if pid == source_product_id
        )
        purchase_history_rows_to_move = len(self.purchase_evidence.get(source_product_id, []))
        variant_mismatch = normalize_identity_text(source.variant) != normalize_identity_text(
            target.variant
        )
        return ProductMergePreview(
            target_product_id=target.id,
            source_product_id=source.id,
            target_canonical_name=target.canonical_name,
            source_canonical_name=source.canonical_name,
            target_variant=target.variant,
            source_variant=source.variant,
            target_display_name=target.canonical_name,
            source_display_name=source.canonical_name,
            variant_mismatch=variant_mismatch,
            aliases_to_move=aliases_to_move,
            stock_rows_to_move=stock_rows_to_move,
            recipe_links_to_move=recipe_links_to_move,
            preferences_to_move=preferences_to_move,
            exclusions_to_move=exclusions_to_move,
            purchase_history_rows_to_move=purchase_history_rows_to_move,
        )

    def merge_products(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID,
        actor_id: str = "",
    ) -> tuple[CanonicalProduct, CanonicalProduct]:
        if target_product_id == source_product_id:
            raise ValueError("Cannot merge a canonical product into itself")
        if self.identity_repo is not None:
            self._sync_from_identity_repo()
        target = self.products.get(target_product_id)
        if target is None:
            raise KeyError(f"Canonical product {target_product_id} not found")
        source = self.products.get(source_product_id)
        if source is None:
            raise KeyError(f"Canonical product {source_product_id} not found")

        # 1. Merge aliases
        target_aliases = self.aliases[target_product_id]
        target_by_key = {
            (a.locale, a.context, normalize_identity_text(a.alias)): idx
            for idx, a in enumerate(target_aliases)
        }
        for src_alias in self.aliases.pop(source_product_id, []):
            key = (src_alias.locale, src_alias.context, normalize_identity_text(src_alias.alias))
            if key in target_by_key:
                idx = target_by_key[key]
                if src_alias.reviewed and not target_aliases[idx].reviewed:
                    target_aliases[idx] = target_aliases[idx].model_copy(update={"reviewed": True})
            else:
                target_by_key[key] = len(target_aliases)
                target_aliases.append(deepcopy(src_alias))

        # 2. Merge preferences & exclusions across all households
        all_hids = {
            hid for hid, pid in self.preferences if pid in (target_product_id, source_product_id)
        } | {hid for hid, pid in self.exclusions if pid in (target_product_id, source_product_id)}
        for hid in all_hids:
            tgt_excs = self.exclusions.pop((hid, target_product_id), [])
            src_excs = self.exclusions.pop((hid, source_product_id), [])
            merged_excs: dict[str, ExcludedOdaSku] = {
                e.oda_product_id: deepcopy(e) for e in tgt_excs
            }
            for e in src_excs:
                if e.oda_product_id not in merged_excs:
                    merged_excs[e.oda_product_id] = deepcopy(e)
            excl_ids = set(merged_excs.keys())

            tgt_prefs = self.preferences.pop((hid, target_product_id), [])
            src_prefs = self.preferences.pop((hid, source_product_id), [])
            merged_prefs: list[PreferredOdaSku] = []
            seen_ids: set[str] = set()
            for p in [*tgt_prefs, *src_prefs]:
                if p.oda_product_id in excl_ids or p.oda_product_id in seen_ids:
                    continue
                seen_ids.add(p.oda_product_id)
                merged_prefs.append(p.model_copy(update={"rank": len(merged_prefs) + 1}))

            if merged_prefs:
                self.preferences[(hid, target_product_id)] = merged_prefs
            if merged_excs:
                self.exclusions[(hid, target_product_id)] = list(merged_excs.values())

        # 3. Merge stock items
        for item in self.stock_items.pop(source_product_id, []):
            self.stock_items[target_product_id].append(deepcopy(item))

        # 4. Merge purchase evidence & recipe link counts
        for ev in self.purchase_evidence.pop(source_product_id, []):
            self.purchase_evidence[target_product_id].append(deepcopy(ev))
        self.recipe_links[target_product_id] += self.recipe_links.pop(source_product_id, 0)

        # 5. Merge remembered SKUs
        for rem in self.remembered_skus.pop(source_product_id, []):
            self.record_remembered_sku(
                target_product_id,
                rem.oda_product_id,
                name=rem.name,
                brand=rem.brand,
                price=rem.price,
                unit_name=rem.unit_name,
                last_selected_at=rem.last_selected_at,
                source=rem.source,
            )

        # 6. Merge previously purchased SKUs
        for ps in self.previously_purchased_skus.pop(source_product_id, []):
            self.record_previously_purchased_sku(
                target_product_id,
                ps.sku_id,
                ps.provider,
                name=ps.name,
                store_name=ps.store_name,
                ean=ps.ean,
                last_purchased_at=ps.last_purchased_at,
                provenance=ps.provenance,
            )

        # 7. Delete source product & update identity_repo if present
        self._merged_source_ids.add(source_product_id)
        self.products.pop(source_product_id, None)
        if self.identity_repo is not None:
            product_dict = getattr(self.identity_repo, "_products", None) or getattr(
                self.identity_repo, "products", None
            )
            if isinstance(product_dict, dict):
                for k in [k for k, v in product_dict.items() if v.id == source_product_id]:
                    product_dict.pop(k, None)
            alias_dict = getattr(self.identity_repo, "_aliases", None) or getattr(
                self.identity_repo, "aliases", None
            )
            if isinstance(alias_dict, dict):
                for pids in alias_dict.values():
                    if isinstance(pids, set) and source_product_id in pids:
                        pids.discard(source_product_id)
                        pids.add(target_product_id)

        return deepcopy(target), deepcopy(source)
