"""Household-scoped commands for exact catalogue-to-retailer SKU memory."""

from uuid import UUID

from home_kitchen.catalogue.service import FoodCatalogueService
from home_kitchen.catalogue.sku_models import (
    ExplicitPurchaseEvidence,
    RetailerProductOption,
    RetailerProductSnapshot,
)
from home_kitchen.catalogue.sku_repository import PostgresCatalogueSkuRepository


class CatalogueSkuService:
    """Join generic foods to explicitly remembered exact products and user facts.

    ``household_id`` is always a required argument. ``actor_id`` is audit identity;
    it never determines the household scope.
    """

    def __init__(self, foods: FoodCatalogueService, repository: PostgresCatalogueSkuRepository):
        self.foods = foods
        self.repository = repository

    def autocomplete(self, query: str, limit: int = 10):
        return self.foods.autocomplete(query, limit)

    def remember_selection(
        self,
        household_id: UUID,
        food_catalogue_id: UUID,
        snapshot: RetailerProductSnapshot,
        *,
        actor_id: str,
    ) -> RetailerProductOption:
        self._actor(actor_id)
        food = self.foods.get(food_catalogue_id)
        if food is None:
            raise KeyError("generic food does not exist")
        # No trusted retailer verification flow exists yet; callers cannot promote
        # provider claims into verified evidence by setting a model field.
        unverified = snapshot.model_copy(update={"evidence_status": "unverified"})
        return self.repository.remember_selection(household_id, food, unverified, actor_id)

    def list_options(
        self, household_id: UUID, food_catalogue_id: UUID
    ) -> list[RetailerProductOption]:
        if self.foods.get(food_catalogue_id) is None:
            raise KeyError("generic food does not exist")
        return self.repository.list_options(household_id, food_catalogue_id)

    def set_preferred(
        self, household_id: UUID, option_id: UUID, *, actor_id: str
    ) -> RetailerProductOption:
        self._actor(actor_id)
        self.repository.set_preferred(household_id, option_id, actor_id)
        return self.repository.get_option(household_id, option_id)

    def unset_preferred(
        self, household_id: UUID, option_id: UUID, *, actor_id: str
    ) -> RetailerProductOption:
        self._actor(actor_id)
        self.repository.unset_preferred(household_id, option_id, actor_id)
        return self.repository.get_option(household_id, option_id)

    def list_selections(self, household_id: UUID, option_id: UUID) -> list[dict]:
        return self.repository.list_selections(household_id, option_id)

    def list_preference_events(self, household_id: UUID, option_id: UUID) -> list[dict]:
        return self.repository.list_preference_events(household_id, option_id)

    def record_purchase(
        self,
        household_id: UUID,
        option_id: UUID,
        snapshot_id: UUID,
        evidence: ExplicitPurchaseEvidence,
        *,
        actor_id: str,
    ) -> UUID:
        self._actor(actor_id)
        return self.repository.record_purchase(
            household_id, option_id, snapshot_id, evidence, actor_id
        )

    def list_purchases(self, household_id: UUID, option_id: UUID) -> list[dict]:
        return self.repository.list_purchases(household_id, option_id)

    @staticmethod
    def _actor(actor_id: str) -> None:
        if not actor_id.strip():
            raise ValueError("actor_id is required for an audited mutation")
