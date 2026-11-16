"""PostgreSQL / SQLAlchemy secondary adapters for meal planning proposals."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection

from home_kitchen.history import HistoryChannel, HistoryEntryCreate, HistoryRepository
from home_kitchen.models import Meal, Recipe
from home_kitchen.planning.domain.exceptions import (
    ProposalActorMismatch,
    ProposalNotFound,
)
from home_kitchen.planning.domain.models import ProposalRead, ProposalRecord, ProposalStatus
from home_kitchen.planning.ports.readers import RecipeReader
from home_kitchen.planning.ports.recorders import HistoryRecorder
from home_kitchen.planning.ports.repository import ProposalRepository
from home_kitchen.planning.ports.unit_of_work import PlanningSession, PlanningUnitOfWork
from home_kitchen.planning.ports.writers import MealWriter
from home_kitchen.store import Store, entity_values, meals, recipes

if TYPE_CHECKING:
    pass

_proposals = Table(
    "kitchen_plan_proposals",
    MetaData(),
    Column("id", Uuid, primary_key=True),
    Column("actor_id", Text, nullable=False),
    Column("meals", JSONB, nullable=False),
    Column("recipe_fingerprints", JSONB, nullable=False),
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

    def insert_meal(self, meal: Meal) -> bool:
        inserted = self.connection.execute(
            insert(meals)
            .values(**entity_values(meal))
            .on_conflict_do_nothing(index_elements=[meals.c.id])
            .returning(meals.c.id)
        ).scalar_one_or_none()
        return inserted is not None


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
    """PostgreSQL Unit of Work creating connection sessions from the store engine."""

    def __init__(self, store: Store, history: HistoryRepository):
        self.store = store
        self.history = history

    @contextmanager
    def session(self) -> Generator[PostgresPlanningSession]:
        with self.store.connection() as connection:
            yield PostgresPlanningSession(connection, self.history)


# Backward-compatibility alias
PostgresPlanningProposalAdapter = PostgresPlanningUnitOfWork
