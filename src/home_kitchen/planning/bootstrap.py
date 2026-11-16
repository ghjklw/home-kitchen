"""Bootstrap and dependency injection wiring for meal planning proposals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from home_kitchen.planning.adapters.memory import MemoryPlanningUnitOfWork
from home_kitchen.planning.adapters.postgres import PostgresPlanningUnitOfWork
from home_kitchen.planning.application.service import PlanningProposalService

if TYPE_CHECKING:
    from home_kitchen.history import HistoryRepository
    from home_kitchen.service import KitchenService


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
