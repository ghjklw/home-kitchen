"""Domain service for the Product Overview read model and Oda preferences."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from home_kitchen.catalogue.identity import CanonicalProduct, normalize_identity_text
from home_kitchen.catalogue.overview_models import (
    ExcludedOdaSku,
    PreferredOdaSku,
    PreferredOdaSkuInput,
    ProductAliasItem,
    ProductOverview,
    ProductPreferencesUpdate,
    ProductStockUpdate,
    ProvenanceCounts,
    PurchaseEvidenceRecord,
    PurchaseFrequencyOverview,
)
from home_kitchen.catalogue.overview_repository import ProductOverviewRepository
from home_kitchen.units import compatible_units, normalize_unit

if TYPE_CHECKING:
    from home_kitchen.basket.matching import BasketCandidate
    from home_kitchen.catalogue.identity_service import CanonicalIdentityService
    from home_kitchen.catalogue.sku_service import CatalogueSkuService
    from home_kitchen.history import HistoryRepository
    from home_kitchen.inventory import InventoryService

DEFAULT_HOUSEHOLD_ID = UUID("00000000-0000-0000-0000-000000000001")


def compute_purchase_frequency(
    evidence_list: list[PurchaseEvidenceRecord],
) -> PurchaseFrequencyOverview:
    provider_verified = sum(1 for e in evidence_list if e.provenance == "provider_verified")
    user_confirmed = sum(1 for e in evidence_list if e.provenance == "user_confirmed")
    uncertain = sum(1 for e in evidence_list if e.provenance == "uncertain")
    counts = ProvenanceCounts(
        provider_verified=provider_verified,
        user_confirmed=user_confirmed,
        uncertain=uncertain,
    )

    confirmed = [
        e for e in evidence_list if e.provenance in ("provider_verified", "user_confirmed")
    ]
    confirmed.sort(key=lambda e: e.purchased_at)
    sample_size = len(confirmed)

    if sample_size < 2:
        return PurchaseFrequencyOverview(
            status="unknown",
            average_interval_days=None,
            sample_size=sample_size,
            first_purchased_at=confirmed[0].purchased_at if confirmed else None,
            last_purchased_at=confirmed[-1].purchased_at if confirmed else None,
            date_range_days=0 if confirmed else None,
            frequency_display="unknown",
            counts_by_provenance=counts,
            evidence_history=evidence_list,
        )

    first_at = confirmed[0].purchased_at
    last_at = confirmed[-1].purchased_at
    date_range_days = (last_at.date() - first_at.date()).days

    if date_range_days <= 0:
        return PurchaseFrequencyOverview(
            status="unknown",
            average_interval_days=None,
            sample_size=sample_size,
            first_purchased_at=first_at,
            last_purchased_at=last_at,
            date_range_days=0,
            frequency_display="unknown",
            counts_by_provenance=counts,
            evidence_history=evidence_list,
        )

    intervals = sample_size - 1
    avg_days = round(date_range_days / intervals, 1)
    display = f"every {avg_days:g} days" if avg_days != 1 else "every 1 day"

    return PurchaseFrequencyOverview(
        status="known",
        average_interval_days=avg_days,
        sample_size=sample_size,
        first_purchased_at=first_at,
        last_purchased_at=last_at,
        date_range_days=date_range_days,
        frequency_display=display,
        counts_by_provenance=counts,
        evidence_history=evidence_list,
    )


def resolve_display_name(
    product: CanonicalProduct,
    aliases: list[ProductAliasItem],
    locale: str = "nb",
) -> str:
    locale_norm = locale.casefold().strip()
    matching = [a for a in aliases if a.locale.casefold().strip() == locale_norm]
    best_alias: str | None = None
    if matching:

        def alias_score(a: ProductAliasItem) -> int:
            score = 0
            if a.reviewed:
                score += 10
            if a.context.casefold() in ("recipe", ""):
                score += 5
            return score

        matching_sorted = sorted(matching, key=alias_score, reverse=True)
        best_alias = matching_sorted[0].alias

    base_name = best_alias or product.canonical_name
    if product.variant and product.variant.casefold() not in base_name.casefold():
        return f"{base_name} ({product.variant})"
    return base_name


def group_translations_by_locale(
    aliases: list[ProductAliasItem],
) -> dict[str, list[str]]:
    translations: dict[str, list[str]] = defaultdict(list)
    for a in aliases:
        loc = a.locale.casefold().strip()
        if a.alias not in translations[loc]:
            translations[loc].append(a.alias)
    return dict(translations)


class ProductOverviewService:
    def __init__(
        self,
        repository: ProductOverviewRepository,
        identities: CanonicalIdentityService | None = None,
        inventory: InventoryService | None = None,
        history: HistoryRepository | None = None,
        catalogue_skus: CatalogueSkuService | None = None,
        household_id: UUID | None = None,
    ):
        self.repository = repository
        self.identities = identities
        self.inventory = inventory
        self.history = history
        self.catalogue_skus = catalogue_skus
        self.household_id = household_id or DEFAULT_HOUSEHOLD_ID

    def _build_product_overview(
        self,
        prod: CanonicalProduct,
        prod_aliases: list[ProductAliasItem],
        preferred: list[PreferredOdaSku],
        excluded: list[ExcludedOdaSku],
        household_id: UUID,
        locale: str,
    ) -> ProductOverview:
        stock = self.repository.get_stock_for_product(prod, prod_aliases)
        purchase_evidence = self.repository.get_purchase_evidence_for_product(
            prod, prod_aliases, preferred, household_id
        )
        remembered = self.repository.get_remembered_skus_for_product(
            prod, prod_aliases, household_id
        )
        previously_purchased = self.repository.get_previously_purchased_skus_for_product(
            prod, prod_aliases, preferred, household_id
        )
        freq = compute_purchase_frequency(purchase_evidence)
        display_name = resolve_display_name(prod, prod_aliases, locale=locale)
        translations = group_translations_by_locale(prod_aliases)

        return ProductOverview(
            id=prod.id,
            canonical_name=prod.canonical_name,
            variant=prod.variant,
            display_name=display_name,
            locale=locale,
            aliases=prod_aliases,
            translations_by_locale=translations,
            preferred_oda_skus=preferred,
            excluded_oda_skus=excluded,
            remembered_oda_skus=remembered,
            previously_purchased_skus=previously_purchased,
            stock=stock,
            purchase_frequency=freq,
        )

    def get_overview(
        self,
        household_id: UUID | None = None,
        query: str | None = None,
        locale: str = "nb",
    ) -> list[ProductOverview]:
        hid = household_id or self.household_id
        products = self.repository.list_products(query=query)
        if not products:
            return []

        product_ids = [p.id for p in products]
        aliases_map = self.repository.get_aliases_for_products(product_ids)
        prefs_map = self.repository.get_all_preferences(hid)

        overview_list: list[ProductOverview] = []
        for prod in products:
            prod_aliases = aliases_map.get(prod.id, [])
            preferred, excluded = prefs_map.get(prod.id, ([], []))
            overview_list.append(
                self._build_product_overview(prod, prod_aliases, preferred, excluded, hid, locale)
            )

        if query:
            q_norm = normalize_identity_text(query)
            filtered = []
            for item in overview_list:
                candidates = {
                    normalize_identity_text(item.canonical_name),
                    normalize_identity_text(item.variant),
                    normalize_identity_text(item.display_name),
                }
                for a in item.aliases:
                    candidates.add(normalize_identity_text(a.alias))
                if any(q_norm in c for c in candidates if c):
                    filtered.append(item)
            return filtered

        return overview_list

    def get_product_overview(
        self,
        product_id: UUID,
        household_id: UUID | None = None,
        locale: str = "nb",
    ) -> ProductOverview:
        hid = household_id or self.household_id
        prod = self.repository.get_product(product_id)
        if prod is None:
            raise KeyError(f"Canonical product {product_id} not found")

        aliases_map = self.repository.get_aliases_for_products([product_id])
        prod_aliases = aliases_map.get(product_id, [])
        preferred, excluded = self.repository.get_preferences(hid, product_id)
        return self._build_product_overview(prod, prod_aliases, preferred, excluded, hid, locale)

    def update_stock(
        self,
        product_id: UUID,
        stock_update: ProductStockUpdate,
        household_id: UUID | None = None,
        locale: str = "nb",
    ) -> ProductOverview:
        hid = household_id or self.household_id
        prod = self.repository.get_product(product_id)
        if prod is None:
            raise KeyError(f"Canonical product {product_id} not found")

        aliases_map = self.repository.get_aliases_for_products([product_id])
        prod_aliases = aliases_map.get(product_id, [])

        if self.inventory is not None:
            from home_kitchen.inventory import InventoryItemCreate

            existing = self.repository.get_stock_for_product(prod, prod_aliases)
            target_name = prod.canonical_name
            target_category = stock_update.category
            if stock_update.inventory_id is not None:
                matched_item = next(
                    (i for i in existing.items if i.inventory_id == stock_update.inventory_id),
                    None,
                )
                if matched_item is not None:
                    target_name = matched_item.name
                    if stock_update.category == "Other" and matched_item.category != "Other":
                        target_category = matched_item.category
                    if normalize_unit(matched_item.unit) not in compatible_units(stock_update.unit):
                        self.inventory.remove(stock_update.inventory_id)
            elif existing.items:
                target_name = existing.items[0].name
                if stock_update.category == "Other" and existing.items[0].category != "Other":
                    target_category = existing.items[0].category
                if len(existing.items) == 1 and normalize_unit(
                    existing.items[0].unit
                ) not in compatible_units(stock_update.unit):
                    if existing.items[0].inventory_id is not None:
                        self.inventory.remove(existing.items[0].inventory_id)
            else:
                nb_aliases = [
                    a
                    for a in prod_aliases
                    if a.locale.casefold().strip() == "nb" and a.alias.strip()
                ]
                if nb_aliases:
                    nb_aliases.sort(key=lambda a: (not a.reviewed, a.context != "recipe"))
                    target_name = nb_aliases[0].alias
                elif prod.variant:
                    target_name = f"{prod.canonical_name} {prod.variant}"

            self.inventory.upsert(
                InventoryItemCreate(
                    name=target_name,
                    unit=stock_update.unit,
                    quantity=stock_update.quantity,
                    category=target_category,
                    note=stock_update.note,
                )
            )
        else:
            self.repository.upsert_stock_for_product(prod, prod_aliases, stock_update)

        return self.get_product_overview(product_id, hid, locale=locale)

    def merge_products(
        self,
        target_product_id: UUID,
        source_product_id: UUID,
        household_id: UUID | None = None,
        actor_id: str = "",
        locale: str = "nb",
    ) -> ProductOverview:
        hid = household_id or self.household_id
        target, source = self.repository.merge_products(
            target_product_id, source_product_id, hid, actor_id=actor_id
        )
        if self.history is not None:
            engine = getattr(self.history, "engine", None)
            if engine is not None and engine.dialect.name == "postgresql":
                from home_kitchen.context import history_context
                from home_kitchen.history import HistoryEntryCreate

                self.history.record(
                    HistoryEntryCreate(
                        channel=history_context.get().channel,
                        kind="domain_event",
                        message=(
                            f"Merged canonical product {source.canonical_name} "
                            f"into {target.canonical_name}"
                        ),
                        structured={
                            "action": "canonical_product_merged",
                            "source_product_id": str(source.id),
                            "target_product_id": str(target.id),
                            "source_canonical_name": source.canonical_name,
                            "target_canonical_name": target.canonical_name,
                        },
                    ),
                    actor_id=actor_id or None,
                )
        return self.get_product_overview(target_product_id, hid, locale=locale)

    def get_preferences(
        self,
        product_id: UUID,
        household_id: UUID | None = None,
    ) -> tuple[list[PreferredOdaSku], list[ExcludedOdaSku]]:
        hid = household_id or self.household_id
        prod = self.repository.get_product(product_id)
        if prod is None:
            raise KeyError(f"Canonical product {product_id} not found")
        return self.repository.get_preferences(hid, product_id)

    def update_preferences(
        self,
        product_id: UUID,
        update: ProductPreferencesUpdate,
        household_id: UUID | None = None,
        actor_id: str = "",
    ) -> ProductOverview:
        hid = household_id or self.household_id
        prod = self.repository.get_product(product_id)
        if prod is None:
            raise KeyError(f"Canonical product {product_id} not found")

        # Process exclusions first
        excluded_skus: list[ExcludedOdaSku] = []
        excluded_ids: set[str] = set()
        for item in update.excluded_oda_skus:
            if isinstance(item, str):
                sku_id = item.strip()
                reason = ""
                name = None
            else:
                sku_id = item.oda_product_id.strip()
                reason = item.reason
                name = item.name
            if sku_id and sku_id not in excluded_ids:
                excluded_ids.add(sku_id)
                excluded_skus.append(
                    ExcludedOdaSku(
                        oda_product_id=sku_id,
                        name=name,
                        reason=reason,
                        excluded_at=datetime.now(UTC),
                    )
                )

        # Process preferred SKUs; exclusions strictly take precedence
        preferred_skus: list[PreferredOdaSku] = []
        seen_pref_ids: set[str] = set()
        current_rank = 1
        for item in update.preferred_oda_skus:
            if isinstance(item, str):
                sku_id = item.strip()
                pref_in = PreferredOdaSkuInput(oda_product_id=sku_id)
            else:
                sku_id = item.oda_product_id.strip()
                pref_in = item
            if not sku_id or sku_id in excluded_ids or sku_id in seen_pref_ids:
                continue
            seen_pref_ids.add(sku_id)
            preferred_skus.append(
                PreferredOdaSku(
                    oda_product_id=sku_id,
                    rank=current_rank,
                    name=pref_in.name,
                    brand=pref_in.brand,
                    price=pref_in.price,
                    unit_price=pref_in.unit_price,
                    unit_name=pref_in.unit_name,
                    url=pref_in.url,
                    image_url=pref_in.image_url,
                )
            )
            current_rank += 1

        self.repository.set_preferences(
            hid, product_id, preferred_skus, excluded_skus, actor_id=actor_id
        )

        return self.get_product_overview(product_id, hid)

    def suggest_preferred_sku(
        self,
        product_id: UUID,
        available_sku_ids: list[str] | None = None,
        household_id: UUID | None = None,
    ) -> tuple[PreferredOdaSku | None, str | None]:
        hid = household_id or self.household_id
        preferred, excluded = self.repository.get_preferences(hid, product_id)
        excluded_ids = {e.oda_product_id for e in excluded}

        for pref in sorted(preferred, key=lambda p: p.rank):
            if pref.oda_product_id in excluded_ids:
                continue
            if available_sku_ids is not None and pref.oda_product_id not in available_sku_ids:
                continue
            reason = (
                f"Suggested from household preferred Oda SKU preference (rank {pref.rank}); "
                f"fresh availability review and explicit selection required."
            )
            return pref, reason
        return None, None

    def filter_and_rank_candidates(
        self,
        product_id: UUID,
        candidates: list[BasketCandidate],
        household_id: UUID | None = None,
    ) -> list[BasketCandidate]:
        hid = household_id or self.household_id
        preferred, excluded = self.repository.get_preferences(hid, product_id)
        excluded_ids = {e.oda_product_id for e in excluded}
        pref_by_id = {p.oda_product_id: p for p in preferred}

        eligible_candidates: list[tuple[tuple[int, int], BasketCandidate]] = []
        for index, candidate in enumerate(candidates):
            if candidate.product_id in excluded_ids:
                continue
            pref = pref_by_id.get(candidate.product_id)
            if pref is not None:
                updated = candidate.model_copy(
                    update={
                        "is_preferred": True,
                        "preference_rank": pref.rank,
                        "match_reason": (
                            f"Suggested from household preferred Oda SKU (rank {pref.rank}); "
                            f"{candidate.match_reason}"
                        ),
                    }
                )
                eligible_candidates.append(((0, pref.rank), updated))
            else:
                eligible_candidates.append(((1, index), candidate))

        eligible_candidates.sort(key=lambda pair: pair[0])
        return [c for _, c in eligible_candidates]
