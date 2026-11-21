"""Bootstrap and dependency injection wiring for meal planning proposals."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from home_kitchen.planning.adapters.memory import (
    MemoryMealAgendaRepository,
    MemoryPlanningUnitOfWork,
    MemorySingleRecipeReader,
)
from home_kitchen.planning.adapters.postgres import (
    PostgresMealAgendaRepository,
    PostgresPlanningUnitOfWork,
    PostgresSingleRecipeReader,
)
from home_kitchen.planning.application.agenda import MealAgendaService
from home_kitchen.planning.application.service import PlanningProposalService
from home_kitchen.planning.ports.readers import SingleRecipeReader
from home_kitchen.planning.ports.writers import MealAgendaRepository

if TYPE_CHECKING:
    from home_kitchen.history import HistoryRepository
    from home_kitchen.models import Meal, Recipe
    from home_kitchen.service import KitchenService


def create_planning_uow_for_kitchen(
    kitchen: KitchenService,
    history: HistoryRepository | None = None,
) -> tuple[PostgresPlanningUnitOfWork, Any]:
    """Build a PostgreSQL planning UoW and history repository from a legacy KitchenService."""
    from home_kitchen.history import HistoryRepository

    resolved_history = history or HistoryRepository(kitchen.store.engine)
    uow = PostgresPlanningUnitOfWork(kitchen.store, resolved_history)
    return uow, resolved_history


def create_planning_proposal_service(
    kitchen: KitchenService,
    history: HistoryRepository | None = None,
    *,
    ttl_seconds: int = 900,
    max_meals: int = 90,
    max_range_days: int = 366,
) -> PlanningProposalService:
    """Bootstrap a production PlanningProposalService backed by PostgreSQL."""
    from home_kitchen.history import HistoryRepository

    resolved_history = history or HistoryRepository(kitchen.store.engine)
    uow = PostgresPlanningUnitOfWork(kitchen.store, resolved_history)
    service = PlanningProposalService(
        uow,
        resolved_history,
        ttl_seconds=ttl_seconds,
        max_meals=max_meals,
        max_range_days=max_range_days,
    )
    service.kitchen = kitchen
    return service


def create_in_memory_planning_proposal_service(
    uow: MemoryPlanningUnitOfWork | None = None,
    *,
    ttl_seconds: int = 900,
    max_meals: int = 90,
    max_range_days: int = 366,
) -> tuple[PlanningProposalService, MemoryPlanningUnitOfWork]:
    """Bootstrap an in-memory PlanningProposalService for testing without database."""
    resolved_uow = uow if uow is not None else MemoryPlanningUnitOfWork()
    service = PlanningProposalService(
        resolved_uow,
        ttl_seconds=ttl_seconds,
        max_meals=max_meals,
        max_range_days=max_range_days,
    )
    return service, resolved_uow


def create_meal_agenda_service(
    engine: Any,
    history: Any = None,
    *,
    meals: MealAgendaRepository | None = None,
    recipes: SingleRecipeReader | None = None,
) -> MealAgendaService:
    """Bootstrap a production MealAgendaService backed by PostgreSQL."""
    resolved_meals = meals or PostgresMealAgendaRepository(engine, history)
    resolved_recipes = recipes or PostgresSingleRecipeReader(engine)
    return MealAgendaService(resolved_meals, resolved_recipes)


def create_in_memory_meal_agenda_service(
    *,
    recipes: dict[Any, Recipe] | None = None,
    meals: dict[Any, Meal] | None = None,
) -> tuple[MealAgendaService, MemoryMealAgendaRepository, MemorySingleRecipeReader]:
    """Bootstrap an in-memory MealAgendaService for testing without a database."""
    recipe_reader = MemorySingleRecipeReader(recipes)
    meal_repo = MemoryMealAgendaRepository(meals)
    return MealAgendaService(meal_repo, recipe_reader), meal_repo, recipe_reader
