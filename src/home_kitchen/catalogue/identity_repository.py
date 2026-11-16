"""Persistence operations for canonical identities and contextual aliases."""

from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit, normalize_identity_text


class PostgresIdentityRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def product(self, canonical_name: str, variant: str) -> CanonicalProduct:
        identity_id = uuid4()
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""INSERT INTO kitchen_canonical_products (id, canonical_name, variant)
                    VALUES (:id, :name, :variant)
                    ON CONFLICT (canonical_name, variant) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
                    RETURNING id, canonical_name, variant"""),
                    {"id": identity_id, "name": canonical_name, "variant": variant},
                )
                .mappings()
                .one()
            )
        return CanonicalProduct.model_validate(dict(row))

    def unit(self, canonical_name: str) -> CanonicalUnit:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("""INSERT INTO kitchen_canonical_units (id, canonical_name)
                    VALUES (:id, :name)
                    ON CONFLICT (canonical_name) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
                    RETURNING id, canonical_name"""),
                    {"id": uuid4(), "name": canonical_name},
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
                    VALUES (:locale, :context, :normalized, :alias, :identity_id, false)
                    ON CONFLICT DO NOTHING"""),
                {
                    "locale": locale,
                    "context": context,
                    "normalized": normalize_identity_text(alias),
                    "alias": alias,
                    "identity_id": identity_id,
                },
            )

    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        table, fk = self._table(kind)
        with self.engine.begin() as connection:
            self._require_identity(connection, kind, identity_id)
            connection.execute(
                text(
                    f"DELETE FROM {table} WHERE locale=:locale AND context=:context AND normalized_alias=:normalized"
                ),
                {
                    "locale": locale,
                    "context": context,
                    "normalized": normalize_identity_text(alias),
                },
            )
            connection.execute(
                text(f"""INSERT INTO {table} (locale, context, normalized_alias, alias, {fk}, reviewed)
                    VALUES (:locale, :context, :normalized, :alias, :identity_id, true)"""),
                {
                    "locale": locale,
                    "context": context,
                    "normalized": normalize_identity_text(alias),
                    "alias": alias,
                    "identity_id": identity_id,
                },
            )

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
