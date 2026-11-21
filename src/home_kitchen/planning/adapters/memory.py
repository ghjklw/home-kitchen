"""In-memory fake secondary adapters for testing meal planning proposals without DB."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from home_kitchen.context import HistoryChannel
from home_kitchen.models import Meal, Recipe
from home_kitchen.planning.domain.exceptions import (
    ProposalActorMismatch,
    ProposalNotFound,
)
from home_kitchen.planning.domain.models import ProposalRead, ProposalRecord, ProposalStatus
from home_kitchen.planning.ports.readers import RecipeReader, SingleRecipeReader
from home_kitchen.planning.ports.recorders import HistoryRecorder
from home_kitchen.planning.ports.repository import ProposalRepository
from home_kitchen.planning.ports.unit_of_work import PlanningSession, PlanningUnitOfWork
from home_kitchen.planning.ports.writers import MealAgendaRepository, MealWriter


class InMemoryProposalRepository(ProposalRepository):
    """In-memory implementation of proposal storage port."""

    def __init__(self, proposals: dict[UUID, ProposalRecord]):
        self.proposals = proposals

    def save(self, record: ProposalRecord) -> ProposalRead:
        self.proposals[record.id] = record.model_copy(deep=True)
        return ProposalRead.model_validate(record.model_dump())

    def get(self, proposal_id: UUID, actor_id: str, *, lock: bool = False) -> ProposalRead:
        if proposal_id not in self.proposals:
            raise ProposalNotFound(str(proposal_id))
        rec = self.proposals[proposal_id]
        if rec.actor_id != actor_id:
            raise ProposalActorMismatch(str(proposal_id))
        return ProposalRead.model_validate(rec.model_dump())

    def update_status(
        self,
        proposal_id: UUID,
        *,
        status: ProposalStatus,
        attempt_count: int,
        last_error: str | None,
        applied_meal_ids: list[UUID],
    ) -> ProposalRead:
        if proposal_id not in self.proposals:
            raise ProposalNotFound(str(proposal_id))
        rec = self.proposals[proposal_id]
        rec.status = status
        rec.attempt_count = attempt_count
        rec.last_error = last_error
        rec.applied_meal_ids = applied_meal_ids
        return ProposalRead.model_validate(rec.model_dump())


class InMemoryRecipeReader(RecipeReader):
    """In-memory implementation of recipe reader port."""

    def __init__(self, recipes: dict[UUID, Recipe]):
        self.recipes = recipes

    def get_recipes(self, recipe_ids: list[UUID], *, lock: bool = False) -> dict[UUID, Recipe]:
        return {
            rid: self.recipes[rid].model_copy(deep=True)
            for rid in sorted(recipe_ids, key=str)
            if rid in self.recipes
        }


class InMemoryMealWriter(MealWriter):
    """In-memory implementation of meal persistence port."""

    def __init__(self, meals: dict[UUID, Meal]):
        self.meals = meals

    def get_meals(self, meal_ids: list[UUID], *, lock: bool = False) -> dict[UUID, Meal]:
        return {
            mid: self.meals[mid].model_copy(deep=True)
            for mid in sorted(set(meal_ids), key=str)
            if mid in self.meals
        }

    def insert_meal(self, meal: Meal) -> bool:
        if meal.id in self.meals:
            return False
        self.meals[meal.id] = meal.model_copy(deep=True)
        return True

    def update_meal(self, meal: Meal) -> bool:
        if meal.id not in self.meals:
            return False
        self.meals[meal.id] = meal.model_copy(deep=True)
        return True


class InMemoryHistoryRecorder(HistoryRecorder):
    """In-memory implementation of planning history recorder port."""

    def __init__(self, entries: list[dict[str, Any]]):
        self.entries = entries

    def record_planning(
        self,
        *,
        actor_id: str,
        channel: HistoryChannel,
        message: str,
        recipe_id: UUID,
        structured: dict[str, Any],
    ) -> None:
        self.entries.append(
            {
                "actor_id": actor_id,
                "channel": channel,
                "kind": "planning",
                "message": message,
                "recipe_id": recipe_id,
                "structured": structured,
            }
        )


class InMemoryPlanningSession(PlanningSession):
    """In-memory planning session with savepoint rollback emulation."""

    def __init__(self, uow: MemoryPlanningUnitOfWork):
        self._uow = uow
        self._proposals = InMemoryProposalRepository(uow.proposals)
        self._recipes = InMemoryRecipeReader(uow.recipes)
        self._meals = InMemoryMealWriter(uow.meals)
        self._history = InMemoryHistoryRecorder(uow.history_entries)

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
        saved_meals = {mid: meal.model_copy(deep=True) for mid, meal in self._uow.meals.items()}
        saved_history = list(self._uow.history_entries)
        try:
            yield
        except Exception:
            self._uow.meals.clear()
            self._uow.meals.update(saved_meals)
            self._uow.history_entries.clear()
            self._uow.history_entries.extend(saved_history)
            raise


class MemoryPlanningUnitOfWork(PlanningUnitOfWork):
    """In-memory Unit of Work for pure isolated unit testing."""

    def __init__(
        self,
        *,
        recipes: dict[UUID, Recipe] | None = None,
        proposals: dict[UUID, ProposalRecord] | None = None,
        meals: dict[UUID, Meal] | None = None,
        history_entries: list[dict[str, Any]] | None = None,
    ):
        self.recipes = recipes if recipes is not None else {}
        self.proposals = proposals if proposals is not None else {}
        self.meals = meals if meals is not None else {}
        self.history_entries = history_entries if history_entries is not None else []

    def add_recipe(self, recipe: Recipe) -> None:
        self.recipes[recipe.id] = recipe

    @contextmanager
    def session(self) -> Generator[InMemoryPlanningSession]:
        yield InMemoryPlanningSession(self)


class MemorySingleRecipeReader(SingleRecipeReader):
    """In-memory single-recipe reader for testing MealAgendaService without a DB."""

    def __init__(self, recipes: dict[UUID, Recipe] | None = None) -> None:
        self.recipes = recipes if recipes is not None else {}

    def get_recipe(self, recipe_id: UUID) -> Recipe:
        if recipe_id not in self.recipes:
            raise KeyError(str(recipe_id))
        return self.recipes[recipe_id].model_copy(deep=True)


class MemoryMealAgendaRepository(MealAgendaRepository):
    """In-memory meal-agenda repository preserving insertion order and history semantics."""

    def __init__(
        self,
        meals: dict[UUID, Meal] | None = None,
        history_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        self.meals = meals if meals is not None else {}
        self.history_entries = history_entries if history_entries is not None else []

    def save_meal(self, meal: Meal) -> None:
        if meal.id in self.meals:
            raise ValueError("A meal with this ID already exists")
        self.meals[meal.id] = meal.model_copy(deep=True)
        self.history_entries.append(
            {
                "kind": "planning",
                "message": "Saved meal",
                "recipe_id": meal.recipe_id,
                "entity_type": "meal",
                "entity_id": str(meal.id),
            }
        )

    def get_meal(self, meal_id: UUID) -> Meal:
        if meal_id not in self.meals:
            raise KeyError(str(meal_id))
        return self.meals[meal_id].model_copy(deep=True)

    def update_meal(self, meal: Meal) -> None:
        if meal.id not in self.meals:
            raise KeyError(str(meal.id))
        self.meals[meal.id] = meal.model_copy(deep=True)
        self.history_entries.append(
            {
                "kind": "planning",
                "message": "Updated planned meal",
                "recipe_id": meal.recipe_id,
                "entity_type": "meal",
                "entity_id": str(meal.id),
            }
        )

    def list_meals(self) -> list[Meal]:
        return [meal.model_copy(deep=True) for meal in self.meals.values()]

    def delete_meal(self, meal_id: UUID) -> None:
        if meal_id not in self.meals:
            raise KeyError(str(meal_id))
        del self.meals[meal_id]

    def has_meals_for_recipe(self, recipe_id: UUID) -> bool:
        return any(meal.recipe_id == recipe_id for meal in self.meals.values())
