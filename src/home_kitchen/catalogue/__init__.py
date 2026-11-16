"""Shared, source-attributed reference foods for household grocery workflows."""

from home_kitchen.catalogue.models import FoodCatalogueItem
from home_kitchen.catalogue.overview_models import (
    ExcludedOdaSku,
    ExcludedOdaSkuInput,
    PreferredOdaSku,
    PreferredOdaSkuInput,
    ProductAliasItem,
    ProductOverview,
    ProductPreferencesUpdate,
    ProductStockOverview,
    PurchaseEvidenceRecord,
    PurchaseFrequencyOverview,
    StockItemDetail,
)
from home_kitchen.catalogue.overview_repository import (
    MemoryProductOverviewRepository,
    PostgresProductOverviewRepository,
    ProductOverviewRepository,
)
from home_kitchen.catalogue.overview_service import (
    ProductOverviewService,
    compute_purchase_frequency,
    resolve_display_name,
)
from home_kitchen.catalogue.repository import PostgresFoodCatalogueRepository
from home_kitchen.catalogue.service import FoodCatalogueService
from home_kitchen.catalogue.sku_models import (
    ExplicitPurchaseEvidence,
    RetailerProductOption,
    RetailerProductSnapshot,
)
from home_kitchen.catalogue.sku_repository import PostgresCatalogueSkuRepository
from home_kitchen.catalogue.sku_service import CatalogueSkuService

__all__ = [
    "CatalogueSkuService",
    "ExcludedOdaSku",
    "ExcludedOdaSkuInput",
    "ExplicitPurchaseEvidence",
    "FoodCatalogueItem",
    "FoodCatalogueService",
    "MemoryProductOverviewRepository",
    "PostgresCatalogueSkuRepository",
    "PostgresFoodCatalogueRepository",
    "PostgresProductOverviewRepository",
    "PreferredOdaSku",
    "PreferredOdaSkuInput",
    "ProductAliasItem",
    "ProductOverview",
    "ProductOverviewRepository",
    "ProductOverviewService",
    "ProductPreferencesUpdate",
    "ProductStockOverview",
    "PurchaseEvidenceRecord",
    "PurchaseFrequencyOverview",
    "RetailerProductOption",
    "RetailerProductSnapshot",
    "StockItemDetail",
    "compute_purchase_frequency",
    "resolve_display_name",
]
