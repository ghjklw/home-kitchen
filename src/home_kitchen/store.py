"""Transactional PostgreSQL storage for kitchen records."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    ForeignKey,
    Identity,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, make_url

from .context import history_context
from .history import HistoryEntryCreate, HistoryRepository
from .models import Meal, Recipe, RegularItem, ShoppingItem

Model = TypeVar("Model", bound=BaseModel)
metadata = MetaData()
recipes = Table(
    "recipes",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("servings", Integer, nullable=False),
    Column("ingredients", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("instructions", JSON().with_variant(JSONB, "postgresql"), nullable=False),
    Column("source_url", Text),
    Column("image_url", Text),
    Column("source", Text, nullable=False),
    Column("source_ingredients", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("provenance", JSONB),
    Column("photo_source", JSONB),
    Column("photo_source_id", Uuid, ForeignKey("recipe_photos.id")),
    Column("image_attribution", JSONB),
)
meals = Table(
    "meals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("date", Date, nullable=False),
    Column("slot", Text, nullable=False),
    Column("recipe_id", Uuid, ForeignKey("recipes.id"), nullable=False),
    Column("servings", Integer, nullable=False),
    UniqueConstraint("date", "slot"),
)
items = Table(
    "items",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("category", Text, nullable=False, server_default="Other"),
    Column("quantity", Numeric, nullable=False),
    Column("unit", Text, nullable=False),
    Column("completed", Boolean, nullable=False),
)
regulars = Table(
    "regulars",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("position", BigInteger, Identity(), nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("category", Text, nullable=False, server_default="Other"),
    Column("quantity", Numeric, nullable=False),
    Column("unit", Text, nullable=False),
    Column("interval_days", Integer, nullable=False),
    Column("last_purchased", Date),
)
confirmations = Table(
    "confirmations",
    metadata,
    Column("regular_id", Uuid, ForeignKey("regulars.id", ondelete="CASCADE"), primary_key=True),
    Column("item_id", Uuid, ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
)
settings = Table(
    "settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
)


class MealSlotOccupied(ValueError):
    """A new meal must never silently replace an existing plan."""


TABLES = {"recipe": recipes, "meal": meals, "item": items, "regular": regulars}


def entity_values(entity: BaseModel) -> dict:
    values = entity.model_dump()
    # JSON columns contain JSON values; native dates, UUIDs and decimals elsewhere
    # retain their database types and precision.
    if isinstance(entity, Recipe):
        json_values = entity.model_dump(mode="json")
        for field in (
            "ingredients",
            "source_ingredients",
            "provenance",
            "photo_source",
            "image_attribution",
        ):
            values[field] = json_values[field]
        values["photo_source_id"] = entity.photo_source.id if entity.photo_source else None
        values["source_url"] = str(entity.source_url) if entity.source_url else None
        values["image_url"] = str(entity.image_url) if entity.image_url else None
    return values


class Store:
    def __init__(self, database_url: str):
        if make_url(database_url).drivername != "postgresql+psycopg":
            raise ValueError("database_url must use postgresql+psycopg")
        self.engine = create_engine(database_url, pool_pre_ping=True)

    @contextmanager
    def connection(self) -> Generator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    def health(self) -> None:
        with self.connection() as connection:
            connection.execute(text("SELECT 1"))

    def save(self, kind: str, entity: Recipe | Meal | RegularItem | ShoppingItem) -> None:
        table = TABLES[kind]
        values = entity_values(entity)
        statement = insert(table).values(**values)
        conflict = [table.c.date, table.c.slot] if kind == "meal" else [table.c.id]
        with self.connection() as connection:
            if isinstance(entity, Recipe) and entity.photo_source is not None:
                from .recipes.photo_storage import recipe_photos

                exists = connection.execute(
                    select(recipe_photos.c.id).where(recipe_photos.c.id == entity.photo_source.id)
                ).scalar_one_or_none()
                if exists is None:
                    raise ValueError("Photo source was not found")
            previous = None
            if isinstance(entity, RegularItem):
                previous = connection.execute(
                    select(regulars.c.last_purchased).where(regulars.c.id == entity.id)
                ).first()
            if kind == "meal":
                inserted = connection.execute(
                    statement.on_conflict_do_nothing(index_elements=conflict).returning(table.c.id)
                ).scalar_one_or_none()
                if inserted is None:
                    raise MealSlotOccupied(
                        "This meal slot is already planned. Choose another slot or remove "
                        "the existing meal before adding its replacement."
                    )
            else:
                connection.execute(
                    statement.on_conflict_do_update(index_elements=conflict, set_=values)
                )
            purchased = (
                isinstance(entity, RegularItem)
                and entity.last_purchased is not None
                and (previous is None or previous[0] != entity.last_purchased)
            )
            HistoryRepository(self.engine).record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="purchase"
                    if purchased
                    else "planning"
                    if kind == "meal"
                    else "domain_event",
                    message=f"Purchased {entity.name}" if purchased else f"Saved {kind}",
                    structured={
                        "entity_type": kind,
                        "entity_id": str(entity.id),
                        "data": entity.model_dump(mode="json"),
                    },
                    recipe_id=entity.id
                    if isinstance(entity, Recipe)
                    else entity.recipe_id
                    if isinstance(entity, Meal)
                    else None,
                ),
                connection=connection,
            )

    def get(self, kind: str, entity_id: UUID, model: type[Model]) -> Model:
        table = TABLES[kind]
        with self.connection() as connection:
            row = (
                connection.execute(select(table).where(table.c.id == entity_id)).mappings().first()
            )
        if row is None:
            raise KeyError(str(entity_id))
        return model.model_validate(dict(row))

    def list(self, kind: str, model: type[Model]) -> list[Model]:
        with self.connection() as connection:
            rows = (
                connection.execute(select(TABLES[kind]).order_by(TABLES[kind].c.position))
                .mappings()
                .all()
            )
        return [model.model_validate(dict(row)) for row in rows]

    def delete(self, kind: str, entity_id: UUID) -> None:
        table = TABLES[kind]
        with self.connection() as connection:
            result = connection.execute(table.delete().where(table.c.id == entity_id))
            if result.rowcount == 0:
                raise KeyError(str(entity_id))

    def active_confirmations(self) -> set[UUID]:
        with self.connection() as connection:
            return set(
                connection.execute(
                    select(confirmations.c.regular_id)
                    .join(items)
                    .where(items.c.completed.is_(False))
                ).scalars()
            )

    def confirm(self, regular_id: UUID, item: ShoppingItem) -> ShoppingItem:
        """Lock the regular record so concurrent requests reuse its active capture."""
        with self.connection() as connection:
            regular = connection.execute(
                select(regulars.c.id).where(regulars.c.id == regular_id).with_for_update()
            ).first()
            if regular is None:
                raise KeyError(str(regular_id))
            row = (
                connection.execute(
                    select(items)
                    .join(confirmations)
                    .where(confirmations.c.regular_id == regular_id)
                )
                .mappings()
                .first()
            )
            if row is not None and not row["completed"]:
                return ShoppingItem.model_validate(dict(row))
            connection.execute(insert(items).values(**entity_values(item)))
            statement = insert(confirmations).values(regular_id=regular_id, item_id=item.id)
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[confirmations.c.regular_id], set_={"item_id": item.id}
                )
            )
        return item

    def get_setting(self, key: str) -> str | None:
        with self.connection() as connection:
            return connection.execute(
                select(settings.c.value).where(settings.c.key == key)
            ).scalar()

    def ensure_setting(self, key: str, value: str) -> str:
        """Initialize a deployment setting once, including concurrent startups."""
        with self.connection() as connection:
            connection.execute(
                insert(settings)
                .values(key=key, value=value)
                .on_conflict_do_nothing(index_elements=[settings.c.key])
            )
            return connection.execute(
                select(settings.c.value).where(settings.c.key == key)
            ).scalar_one()

    def set_setting(self, key: str, value: str) -> None:
        statement = insert(settings).values(key=key, value=value)
        with self.connection() as connection:
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[settings.c.key], set_={"value": value}
                )
            )
