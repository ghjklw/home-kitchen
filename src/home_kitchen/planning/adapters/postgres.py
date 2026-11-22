"""PostgreSQL / SQLAlchemy secondary adapters for meal planning proposals."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection, Engine

from home_kitchen.context import HistoryChannel, history_context
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.models import Meal, Recipe
from home_kitchen.planning.domain.exceptions import (
    MealEditConflict,
    ProposalActorMismatch,
    ProposalNotFound,
)
from home_kitchen.planning.domain.models import ProposalRead, ProposalRecord, ProposalStatus
from home_kitchen.planning.ports.readers import RecipeReader, SingleRecipeReader
from home_kitchen.planning.ports.recorders import HistoryRecorder
from home_kitchen.planning.ports.repository import ProposalRepository
from home_kitchen.planning.ports.unit_of_work import PlanningSession, PlanningUnitOfWork
from home_kitchen.planning.ports.writers import MealAgendaRepository, MealWriter
from home_kitchen.store import MealIDConflict, Store, entity_values, meals, recipes

if TYPE_CHECKING:
    pass

_proposals = Table(
    "kitchen_plan_proposals",
    MetaData(),
    Column("id", Uuid, primary_key=True),
    Column("actor_id", Text, nullable=False),
    Column("meals", JSONB, nullable=False),
    Column("recipe_fingerprints", JSONB, nullable=False),
    Column("meal_fingerprints", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("target_meals", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("applied_meal_ids", JSONB, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("last_error", Text),
)


class PostgresProposalRepository(ProposalRepository):
    """PostgreSQL implementation of proposal persistence port."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def save(self, record: ProposalRecord) -> ProposalRead:
        row = (
            self.connection.execute(
                insert(_proposals)
                .values(
                    id=record.id,
                    actor_id=record.actor_id,
                    meals=record.meals,
                    recipe_fingerprints=record.recipe_fingerprints,
                    meal_fingerprints=record.meal_fingerprints,
                    target_meals=record.target_meals,
                    created_at=record.created_at,
                    expires_at=record.expires_at,
                    status=record.status,
                    applied_meal_ids=[str(v) for v in record.applied_meal_ids],
                    attempt_count=record.attempt_count,
                    last_error=record.last_error,
                )
                .returning(_proposals)
            )
            .mappings()
            .one()
        )
        return ProposalRead.model_validate(dict(row))

    def get(self, proposal_id: UUID, actor_id: str, *, lock: bool = False) -> ProposalRead:
        statement = select(_proposals).where(_proposals.c.id == proposal_id)
        if lock:
            statement = statement.with_for_update()
        row = self.connection.execute(statement).mappings().first()
        if row is None:
            raise ProposalNotFound(str(proposal_id))
        if row["actor_id"] != actor_id:
            raise ProposalActorMismatch(str(proposal_id))
        return ProposalRead.model_validate(dict(row))

    def update_status(
        self,
        proposal_id: UUID,
        *,
        status: ProposalStatus,
        attempt_count: int,
        last_error: str | None,
        applied_meal_ids: list[UUID],
    ) -> ProposalRead:
        row = (
            self.connection.execute(
                _proposals.update()
                .where(_proposals.c.id == proposal_id)
                .values(
                    status=status,
                    attempt_count=attempt_count,
                    last_error=last_error,
                    applied_meal_ids=[str(value) for value in applied_meal_ids],
                )
                .returning(_proposals)
            )
            .mappings()
            .one()
        )
        return ProposalRead.model_validate(dict(row))


class PostgresRecipeReader(RecipeReader):
    """PostgreSQL implementation of recipe reader port."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def get_recipes(self, recipe_ids: list[UUID], *, lock: bool = False) -> dict[UUID, Recipe]:
        result: dict[UUID, Recipe] = {}
        # Query recipes in consistent order
        for recipe_id in sorted(recipe_ids, key=str):
            statement = select(recipes).where(recipes.c.id == recipe_id)
            if lock:
                statement = statement.with_for_update()
            row = self.connection.execute(statement).mappings().first()
            if row is not None:
                result[recipe_id] = Recipe.model_validate(dict(row))
        return result


class PostgresMealWriter(MealWriter):
    """PostgreSQL implementation of meal persistence port."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def get_meals(self, meal_ids: list[UUID], *, lock: bool = False) -> dict[UUID, Meal]:
        result: dict[UUID, Meal] = {}
        for meal_id in sorted(set(meal_ids), key=str):
            statement = select(meals).where(meals.c.id == meal_id)
            if lock:
                statement = statement.with_for_update()
            row = self.connection.execute(statement).mappings().first()
            if row is not None:
                result[meal_id] = Meal.model_validate(dict(row))
        return result

    def insert_meal(self, meal: Meal) -> bool:
        inserted = self.connection.execute(
            insert(meals)
            .values(**entity_values(meal))
            .on_conflict_do_nothing(index_elements=[meals.c.id])
            .returning(meals.c.id)
        ).scalar_one_or_none()
        return inserted is not None

    def update_meal(self, meal: Meal) -> bool:
        values = entity_values(meal)
        values.pop("id")
        values.pop("version", None)
        updated = self.connection.execute(
            meals.update()
            .where(meals.c.id == meal.id)
            .values(**values, version=meals.c.version + 1)
            .returning(meals.c.id)
        ).scalar_one_or_none()
        return updated is not None


