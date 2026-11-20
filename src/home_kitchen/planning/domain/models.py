"""Domain models and value objects for meal planning proposals."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, computed_field, model_validator

from home_kitchen.models import Meal, MealEntry

ProposalStatus = Literal["pending", "applied", "expired", "stale"]


class ProposalRead(BaseModel):
    id: UUID
    meal_entries: list[MealEntry]
    recipe_fingerprints: dict[str, str]
    meal_fingerprints: dict[str, str] = Field(default_factory=dict)
    existing_meals: dict[str, Meal] = Field(default_factory=dict)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: ProposalStatus
    applied_meal_ids: list[UUID]
    attempt_count: int
    last_error: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_meal_entries(cls, value: Any) -> Any:
        if isinstance(value, dict):
            data = dict(value)
            if "meal_entries" not in data and "meals" in data:
                data["meal_entries"] = data.pop("meals")
            else:
                data.pop("meals", None)
            if "existing_meals" not in data and "target_meals" in data:
                data["existing_meals"] = data.pop("target_meals")
            data.pop("additions", None)
            data.pop("edits", None)
            return data
        return value

    @computed_field
    @property
    def meals(self) -> list[MealEntry]:
        """Backward-compatible alias for all proposed meal entries."""
        return self.meal_entries

    @computed_field
    @property
    def additions(self) -> list[MealEntry]:
        """Proposed new meal entries (meal_id is None)."""
        return [entry for entry in self.meal_entries if entry.meal_id is None]

    @computed_field
    @property
    def edits(self) -> list[MealEntry]:
        """Proposed edits targeting explicit existing meal IDs."""
        return [entry for entry in self.meal_entries if entry.meal_id is not None]


class ProposalRecord(BaseModel):
    id: UUID
    actor_id: str
    meals: list[dict[str, Any]]
    recipe_fingerprints: dict[str, str]
    meal_fingerprints: dict[str, str] = Field(default_factory=dict)
    target_meals: dict[str, dict[str, Any]] = Field(default_factory=dict)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    status: ProposalStatus
    applied_meal_ids: list[UUID] = Field(default_factory=list)
    attempt_count: int = 0
    last_error: str | None = None
