"""Real PostgreSQL guarantees for reviewable multi-meal proposals."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from home_kitchen.history import HistoryRepository
from home_kitchen.models import Ingredient, Meal, MealCreate, RecipeCreate
from home_kitchen.planning_proposals import (
    PlanningProposalService,
    ProposalActorMismatch,
    ProposalConflict,
    ProposalExpired,
)
from home_kitchen.service import KitchenService


@pytest.fixture
def setup(test_database_url):
    kitchen = KitchenService(test_database_url)
    recipe = kitchen.create_recipe(
        RecipeCreate(
            title="Soup",
            servings=2,
            ingredients=[Ingredient(name="Carrot", quantity=2)],
            instructions=["Cook"],
        )
    )
    service = PlanningProposalService(kitchen)
    start = date.today() + timedelta(days=40)
    proposed = [
        MealCreate(date=start, slot="breakfast", recipe_id=recipe.id, servings=1),
        MealCreate(date=start, slot="lunch", recipe_id=recipe.id, servings=3),
    ]
    yield kitchen, recipe, service, proposed
    kitchen.store.engine.dispose()


def test_review_actor_and_idempotent_atomic_apply(setup):
    kitchen, recipe, service, proposed = setup
    proposal = service.create("alice", proposed)
    assert proposal.status == "pending"
    assert len(proposal.recipe_fingerprints[str(recipe.id)]) == 64
    assert kitchen.store.list("meal", Meal) == []
    with pytest.raises(ValueError, match="reviewed=true"):
        service.confirm(proposal.id, "alice", reviewed=False)
    with pytest.raises(ProposalActorMismatch):
        service.confirm(proposal.id, "bob", reviewed=True)
    with pytest.raises(ProposalActorMismatch):
        service.get(proposal.id, "bob")
    applied = service.confirm(proposal.id, "alice", reviewed=True)
    assert applied.status == "applied"
    assert len(applied.applied_meal_ids) == 2
    assert service.confirm(proposal.id, "alice", reviewed=True) == applied
    entries = HistoryRepository(kitchen.store.engine).list()
    planning = [entry for entry in entries if entry.kind == "planning"]
    assert len(planning) == 2
    assert all(entry.actor_id == "alice" for entry in planning)
    assert planning[0].structured["proposal_id"] == str(proposal.id)


def test_pending_legacy_slot_proposal_json_still_applies(setup):
    kitchen, _, service, proposed = setup
    proposal = service.create("alice", proposed[:1])
    old_payload = [
        {
            "date": proposed[0].date.isoformat(),
            "slot": "breakfast",
            "recipe_id": str(proposed[0].recipe_id),
            "servings": proposed[0].servings,
        }
    ]
    with kitchen.store.connection() as connection:
        connection.execute(
            text("UPDATE kitchen_plan_proposals SET meals = CAST(:meals AS jsonb) WHERE id = :id"),
            {"meals": json.dumps(old_payload), "id": proposal.id},
        )
    loaded = service.get(proposal.id, "alice")
    assert loaded.meals[0].label == "Frokost"
    applied = service.confirm(proposal.id, "alice", reviewed=True)
    assert applied.status == "applied"
    assert kitchen.store.get("meal", applied.applied_meal_ids[0], Meal).label == "Frokost"


def test_duplicate_date_recipe_and_bounded_input(setup):
    kitchen, _, service, proposed = setup
    duplicate = service.create("alice", [proposed[0], proposed[0]])
    assert len(duplicate.meals) == 2
    with pytest.raises(ValueError, match="between"):
        service.create("alice", [])
    with pytest.raises(ValueError, match="range"):
        service.create(
            "alice",
            [
                proposed[0],
                proposed[1].model_copy(update={"date": proposed[0].date + timedelta(days=366)}),
            ],
        )
    kitchen.plan_meal(proposed[0])
    additive = service.create("alice", proposed)
    assert len(additive.meals) == 2


def test_recipe_change_and_expiry_are_durable_without_meals(setup):
    kitchen, recipe, service, proposed = setup
    changed = service.create("alice", proposed)
    kitchen.update_recipe(
        recipe.id,
        RecipeCreate(**recipe.model_dump(exclude={"id"})).model_copy(
            update={"title": "Changed soup"}
        ),
    )
    with pytest.raises(ProposalConflict, match="recipes changed"):
        service.confirm(changed.id, "alice", reviewed=True)
    assert service.get(changed.id, "alice").status == "stale"
    assert service.get(changed.id, "alice").attempt_count == 1
    assert kitchen.list_meals(proposed[0].date, proposed[-1].date) == []
    expired = service.create("alice", proposed)
    with kitchen.store.connection() as connection:
        connection.execute(
            text(
                "UPDATE kitchen_plan_proposals SET expires_at = now() "
                "- interval '1 minute' WHERE id = :id"
            ),
            {"id": expired.id},
        )
    with pytest.raises(ProposalExpired):
        service.confirm(expired.id, "alice", reviewed=True)
    assert service.get(expired.id, "alice").status == "expired"
    assert kitchen.list_meals(proposed[0].date, proposed[-1].date) == []


def test_existing_date_entries_do_not_block_reviewed_additions(setup):
    kitchen, _, service, proposed = setup
    proposal = service.create("alice", proposed)
    existing = kitchen.plan_meal(proposed[0])
    applied = service.confirm(proposal.id, "alice", reviewed=True)
    assert applied.status == "applied"
    planned = kitchen.list_meals(proposed[0].date, proposed[-1].date)
    assert len(planned) == 3
    assert existing.id in {meal.id for meal in planned}


def test_concurrent_confirmation_and_overlapping_proposals(setup):
    kitchen, _, service, proposed = setup
    first = service.create("alice", proposed)
    second = service.create("alice", proposed)

    def apply(proposal_id):
        try:
            return service.confirm(proposal_id, "alice", reviewed=True)
        except ProposalConflict:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(apply, [first.id, second.id, first.id]))
    winners = [result for result in results if result is not None]
    assert winners
    assert len({tuple(result.applied_meal_ids) for result in winners}) == 2
    assert len(kitchen.list_meals(proposed[0].date, proposed[-1].date)) == 4
    assert len([entry for entry in service.history.list() if entry.kind == "planning"]) == 4


def test_history_failure_rolls_back_all_writes(setup, monkeypatch):
    kitchen, _, service, proposed = setup
    proposal = service.create("alice", proposed)
    original = service.history.record
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("history unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(service.history, "record", fail_second)
    with pytest.raises(RuntimeError, match="history unavailable"):
        service.confirm(proposal.id, "alice", reviewed=True)
    assert kitchen.list_meals(proposed[0].date, proposed[-1].date) == []
    assert service.get(proposal.id, "alice").status == "pending"
    assert not [entry for entry in service.history.list() if entry.kind == "planning"]


def test_deleted_recipe_is_stale(setup):
    kitchen, recipe, service, proposed = setup
    proposal = service.create("alice", proposed)
    kitchen.delete_recipe(recipe.id)
    with pytest.raises(ProposalConflict, match="unavailable"):
        service.confirm(proposal.id, "alice", reviewed=True)
    assert service.get(proposal.id, "alice").status == "stale"


def test_ttl_and_limits_are_service_configuration(setup):
    kitchen, _, _, _ = setup
    for arguments in (
        {"ttl_seconds": 0},
        {"ttl_seconds": 86401},
        {"max_meals": 91},
        {"max_range_days": 367},
    ):
        with pytest.raises(ValueError):
            PlanningProposalService(kitchen, **arguments)