class PostgresHistoryRecorder(HistoryRecorder):
    """PostgreSQL implementation of planning history recorder port."""

    def __init__(self, connection: Connection, history: HistoryRepository):
        self.connection = connection
        self.history = history

    def record_planning(
        self,
        *,
        actor_id: str,
        channel: HistoryChannel,
        message: str,
        recipe_id: UUID,
        structured: dict[str, Any],
    ) -> None:
        self.history.record(
            HistoryEntryCreate(
                channel=channel,
                kind="planning",
                message=message,
                recipe_id=recipe_id,
                structured=structured,
            ),
            actor_id=actor_id,
            connection=self.connection,
        )


class PostgresPlanningSession(PlanningSession):
    """PostgreSQL session managing transactional operations and savepoints."""

    def __init__(self, connection: Connection, history: HistoryRepository):
        self.connection = connection
        self._proposals = PostgresProposalRepository(connection)
        self._recipes = PostgresRecipeReader(connection)
        self._meals = PostgresMealWriter(connection)
        self._history = PostgresHistoryRecorder(connection, history)

    @property
    def proposals(self) -> ProposalRepository:
        return self._proposals

    @property
    def recipes(self) -> RecipeReader:
        return self._recipes

    @property
    def meals(self) -> MealWriter:
        return self._meals

    @property
    def history(self) -> HistoryRecorder:
        return self._history

    @contextmanager
    def savepoint(self) -> Generator[None]:
        with self.connection.begin_nested():
            yield


class PostgresPlanningUnitOfWork(PlanningUnitOfWork):
    """PostgreSQL Unit of Work creating connection sessions from an Engine or Store."""

    def __init__(self, store_or_engine: Store | Engine, history: Any):
        self.store_or_engine = store_or_engine
        self.history = history

    @contextmanager
    def session(self) -> Generator[PostgresPlanningSession]:
        if isinstance(self.store_or_engine, Engine):
            with self.store_or_engine.begin() as connection:
                yield PostgresPlanningSession(connection, self.history)
        else:
            with self.store_or_engine.connection() as connection:
                yield PostgresPlanningSession(connection, self.history)


class PostgresSingleRecipeReader(SingleRecipeReader):
    """PostgreSQL implementation of single-recipe lookup for meal planning."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        with self.engine.begin() as connection:
            row = (
                connection.execute(select(recipes).where(recipes.c.id == recipe_id))
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(str(recipe_id))
        return Recipe.model_validate(dict(row))


class PostgresMealAgendaRepository(MealAgendaRepository):
    """PostgreSQL adapter for planned meal-agenda CRUD and planning history."""

    def __init__(self, engine: Engine, history: Any = None) -> None:
        self.engine = engine
        self.history = history if history is not None else HistoryRepository(engine)

    def save_meal(self, meal: Meal) -> None:
        values = entity_values(meal)
        with self.engine.begin() as connection:
            inserted = connection.execute(
                insert(meals)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[meals.c.id])
                .returning(meals.c.id)
            ).scalar_one_or_none()
            if inserted is None:
                raise MealIDConflict("A meal with this ID already exists")
            self.history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="planning",
                    message="Saved meal",
                    structured={
                        "entity_type": "meal",
                        "entity_id": str(meal.id),
                        "data": meal.model_dump(mode="json"),
                    },
                    recipe_id=meal.recipe_id,
                ),
                connection=connection,
            )

    def get_meal(self, meal_id: UUID) -> Meal:
        with self.engine.begin() as connection:
            row = connection.execute(select(meals).where(meals.c.id == meal_id)).mappings().first()
        if row is None:
            raise KeyError(str(meal_id))
        return Meal.model_validate(dict(row))

    def update_meal(self, meal: Meal, *, expected_version: int) -> Meal:
        values = entity_values(meal)
        values.pop("id")
        values.pop("version", None)
        with self.engine.begin() as connection:
            statement = update(meals).where(meals.c.id == meal.id)
            statement = statement.where(meals.c.version == expected_version)
            row = (
                connection.execute(
                    statement.values(**values, version=meals.c.version + 1).returning(meals)
                )
                .mappings()
                .first()
            )
            if row is None:
                current_version = connection.execute(
                    select(meals.c.version).where(meals.c.id == meal.id)
                ).scalar_one_or_none()
                if current_version is None:
                    raise KeyError(str(meal.id))
                raise MealEditConflict(expected_version, current_version)
            updated_meal = Meal.model_validate(dict(row))
            self.history.record(
                HistoryEntryCreate(
                    channel=history_context.get().channel,
                    kind="planning",
                    message="Updated planned meal",
                    recipe_id=updated_meal.recipe_id,
                    structured={
                        "entity_type": "meal",
                        "entity_id": str(updated_meal.id),
                        "data": updated_meal.model_dump(mode="json"),
                    },
                ),
                connection=connection,
            )
            return updated_meal

    def list_meals(self) -> list[Meal]:
        with self.engine.begin() as connection:
            rows = connection.execute(select(meals).order_by(meals.c.position)).mappings().all()
        return [Meal.model_validate(dict(row)) for row in rows]

    def delete_meal(self, meal_id: UUID) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(meals.delete().where(meals.c.id == meal_id))
            if result.rowcount == 0:
                raise KeyError(str(meal_id))

    def has_meals_for_recipe(self, recipe_id: UUID) -> bool:
        with self.engine.begin() as connection:
            row = connection.execute(
                select(meals.c.id).where(meals.c.recipe_id == recipe_id).limit(1)
            ).first()
        return row is not None


# Backward-compatibility alias
PostgresPlanningProposalAdapter = PostgresPlanningUnitOfWork
