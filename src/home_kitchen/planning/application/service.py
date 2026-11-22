"""Application service orchestrating meal planning proposal use cases."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from home_kitchen.context import history_context
from home_kitchen.models import Meal, MealCreate, MealEntry
from home_kitchen.planning.domain.exceptions import (
    ProposalConflict,
    ProposalExpired,
)
from home_kitchen.planning.domain.models import ProposalRead, ProposalRecord, ProposalStatus
from home_kitchen.planning.domain.rules import (
    fingerprint_meal,
    fingerprint_recipe,
    validate_actor,
    validate_proposal_bounds,
    validate_service_bounds,
)
from home_kitchen.planning.ports.unit_of_work import PlanningUnitOfWork


class PlanningProposalService:
    """Cohesive application service for creating, reading, and confirming meal proposals."""

    def __init__(
        self,
        uow_or_kitchen: PlanningUnitOfWork | Any,
        history: Any = None,
        *,
        ttl_seconds: int = 900,
        max_meals: int = 90,
        max_range_days: int = 366,
    ):
        validate_service_bounds(
            ttl_seconds=ttl_seconds,
            max_meals=max_meals,
            max_range_days=max_range_days,
        )
        self.ttl_seconds = ttl_seconds
        self.max_meals = max_meals
        self.max_range_days = max_range_days

        if callable(getattr(uow_or_kitchen, "session", None)):
            self._uow: PlanningUnitOfWork = cast(PlanningUnitOfWork, uow_or_kitchen)
            self.kitchen: Any = None
            self.history: Any = history
        else:
            from home_kitchen.planning.bootstrap import create_planning_uow_for_kitchen

            self.kitchen = uow_or_kitchen
            self._uow, self.history = create_planning_uow_for_kitchen(
                cast(Any, uow_or_kitchen), history
            )

    # Backward-compatible static helpers
    _actor = staticmethod(validate_actor)
    _read = staticmethod(ProposalRead.model_validate)

    def create(self, actor_id: str, meals: Sequence[MealEntry | MealCreate]) -> ProposalRead:
        """Create a new reviewable proposal with recipe and target-meal fingerprints."""
        validate_actor(actor_id)
        proposed = [MealEntry.model_validate(meal) for meal in meals]
        validate_proposal_bounds(
            proposed, max_meals=self.max_meals, max_range_days=self.max_range_days
        )
        now = datetime.now(UTC)

        with self._uow.session() as session:
            recipe_ids = sorted({meal.recipe_id for meal in proposed}, key=str)
            recipes = session.recipes.get_recipes(recipe_ids, lock=False)
            for recipe_id in recipe_ids:
                if recipe_id not in recipes:
                    raise ProposalConflict(
                        f"Recipe {recipe_id} is unavailable. Create and review a new proposal."
                    )
            edit_ids = sorted(
                {entry.meal_id for entry in proposed if entry.meal_id is not None}, key=str
            )
            target_meals = session.meals.get_meals(edit_ids, lock=False) if edit_ids else {}
            for meal_id in edit_ids:
                if meal_id not in target_meals:
                    raise ProposalConflict(
                        f"Meal {meal_id} is unavailable. Create and review a new proposal."
                    )
            fingerprints = {
                str(rid): fingerprint_recipe(recipes[rid])
                for rid in sorted(recipes.keys(), key=str)
            }
            meal_fingerprints = {str(mid): fingerprint_meal(target_meals[mid]) for mid in edit_ids}
            target_snapshots = {
                str(mid): target_meals[mid].model_dump(mode="json") for mid in edit_ids
            }
            record = ProposalRecord(
                id=uuid4(),
                actor_id=actor_id,
                meals=[entry.model_dump(mode="json") for entry in proposed],
                recipe_fingerprints=fingerprints,
                meal_fingerprints=meal_fingerprints,
                target_meals=target_snapshots,
                created_at=now,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
                status="pending",
                applied_meal_ids=[],
                attempt_count=0,
                last_error=None,
            )
            return session.proposals.save(record)

    def get(self, proposal_id: UUID, actor_id: str) -> ProposalRead:
        """Retrieve an existing proposal enforcing actor isolation."""
        validate_actor(actor_id)
        with self._uow.session() as session:
            return session.proposals.get(proposal_id, actor_id, lock=False)

    def confirm(self, proposal_id: UUID, actor_id: str, *, reviewed: bool) -> ProposalRead:
        """Atomically confirm and apply a reviewed proposal with freshness and idempotency guards."""
        if reviewed is not True:
            raise ValueError("Explicit reviewed=true is required to apply the whole proposal")
        validate_actor(actor_id)

        failure: Exception | None = None
        with self._uow.session() as session:
            proposal = session.proposals.get(proposal_id, actor_id, lock=True)
            if proposal.status == "applied":
                return proposal

            attempts = proposal.attempt_count + 1
            status: ProposalStatus = "stale"
            try:
                if proposal.status == "expired" or proposal.expires_at <= datetime.now(UTC):
                    status = "expired"
                    raise ProposalExpired("Proposal expired. Create and review a new proposal.")
                if proposal.status != "pending":
                    raise ProposalConflict("Proposal is stale. Create and review a new proposal.")

                # Savepoint isolates meal writes and history events so that conflicts
                # roll back cleanly while the outer transaction records the attempt durably.
                with session.savepoint():
                    validate_proposal_bounds(
                        proposal.meal_entries,
                        max_meals=self.max_meals,
                        max_range_days=self.max_range_days,
                    )
                    recipe_ids = sorted(
                        {entry.recipe_id for entry in proposal.meal_entries}, key=str
                    )
                    recipes = session.recipes.get_recipes(recipe_ids, lock=True)
                    for recipe_id in recipe_ids:
                        if recipe_id not in recipes:
                            raise ProposalConflict(
                                f"Recipe {recipe_id} is unavailable. Create and review a new proposal."
                            )
                    fingerprints = {
                        str(rid): fingerprint_recipe(recipes[rid])
                        for rid in sorted(recipes.keys(), key=str)
                    }
                    if fingerprints != proposal.recipe_fingerprints:
                        raise ProposalConflict(
                            "Source recipes changed. Create and review a new proposal."
                        )

                    edit_ids = sorted(
                        {
                            entry.meal_id
                            for entry in proposal.meal_entries
                            if entry.meal_id is not None
                        },
                        key=str,
                    )
                    target_meals = session.meals.get_meals(edit_ids, lock=True) if edit_ids else {}
                    for meal_id in edit_ids:
                        if meal_id not in target_meals:
                            raise ProposalConflict(
                                f"Meal {meal_id} is unavailable. Create and review a new proposal."
                            )
                    if proposal.meal_fingerprints:
                        current_meal_fingerprints = {
                            str(mid): fingerprint_meal(target_meals[mid]) for mid in edit_ids
                        }
                        if current_meal_fingerprints != proposal.meal_fingerprints:
                            raise ProposalConflict(
                                "Target meals changed. Create and review a new proposal."
                            )

                    if proposal.expires_at <= datetime.now(UTC):
                        status = "expired"
                        raise ProposalExpired("Proposal expired. Create and review a new proposal.")

                    applied: list[Meal] = []
                    channel = history_context.get().channel if history_context.get() else "api"
                    for entry in proposal.meal_entries:
                        payload = entry.model_dump(exclude={"meal_id"})
                        if entry.meal_id is None:
                            meal = Meal(id=uuid4(), **payload)
                            if not session.meals.insert_meal(meal):
                                raise ProposalConflict(
                                    f"Meal ID {meal.id} already exists. Create and review a new proposal."
                                )
                            action = "add"
                            history_message = "Applied reviewed meal plan"
                        else:
                            current_meal = target_meals[entry.meal_id]
                            meal = Meal(
                                id=entry.meal_id,
                                version=current_meal.version,
                                **payload,
                            )
                            if not session.meals.update_meal(meal):
                                raise ProposalConflict(
                                    f"Meal {meal.id} is unavailable. Create and review a new proposal."
                                )
                            action = "edit"
                            history_message = "Applied reviewed meal plan edit"
                        applied.append(meal)
                        session.history.record_planning(
                            actor_id=actor_id,
                            channel=channel,
                            message=history_message,
                            recipe_id=meal.recipe_id,
                            structured={
                                "proposal_id": str(proposal.id),
                                "action": action,
                                "entity_type": "meal",
                                "entity_id": str(meal.id),
                                "data": meal.model_dump(mode="json"),
                            },
                        )

                    proposal.applied_meal_ids = [meal.id for meal in applied]
                    status = "applied"
            except ProposalConflict as exc:
                failure = exc

            result = session.proposals.update_status(
                proposal.id,
                status=status,
                attempt_count=attempts,
                last_error=str(failure) if failure else None,
                applied_meal_ids=proposal.applied_meal_ids,
            )

        if failure:
            raise failure
        return result
