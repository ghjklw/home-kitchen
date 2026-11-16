"""Reviewable meal proposals with atomic, idempotent explicit confirmation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel
from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Connection

from .context import history_context
from .history import HistoryEntryCreate, HistoryRepository
from .models import Meal, MealCreate, Recipe
from .service import KitchenService
from .store import entity_values, meals, recipes

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


class ProposalConflict(ValueError):
    """Refresh a stale proposal and explicitly review any changed entries."""


class ProposalExpired(ProposalConflict):
    """The proposal must be recreated and reviewed again."""


class ProposalNotFound(KeyError):
    """No proposal is visible to the requesting actor."""


class ProposalActorMismatch(ProposalNotFound):
    """The proposal belongs to another actor."""


class ProposalRead(BaseModel):
    id: UUID
    meals: list[MealCreate]
    recipe_fingerprints: dict[str, str]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["pending", "applied", "expired", "stale"]
    applied_meal_ids: list[UUID]
    attempt_count: int
    last_error: str | None = None


def _fingerprint(recipe: Recipe) -> str:
    canonical = json.dumps(recipe.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class PlanningProposalService:
    def __init__(
        self,
        kitchen: KitchenService,
        history: HistoryRepository | None = None,
        *,
        ttl_seconds: int = 900,
        max_meals: int = 90,
        max_range_days: int = 366,
    ):
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("Proposal TTL must be between 1 second and 24 hours")
        if not 1 <= max_meals <= 90 or not 1 <= max_range_days <= 366:
            raise ValueError("Proposal bounds must be at most 90 meals and 366 days")
        self.kitchen = kitchen
        self.history = history or HistoryRepository(kitchen.store.engine)
        self.ttl_seconds = ttl_seconds
        self.max_meals = max_meals
        self.max_range_days = max_range_days

    @staticmethod
    def _actor(actor_id: str) -> None:
        if not actor_id or not actor_id.strip():
            raise ValueError("An authenticated actor is required")

    @staticmethod
    def _read(row) -> ProposalRead:
        return ProposalRead.model_validate(dict(row))

    def _load(self, connection: Connection, proposal_id: UUID, actor_id: str, *, lock=False):
        self._actor(actor_id)
        statement = select(_proposals).where(_proposals.c.id == proposal_id)
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise ProposalNotFound(str(proposal_id))
        if row["actor_id"] != actor_id:
            raise ProposalActorMismatch(str(proposal_id))
        return row

    @staticmethod
    @staticmethod
    def _recipes(connection: Connection, proposed: list[MealCreate], *, lock=False):
        fingerprints = {}
        # Consistent lock order avoids deadlocks between overlapping proposals.
        for recipe_id in sorted({meal.recipe_id for meal in proposed}, key=str):
            statement = select(recipes).where(recipes.c.id == recipe_id)
            if lock:
                statement = statement.with_for_update()
            row = connection.execute(statement).mappings().first()
            if row is None:
                raise ProposalConflict(
                    f"Recipe {recipe_id} is unavailable. Create and review a new proposal."
                )
            fingerprints[str(recipe_id)] = _fingerprint(Recipe.model_validate(dict(row)))
        return fingerprints

    def create(self, actor_id: str, meals: list[MealCreate]) -> ProposalRead:
        self._actor(actor_id)
        proposed = [MealCreate.model_validate(meal) for meal in meals]
        if not 1 <= len(proposed) <= self.max_meals:
            raise ValueError(f"A proposal must contain between 1 and {self.max_meals} meals")
        dates = [meal.date for meal in proposed]
        if (max(dates) - min(dates)).days + 1 > self.max_range_days:
            raise ValueError(f"Proposal date range must be at most {self.max_range_days} days")
        now = datetime.now(UTC)
        with self.kitchen.store.connection() as connection:
            fingerprints = self._recipes(connection, proposed)
            row = (
                connection.execute(
                    insert(_proposals)
                    .values(
                        id=uuid4(),
                        actor_id=actor_id,
                        meals=[meal.model_dump(mode="json") for meal in proposed],
                        recipe_fingerprints=fingerprints,
                        created_at=now,
                        expires_at=now + timedelta(seconds=self.ttl_seconds),
                        status="pending",
                        applied_meal_ids=[],
                        attempt_count=0,
                        last_error=None,
                    )
                    .returning(_proposals)
                )
                .mappings()
                .one()
            )
            return self._read(row)

    def get(self, proposal_id: UUID, actor_id: str) -> ProposalRead:
        with self.kitchen.store.connection() as connection:
            return self._read(self._load(connection, proposal_id, actor_id))

    def confirm(self, proposal_id: UUID, actor_id: str, *, reviewed: bool) -> ProposalRead:
        if reviewed is not True:
            raise ValueError("Explicit reviewed=true is required to apply the whole proposal")
        failure = None
        with self.kitchen.store.connection() as connection:
            proposal = self._read(self._load(connection, proposal_id, actor_id, lock=True))
            if proposal.status == "applied":
                return proposal
            attempts = proposal.attempt_count + 1
            status = "stale"
            try:
                if proposal.status == "expired" or proposal.expires_at <= datetime.now(UTC):
                    status = "expired"
                    raise ProposalExpired("Proposal expired. Create and review a new proposal.")
                if proposal.status != "pending":
                    raise ProposalConflict("Proposal is stale. Create and review a new proposal.")
                # A savepoint rolls back every meal/history write while the outer
                # transaction records the failed confirmation attempt durably.
                with connection.begin_nested():
                    fingerprints = self._recipes(connection, proposal.meals, lock=True)
                    if fingerprints != proposal.recipe_fingerprints:
                        raise ProposalConflict(
                            "Source recipes changed. Create and review a new proposal."
                        )
                    if proposal.expires_at <= datetime.now(UTC):
                        status = "expired"
                        raise ProposalExpired("Proposal expired. Create and review a new proposal.")
                    applied = []
                    for data in proposal.meals:
                        meal = Meal(id=uuid4(), **data.model_dump())
                        inserted = connection.execute(
                            insert(meals)
                            .values(**entity_values(meal))
                            .on_conflict_do_nothing(index_elements=[meals.c.id])
                            .returning(meals.c.id)
                        ).scalar_one_or_none()
                        if inserted is None:
                            raise ProposalConflict(
                                f"Meal ID {meal.id} already exists. Create and review a new proposal."
                            )
                        applied.append(meal)
                        self.history.record(
                            HistoryEntryCreate(
                                channel=history_context.get().channel,
                                kind="planning",
                                message="Applied reviewed meal plan",
                                recipe_id=meal.recipe_id,
                                structured={
                                    "proposal_id": str(proposal.id),
                                    "entity_type": "meal",
                                    "entity_id": str(meal.id),
                                    "data": meal.model_dump(mode="json"),
                                },
                            ),
                            actor_id=actor_id,
                            connection=connection,
                        )
                    # Return IDs in the same order as the reviewed input.
                    proposal.applied_meal_ids = [meal.id for meal in applied]
                    status = "applied"
            except ProposalConflict as exc:
                failure = exc
            row = (
                connection.execute(
                    _proposals.update()
                    .where(_proposals.c.id == proposal.id)
                    .values(
                        status=status,
                        attempt_count=attempts,
                        last_error=str(failure) if failure else None,
                        applied_meal_ids=[str(value) for value in proposal.applied_meal_ids],
                    )
                    .returning(_proposals)
                )
                .mappings()
                .one()
            )
            result = self._read(row)
        if failure:
            raise failure
        return result
