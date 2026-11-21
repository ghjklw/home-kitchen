"""Domain rules and validation invariants for meal planning proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from home_kitchen.models import Meal, MealCreate, Recipe


def fingerprint_recipe(recipe: Recipe) -> str:
    """Calculate deterministic SHA-256 fingerprint for recipe state."""
    canonical = json.dumps(recipe.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def fingerprint_meal(meal: Meal) -> str:
    """Calculate deterministic SHA-256 fingerprint for an existing planned meal."""
    canonical = json.dumps(meal.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# Backward-compatible alias
_fingerprint = fingerprint_recipe


def validate_actor(actor_id: str) -> None:
    """Validate authenticated actor requirement."""
    if not actor_id or not actor_id.strip():
        raise ValueError("An authenticated actor is required")


def validate_proposal_bounds(
    meals: Sequence[MealCreate], *, max_meals: int = 90, max_range_days: int = 366
) -> None:
    """Enforce bounds on proposal meal counts, calendar date ranges, and unique edit targets."""
    if not 1 <= len(meals) <= max_meals:
        raise ValueError(f"A proposal must contain between 1 and {max_meals} meals")
    dates = [meal.date for meal in meals]
    if (max(dates) - min(dates)).days + 1 > max_range_days:
        raise ValueError(f"Proposal date range must be at most {max_range_days} days")
    seen_edit_ids: set[UUID] = set()
    for meal in meals:
        meal_id = getattr(meal, "meal_id", None)
        if meal_id is not None:
            if meal_id in seen_edit_ids:
                raise ValueError(f"A proposal cannot edit meal ID {meal_id} more than once")
            seen_edit_ids.add(meal_id)


def validate_service_bounds(
    *, ttl_seconds: int = 900, max_meals: int = 90, max_range_days: int = 366
) -> None:
    """Validate proposal service configuration bounds."""
    if not 1 <= ttl_seconds <= 86400:
        raise ValueError("Proposal TTL must be between 1 second and 24 hours")
    if not 1 <= max_meals <= 90 or not 1 <= max_range_days <= 366:
        raise ValueError("Proposal bounds must be at most 90 meals and 366 days")


def validate_date_range(start: date, end: date) -> None:
    """Ensure calendar date ranges are valid (end on or after start)."""
    if end < start:
        raise ValueError("end must be on or after start")
