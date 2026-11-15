"""PostgreSQL persistence for exact household catalogue SKU memory."""

import json
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.models import FoodCatalogueItem
from home_kitchen.catalogue.sku_models import (
    ExplicitPurchaseEvidence,
    RetailerProductOption,
    RetailerProductSnapshot,
)


class PostgresCatalogueSkuRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def remember_selection(
        self,
        household_id: UUID,
        food: FoodCatalogueItem,
        snapshot: RetailerProductSnapshot,
        actor_id: str,
    ) -> RetailerProductOption:
        option_id, snapshot_id = uuid4(), uuid4()
        snapshot_data = snapshot.model_dump(mode="json", exclude={"snapshot_ref"})
        snapshot_data["evidence_status"] = "unverified"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_options "
                    "(id,household_id,food_catalogue_id,provider,product_id) "
                    "VALUES (:id,:household_id,:food_id,:provider,:product_id) "
                    "ON CONFLICT (household_id,food_catalogue_id,provider,product_id) "
                    "DO NOTHING"
                ),
                {
                    "id": option_id,
                    "household_id": household_id,
                    "food_id": food.id,
                    "provider": snapshot.provider,
                    "product_id": snapshot.product_id,
                },
            )
            option = (
                connection.execute(
                    text(
                        "SELECT id FROM kitchen_catalogue_sku_options "
                        "WHERE household_id=:household_id AND food_catalogue_id=:food_id "
                        "AND provider=:provider AND product_id=:product_id FOR UPDATE"
                    ),
                    {
                        "household_id": household_id,
                        "food_id": food.id,
                        "provider": snapshot.provider,
                        "product_id": snapshot.product_id,
                    },
                )
                .mappings()
                .one()
            )
            actual_option_id = option["id"]
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_snapshots "
                    "(id,household_id,option_id,snapshot,snapshot_ref,observed_at,evidence_status) "
                    "VALUES (:id,:household_id,:option_id,CAST(:snapshot AS jsonb),:snapshot_ref,:observed_at,:status)"
                ),
                {
                    "id": snapshot_id,
                    "household_id": household_id,
                    "option_id": actual_option_id,
                    "snapshot": json.dumps(snapshot_data, ensure_ascii=False),
                    "snapshot_ref": snapshot.snapshot_ref,
                    "observed_at": snapshot.observed_at,
                    "status": "unverified",
                },
            )
            connection.execute(
                text(
                    "UPDATE kitchen_catalogue_sku_options SET current_snapshot_id=:snapshot_id "
                    "WHERE id=:option_id AND household_id=:household_id"
                ),
                {
                    "snapshot_id": snapshot_id,
                    "option_id": actual_option_id,
                    "household_id": household_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_selections "
                    "(id,household_id,option_id,snapshot_id,actor_id) "
                    "VALUES (:id,:household_id,:option_id,:snapshot_id,:actor_id)"
                ),
                {
                    "id": uuid4(),
                    "household_id": household_id,
                    "option_id": actual_option_id,
                    "snapshot_id": snapshot_id,
                    "actor_id": actor_id,
                },
            )
        return self.get_option(household_id, actual_option_id)

    def get_option(self, household_id: UUID, option_id: UUID) -> RetailerProductOption:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT o.id,s.id AS snapshot_id,o.household_id,o.food_catalogue_id,o.provider,o.product_id, "
                        "s.snapshot,s.snapshot_ref,s.observed_at,s.evidence_status, "
                        "(p.option_id IS NOT NULL) AS preferred "
                        "FROM kitchen_catalogue_sku_options o "
                        "JOIN kitchen_catalogue_sku_snapshots s ON s.id=o.current_snapshot_id "
                        "AND s.household_id=o.household_id AND s.option_id=o.id "
                        "LEFT JOIN kitchen_catalogue_sku_preferences p ON p.option_id=o.id "
                        "AND p.household_id=o.household_id "
                        "WHERE o.id=:option_id AND o.household_id=:household_id"
                    ),
                    {"option_id": option_id, "household_id": household_id},
                )
                .mappings()
                .one()
            )
        return self._option_from_row(row)

    def list_options(
        self, household_id: UUID, food_catalogue_id: UUID
    ) -> list[RetailerProductOption]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT o.id,s.id AS snapshot_id,o.household_id,o.food_catalogue_id,o.provider,o.product_id, "
                        "s.snapshot,s.snapshot_ref,s.observed_at,s.evidence_status, "
                        "(p.option_id IS NOT NULL) AS preferred "
                        "FROM kitchen_catalogue_sku_options o "
                        "JOIN kitchen_catalogue_sku_snapshots s ON s.id=o.current_snapshot_id "
                        "AND s.household_id=o.household_id AND s.option_id=o.id "
                        "LEFT JOIN kitchen_catalogue_sku_preferences p ON p.option_id=o.id "
                        "AND p.household_id=o.household_id "
                        "WHERE o.household_id=:household_id AND o.food_catalogue_id=:food_id "
                        "ORDER BY p.option_id DESC NULLS LAST, lower(s.snapshot->>'name'), o.provider, o.product_id"
                    ),
                    {"household_id": household_id, "food_id": food_catalogue_id},
                )
                .mappings()
                .all()
            )
        return [self._option_from_row(row) for row in rows]

    def set_preferred(self, household_id: UUID, option_id: UUID, actor_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_preferences (household_id,option_id,actor_id) "
                    "SELECT household_id,id,:actor_id FROM kitchen_catalogue_sku_options "
                    "WHERE household_id=:household_id AND id=:option_id "
                    "ON CONFLICT (household_id,option_id) DO UPDATE "
                    "SET actor_id=EXCLUDED.actor_id,set_at=now()"
                ),
                {"household_id": household_id, "option_id": option_id, "actor_id": actor_id},
            )
            self._require_option(connection, household_id, option_id)
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_preference_events "
                    "(id,household_id,option_id,action,actor_id) "
                    "VALUES (:id,:household_id,:option_id,'set',:actor_id)"
                ),
                {
                    "id": uuid4(),
                    "household_id": household_id,
                    "option_id": option_id,
                    "actor_id": actor_id,
                },
            )

    def unset_preferred(self, household_id: UUID, option_id: UUID, actor_id: str) -> None:
        with self.engine.begin() as connection:
            self._require_option(connection, household_id, option_id)
            connection.execute(
                text(
                    "DELETE FROM kitchen_catalogue_sku_preferences "
                    "WHERE household_id=:household_id AND option_id=:option_id"
                ),
                {"household_id": household_id, "option_id": option_id},
            )
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_preference_events "
                    "(id,household_id,option_id,action,actor_id) "
                    "VALUES (:id,:household_id,:option_id,'unset',:actor_id)"
                ),
                {
                    "id": uuid4(),
                    "household_id": household_id,
                    "option_id": option_id,
                    "actor_id": actor_id,
                },
            )

    def list_preference_events(self, household_id: UUID, option_id: UUID) -> list[dict]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id,household_id,option_id,action,actor_id,occurred_at "
                        "FROM kitchen_catalogue_sku_preference_events WHERE household_id=:household_id "
                        "AND option_id=:option_id ORDER BY occurred_at,id"
                    ),
                    {"household_id": household_id, "option_id": option_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def list_selections(self, household_id: UUID, option_id: UUID) -> list[dict]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id,household_id,option_id,snapshot_id,actor_id,selected_at "
                        "FROM kitchen_catalogue_sku_selections WHERE household_id=:household_id "
                        "AND option_id=:option_id ORDER BY selected_at,id"
                    ),
                    {"household_id": household_id, "option_id": option_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def record_purchase(
        self,
        household_id: UUID,
        option_id: UUID,
        snapshot_id: UUID,
        evidence: ExplicitPurchaseEvidence,
        actor_id: str,
    ) -> UUID:
        purchase_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO kitchen_catalogue_sku_purchases "
                    "(id,household_id,option_id,snapshot_id,evidence_kind,evidence_ref,purchased_at,quantity,quantity_unit,actor_id) "
                    "SELECT :id,:household_id,:option_id,:snapshot_id,:kind,:ref,:purchased_at,:quantity,:unit,:actor_id "
                    "WHERE EXISTS (SELECT 1 FROM kitchen_catalogue_sku_options "
                    "WHERE id=:option_id AND household_id=:household_id)"
                ),
                {
                    "id": purchase_id,
                    "household_id": household_id,
                    "option_id": option_id,
                    "snapshot_id": snapshot_id,
                    "kind": evidence.evidence_kind,
                    "ref": evidence.evidence_ref,
                    "purchased_at": evidence.purchased_at,
                    "quantity": evidence.quantity,
                    "unit": evidence.quantity_unit,
                    "actor_id": actor_id,
                },
            )
            exists = connection.execute(
                text("SELECT 1 FROM kitchen_catalogue_sku_purchases WHERE id=:id"),
                {"id": purchase_id},
            ).scalar_one_or_none()
            if exists is None:
                raise KeyError("SKU option does not exist in this household")
        return purchase_id

    def list_purchases(self, household_id: UUID, option_id: UUID) -> list[dict]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT id,household_id,option_id,snapshot_id,evidence_kind,evidence_ref, "
                        "purchased_at,quantity,quantity_unit,actor_id,created_at "
                        "FROM kitchen_catalogue_sku_purchases WHERE household_id=:household_id "
                        "AND option_id=:option_id ORDER BY purchased_at,id"
                    ),
                    {"household_id": household_id, "option_id": option_id},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    @staticmethod
    def _require_option(connection, household_id: UUID, option_id: UUID) -> None:
        exists = connection.execute(
            text(
                "SELECT 1 FROM kitchen_catalogue_sku_options "
                "WHERE household_id=:household_id AND id=:option_id"
            ),
            {"household_id": household_id, "option_id": option_id},
        ).scalar_one_or_none()
        if exists is None:
            raise KeyError("SKU option does not exist in this household")

    @staticmethod
    def _option_from_row(row) -> RetailerProductOption:
        payload = dict(row["snapshot"])
        payload["snapshot_ref"] = row["snapshot_ref"]
        payload["observed_at"] = row["observed_at"]
        payload["evidence_status"] = row["evidence_status"]
        return RetailerProductOption(
            id=row["id"],
            snapshot_id=row["snapshot_id"],
            household_id=row["household_id"],
            food_catalogue_id=row["food_catalogue_id"],
            provider=row["provider"],
            product_id=row["product_id"],
            snapshot=RetailerProductSnapshot.model_validate(payload),
            preferred=row["preferred"],
        )
