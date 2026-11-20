"""Persistence operations for canonical identities and contextual aliases."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.identity import (
    CanonicalProduct,
    CanonicalUnit,
    IdentityCorrectionEvent,
    normalize_identity_text,
)


class PostgresIdentityRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def product(
        self, canonical_name: str, variant: str, identity_id: UUID | None = None
    ) -> CanonicalProduct:
        target_id = identity_id or uuid4()
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""INSERT INTO kitchen_canonical_products (id, canonical_name, variant)
                    VALUES (:id, :name, :variant)
                    ON CONFLICT (canonical_name, variant) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
                    RETURNING id, canonical_name, variant"""),
                    {"id": target_id, "name": canonical_name, "variant": variant},
                )
                .mappings()
                .one()
            )
        return CanonicalProduct.model_validate(dict(row))

    def unit(self, canonical_name: str, identity_id: UUID | None = None) -> CanonicalUnit:
        target_id = identity_id or uuid4()
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""INSERT INTO kitchen_canonical_units (id, canonical_name)
                    VALUES (:id, :name)
                    ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
                    RETURNING id, canonical_name"""),
                    {"id": target_id, "name": canonical_name},
                )
                .mappings()
                .one()
            )
        return CanonicalUnit.model_validate(dict(row))

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        table, fk = self._table(kind)
        with self.engine.begin() as connection:
            self._require_identity(connection, kind, identity_id)
            connection.execute(
                text(f"""INSERT INTO {table} (locale, context, normalized_alias, alias, {fk}, reviewed)
                    SELECT :locale, :context, :normalized, :alias, :identity_id, false
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table}
                        WHERE locale=:locale AND context=:context AND normalized_alias=:normalized AND reviewed=true
                    )
                    ON CONFLICT DO NOTHING"""),
                {
                    "locale": locale,
                    "context": context,
                    "normalized": normalize_identity_text(alias),
                    "alias": alias,
                    "identity_id": identity_id,
                },
            )

    def has_reviewed_alias(self, kind: str, locale: str, context: str, alias: str) -> bool:
        table, _ = self._table(kind)
        with self.engine.connect() as connection:
            row = connection.execute(
                text(f"""SELECT 1 FROM {table}
                WHERE locale=:locale AND context IN (:context, '') AND normalized_alias=:normalized AND reviewed=true
                LIMIT 1"""),
                {
                    "locale": normalize_identity_text(locale),
                    "context": normalize_identity_text(context),
                    "normalized": normalize_identity_text(alias),
                },
            ).first()
            return row is not None

    def correct_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        actor_id: str = "system",
        household_id: UUID | None = None,
        channel: str = "api",
        source_type: str = "reviewed_correction",
    ) -> IdentityCorrectionEvent | None:
        table, fk = self._table(kind)
        normalized = normalize_identity_text(alias)
        with self.engine.begin() as connection:
            self._require_identity(connection, kind, identity_id)
            existing = (
                connection.execute(
                    text(
                        f"SELECT {fk} AS identity_id, reviewed FROM {table} "
                        "WHERE locale=:locale AND context=:context AND normalized_alias=:normalized "
                        "LIMIT 1"
                    ),
                    {"locale": locale, "context": context, "normalized": normalized},
                )
                .mappings()
                .first()
            )
            previous_identity_id = (
                UUID(str(existing["identity_id"]))
                if existing is not None and existing["identity_id"] is not None
                else None
            )
            previous_reviewed = bool(existing["reviewed"]) if existing is not None else None
            if previous_identity_id == identity_id and previous_reviewed is True:
                return None

            connection.execute(
                text(
                    f"DELETE FROM {table} WHERE locale=:locale AND context=:context AND normalized_alias=:normalized"
                ),
                {
                    "locale": locale,
                    "context": context,
                    "normalized": normalized,
                },
            )
            connection.execute(
                text(f"""INSERT INTO {table} (locale, context, normalized_alias, alias, {fk}, reviewed)
                    VALUES (:locale, :context, :normalized, :alias, :identity_id, true)"""),
                {
                    "locale": locale,
                    "context": context,
                    "normalized": normalized,
                    "alias": alias,
                    "identity_id": identity_id,
                },
            )
            now = datetime.now(UTC)
            event = IdentityCorrectionEvent(
                id=uuid4(),
                occurred_at=now,
                household_id=household_id,
                actor_id=actor_id,
                channel=channel,  # ty: ignore[invalid-argument-type]
                kind=kind,  # ty: ignore[invalid-argument-type]
                locale=locale,
                context=context,
                normalized_alias=normalized,
                alias=alias,
                previous_identity_id=previous_identity_id,
                new_identity_id=identity_id,
                previous_reviewed=previous_reviewed,
                new_reviewed=True,
                source_type=source_type,  # ty: ignore[invalid-argument-type]
            )
            if inspect(connection).has_table("kitchen_identity_correction_events"):
                connection.execute(
                    text(
                        """INSERT INTO kitchen_identity_correction_events
                        (id, occurred_at, household_id, actor_id, channel, kind, locale, context,
                         normalized_alias, alias, previous_identity_id, new_identity_id,
                         previous_reviewed, new_reviewed, source_type)
                        VALUES
                        (:id, :occurred_at, :household_id, :actor_id, :channel, :kind, :locale, :context,
                         :normalized_alias, :alias, :previous_identity_id, :new_identity_id,
                         :previous_reviewed, :new_reviewed, :source_type)"""
                    ),
                    event.model_dump(),
                )
            return event

    def list_correction_events(
        self,
        *,
        kind: str | None = None,
        alias: str | None = None,
        household_id: UUID | None = None,
        limit: int = 50,
    ) -> list[IdentityCorrectionEvent]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self.engine.connect() as connection:
            if not inspect(connection).has_table("kitchen_identity_correction_events"):
                return []
            clauses: list[str] = []
            params: dict[str, object] = {"limit": limit}
            if kind is not None:
                self._table(kind)
                clauses.append("kind = :kind")
                params["kind"] = kind
            if alias is not None and alias.strip():
                clauses.append("normalized_alias = :normalized_alias")
                params["normalized_alias"] = normalize_identity_text(alias)
            if household_id is not None:
                clauses.append("household_id = :household_id")
                params["household_id"] = household_id
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = (
                connection.execute(
                    text(
                        "SELECT id, occurred_at, household_id, actor_id, channel, kind, locale, context, "
                        "normalized_alias, alias, previous_identity_id, new_identity_id, "
                        "previous_reviewed, new_reviewed, source_type "
                        f"FROM kitchen_identity_correction_events {where} "
                        "ORDER BY occurred_at DESC, id DESC LIMIT :limit"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            return [IdentityCorrectionEvent.model_validate(dict(r)) for r in rows]

    @staticmethod
    def _require_identity(connection, kind: str, identity_id: UUID) -> None:
        PostgresIdentityRepository._table(kind)
        identity_table = (
            "kitchen_canonical_products" if kind == "product" else "kitchen_canonical_units"
        )
        exists = connection.execute(
            text(f"SELECT 1 FROM {identity_table} WHERE id=:identity_id"),
            {"identity_id": identity_id},
        ).first()
        if exists is None:
            raise ValueError(f"Unknown canonical {kind} identity")

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        table, fk = self._table(kind)
        identity_table = (
            "kitchen_canonical_products" if kind == "product" else "kitchen_canonical_units"
        )
        columns = (
            "p.id, p.canonical_name, p.variant" if kind == "product" else "p.id, p.canonical_name"
        )
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(f"""SELECT DISTINCT {columns} FROM {table} a
                    JOIN {identity_table} p ON p.id = a.{fk}
                    WHERE a.locale=:locale AND a.context IN (:context, '') AND a.normalized_alias=:normalized
                    ORDER BY p.canonical_name, p.id"""),
                    {
                        "locale": locale,
                        "context": context,
                        "normalized": normalize_identity_text(alias),
                    },
                )
                .mappings()
                .all()
            )
        model = CanonicalProduct if kind == "product" else CanonicalUnit
        return [model.model_validate(dict(row)) for row in rows]

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        if not ids:
            return
        self._table(kind)
        identity_table = (
            "kitchen_canonical_products" if kind == "product" else "kitchen_canonical_units"
        )
        with self.engine.connect() as connection:
            existing = set(
                connection.execute(
                    text(f"SELECT id FROM {identity_table} WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": list(ids)},
                ).scalars()
            )
        if existing != ids:
            raise ValueError(f"Recipe contains an unknown canonical {kind} identity")

    @staticmethod
    def _table(kind: str) -> tuple[str, str]:
        if kind == "product":
            return "kitchen_product_aliases", "product_id"
        if kind == "unit":
            return "kitchen_unit_aliases", "unit_id"
        raise ValueError("kind must be product or unit")
