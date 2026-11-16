"""Ports for atomic transaction management and session contexts."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from home_kitchen.planning.ports.readers import RecipeReader
from home_kitchen.planning.ports.recorders import HistoryRecorder
from home_kitchen.planning.ports.repository import ProposalRepository
from home_kitchen.planning.ports.writers import MealWriter


class PlanningSession(Protocol):
    """Transactional session providing access to ports and nested savepoint guards."""

    @property
    def proposals(self) -> ProposalRepository: ...

    @property
    def recipes(self) -> RecipeReader: ...

    @property
    def meals(self) -> MealWriter: ...

    @property
    def history(self) -> HistoryRecorder: ...

    def savepoint(self) -> AbstractContextManager[None]: ...


class PlanningUnitOfWork(Protocol):
    """Port for establishing transactional planning sessions."""

    def session(self) -> AbstractContextManager[PlanningSession]: ...
