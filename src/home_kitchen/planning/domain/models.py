"""Domain models and value objects for meal planning proposals."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from home_kitchen.models import MealCreate

ProposalStatus = Literal["pending", "applied", "expired", "stale"]


class ProposalRead(BaseModel):
    id: UUID
    meals: list[MealCreate]
    recipe_fingerprints: dict[str, str]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: ProposalStatus
    applied_meal_ids: list[UUID]
    attempt_count: int
    last_error: str | None = None


class ProposalRecord(BaseModel):
    id: UUID
    actor_id: str
    meals: list[dict[str, Any]]
    recipe_fingerprints: dict[str, str]
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: ProposalStatus
    applied_meal_ids: list[UUID] = Field(default_factory=list)
    attempt_count: int = 0
    last_error: str | None = None
