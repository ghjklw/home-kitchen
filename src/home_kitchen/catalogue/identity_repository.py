"""Persistence operations for canonical identities and contextual aliases."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import NoInspectionAvailable

from home_kitchen.catalogue.identity import (
    VALID_ALIAS_SOURCE_TYPES,
    AliasResolution,
    AliasSourceType,
    CanonicalProduct,
    CanonicalUnit,
    IdentityCorrectionEvent,
    normalize_identity_text,
)


def _validate_source_type(source_type: str) -> AliasSourceType:
    if source_type not in VALID_ALIAS_SOURCE_TYPES:
        raise ValueError(
            f"source_type must be one of {sorted(VALID_ALIAS_SOURCE_TYPES)}, got {source_type!r}"
        )
    return cast(AliasSourceType, source_type)


class PostgresIdentityRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _table_columns(connection: Connection, table: str) -> set[str]:
        try:
            return {col["name"] for col in inspect(connection).get_columns(table)}
        except NoInspectionAvailable:
            return set()

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
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        identity_id: UUID,
        *,
        household_id: UUID | None = None,
        source_type: AliasSourceType = "automatic_suggestion",
    ) -> None:
        table, fk = self._table(kind)
        validated_source_type = _validate_source_type(source_type)
        normalized = normalize_identity_text(alias)
        with self.engine.begin() as connection:
            self._require_identity(connection, kind, identity_id)
            cols = self._table_columns(connection, table)
            has_household = "household_id" in cols
            has_source_type = "source_type" in cols

            insert_cols = ["locale", "context", "normalized_alias", "alias", fk, "reviewed"]
            select_exprs = [":locale", ":context", ":normalized", ":alias", ":identity_id", "false"]
            params: dict[str, Any] = {
                "locale": locale,
                "context": context,
                "normalized": normalized,
                "alias": alias,
                "identity_id": identity_id,
            }
            if has_household:
                insert_cols.append("household_id")
                if household_id is None:
                    select_exprs.append("NULL")
                    scope_clause = "household_id IS NULL"
                else:
                    select_exprs.append(":household_id")
                    params["household_id"] = household_id
                    scope_clause = "household_id = :household_id"
            else:
                scope_clause = "1=1"

            if has_source_type:
                insert_cols.append("source_type")
                select_exprs.append(":source_type")
                params["source_type"] = validated_source_type

            connection.execute(
                text(f"""INSERT INTO {table} ({", ".join(insert_cols)})
                    SELECT {", ".join(select_exprs)}
                    WHERE NOT EXISTS (
                        SELECT 1 FROM {table}
                        WHERE {scope_clause}
                          AND locale=:locale
                          AND context=:context
                          AND normalized_alias=:normalized
                          AND reviewed=true
                    )
                    ON CONFLICT DO NOTHING"""),
                params,
            )

    def has_reviewed_alias(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> bool:
        table, _ = self._table(kind)
        with self.engine.connect() as connection:
            cols = self._table_columns(connection, table)
            has_household = "household_id" in cols
            params: dict[str, Any] = {
                "locale": normalize_identity_text(locale),
                "context": normalize_identity_text(context),
                "normalized": normalize_identity_text(alias),
            }
            scope_sql = ""
            if has_household:
                if household_id is None:
                    scope_sql = " AND household_id IS NULL"
                else:
                    scope_sql = " AND (household_id = :household_id OR household_id IS NULL)"
                    params["household_id"] = household_id
            row = connection.execute(
                text(f"""SELECT 1 FROM {table}
                WHERE locale=:locale AND context IN (:context, '') AND normalized_alias=:normalized AND reviewed=true{scope_sql}
                LIMIT 1"""),
                params,
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
        validated_source_type = _validate_source_type(source_type)
        normalized = normalize_identity_text(alias)
        with self.engine.begin() as connection:
            self._require_identity(connection, kind, identity_id)
            cols = self._table_columns(connection, table)
            has_household = "household_id" in cols
            has_source_type = "source_type" in cols

            base_params = {"locale": locale, "context": context, "normalized": normalized}
            if has_household and household_id is not None:
                scoped_existing = (
                    connection.execute(
                        text(
                            f"SELECT {fk} AS identity_id, reviewed FROM {table} "
                            "WHERE household_id=:household_id AND locale=:locale "
                            "AND context=:context AND normalized_alias=:normalized "
                            "LIMIT 1"
                        ),
                        {**base_params, "household_id": household_id},
                    )
                    .mappings()
                    .first()
                )
                if scoped_existing is not None:
                    scoped_id = (
                        UUID(str(scoped_existing["identity_id"]))
                        if scoped_existing["identity_id"] is not None
                        else None
                    )
                    scoped_reviewed = bool(scoped_existing["reviewed"])
                    if scoped_id == identity_id and scoped_reviewed is True:
                        return None
                    existing = scoped_existing
                else:
                    existing = (
                        connection.execute(
                            text(
                                f"SELECT {fk} AS identity_id, reviewed FROM {table} "
                                "WHERE household_id IS NULL AND locale=:locale "
                                "AND context=:context AND normalized_alias=:normalized "
                                f"ORDER BY reviewed DESC, {fk} LIMIT 1"
                            ),
                            base_params,
                        )
                        .mappings()
                        .first()
                    )
            else:
                scope_sql = "household_id IS NULL AND " if has_household else ""
                existing = (
                    connection.execute(
                        text(
                            f"SELECT {fk} AS identity_id, reviewed FROM {table} "
                            f"WHERE {scope_sql}locale=:locale AND context=:context "
                            "AND normalized_alias=:normalized "
                            f"ORDER BY reviewed DESC, {fk} LIMIT 1"
                        ),
                        base_params,
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    prev_id = (
                        UUID(str(existing["identity_id"]))
                        if existing["identity_id"] is not None
                        else None
                    )
                    if prev_id == identity_id and bool(existing["reviewed"]) is True:
                        return None

            previous_identity_id = (
                UUID(str(existing["identity_id"]))
                if existing is not None and existing["identity_id"] is not None
                else None
            )
            previous_reviewed = bool(existing["reviewed"]) if existing is not None else None

            if has_household:
                if household_id is not None:
                    connection.execute(
                        text(
                            f"DELETE FROM {table} WHERE household_id=:household_id "
                            "AND locale=:locale AND context=:context AND normalized_alias=:normalized"
                        ),
                        {**base_params, "household_id": household_id},
                    )
                else:
                    connection.execute(
                        text(
                            f"DELETE FROM {table} WHERE household_id IS NULL "
                            "AND locale=:locale AND context=:context AND normalized_alias=:normalized"
                        ),
                        base_params,
                    )
            else:
                connection.execute(
                    text(
                        f"DELETE FROM {table} WHERE locale=:locale AND context=:context AND normalized_alias=:normalized"
                    ),
                    base_params,
                )

            insert_cols = ["locale", "context", "normalized_alias", "alias", fk, "reviewed"]
            insert_vals = [":locale", ":context", ":normalized", ":alias", ":identity_id", "true"]
            insert_params: dict[str, Any] = {
                **base_params,
                "alias": alias,
                "identity_id": identity_id,
            }
            if has_household:
                insert_cols.append("household_id")
                insert_vals.append(":household_id")
                insert_params["household_id"] = household_id
            if has_source_type:
                insert_cols.append("source_type")
                insert_vals.append(":source_type")
                insert_params["source_type"] = validated_source_type

            connection.execute(
                text(
                    f"INSERT INTO {table} ({', '.join(insert_cols)}) "
                    f"VALUES ({', '.join(insert_vals)})"
                ),
                insert_params,
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
                source_type=validated_source_type,
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

    def resolve_with_metadata(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> AliasResolution:
        table, fk = self._table(kind)
        identity_table = (
            "kitchen_canonical_products" if kind == "product" else "kitchen_canonical_units"
        )
        columns = (
            "p.id, p.canonical_name, p.variant" if kind == "product" else "p.id, p.canonical_name"
        )
        normalized = normalize_identity_text(alias)
        with self.engine.connect() as connection:
            cols = self._table_columns(connection, table)
            has_household = "household_id" in cols
            has_source_type = "source_type" in cols
            source_col = (
                "a.source_type" if has_source_type else "'reviewed_correction' AS source_type"
            )
            select_sql = (
                f"SELECT {columns}, a.context AS alias_context, a.reviewed, {source_col} "
                f"FROM {table} a JOIN {identity_table} p ON p.id = a.{fk}"
            )
            base_params: dict[str, Any] = {
                "locale": locale,
                "context": context,
                "normalized": normalized,
            }

            rows: Sequence[Any] = ()
            household_scoped = False
            if has_household and household_id is not None:
                rows = (
                    connection.execute(
                        text(
                            f"{select_sql} "
                            "WHERE a.household_id=:household_id AND a.locale=:locale "
                            "AND a.context IN (:context, '') AND a.normalized_alias=:normalized "
                            "ORDER BY p.canonical_name, p.id"
                        ),
                        {**base_params, "household_id": household_id},
                    )
                    .mappings()
                    .all()
                )
                if rows:
                    household_scoped = True
                    if context:
                        exact_rows = [r for r in rows if r["alias_context"] == context]
                        if exact_rows:
                            rows = exact_rows

            if not rows:
                scope_sql = "a.household_id IS NULL AND " if has_household else ""
                rows = (
                    connection.execute(
                        text(
                            f"{select_sql} "
                            f"WHERE {scope_sql}a.locale=:locale AND a.context IN (:context, '') "
                            "AND a.normalized_alias=:normalized "
                            "ORDER BY p.canonical_name, p.id"
                        ),
                        base_params,
                    )
                    .mappings()
                    .all()
                )

        model = CanonicalProduct if kind == "product" else CanonicalUnit
        candidates_by_id: dict[UUID, CanonicalProduct | CanonicalUnit] = {}
        source_types: list[AliasSourceType] = []
        reviewed_flags: list[bool] = []
        for row in rows:
            row_dict = dict(row)
            identity = model.model_validate(row_dict)
            if identity.id not in candidates_by_id:
                candidates_by_id[identity.id] = identity
            raw_source = row_dict.get("source_type") or "reviewed_correction"
            validated_source = _validate_source_type(str(raw_source))
            if validated_source not in source_types:
                source_types.append(validated_source)
            reviewed_flags.append(bool(row_dict.get("reviewed")))

        candidates = list(candidates_by_id.values())
        if not candidates:
            return AliasResolution(status="unmapped", candidates=[])

        status = "resolved" if len(candidates) == 1 else "ambiguous"
        resolved_source_type: AliasSourceType | None = (
            source_types[0] if len(source_types) == 1 else None
        )
        resolved_reviewed: bool | None = (
            reviewed_flags[0]
            if len(candidates) == 1 and len(reviewed_flags) == 1
            else (all(reviewed_flags) if len(candidates) == 1 else None)
        )
        return AliasResolution(
            status=status,
            candidates=candidates,
            source_type=resolved_source_type,
            source_types=source_types,
            reviewed=resolved_reviewed,
            household_scoped=household_scoped,
        )

    def resolve(
        self,
        kind: str,
        locale: str,
        context: str,
        alias: str,
        *,
        household_id: UUID | None = None,
    ) -> list[CanonicalProduct | CanonicalUnit]:
        return self.resolve_with_metadata(
            kind, locale, context, alias, household_id=household_id
        ).candidates

    def validate_identities(self, kind: str, ids: set[UUID]) -> None:
        if not ids:
            return
        self._table(kind)
        identity_table = (
            "kitchen_canonical_products" if kind == "product" else "kitchen_canonical_units"
        )
        with self.engine.connect() as connection:
            existing = {
                UUID(str(val))
                for val in connection.execute(
                    text(f"SELECT id FROM {identity_table} WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": list(ids)},
                ).scalars()
            }
        if existing != ids:
            raise ValueError(f"Recipe contains an unknown canonical {kind} identity")

    @staticmethod
    def _table(kind: str) -> tuple[str, str]:
        if kind == "product":
            return "kitchen_product_aliases", "product_id"
        if kind == "unit":
            return "kitchen_unit_aliases", "unit_id"
        raise ValueError("kind must be product or unit")
