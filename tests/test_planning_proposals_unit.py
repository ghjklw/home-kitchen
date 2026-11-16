"""Unit tests proving the extracted meal planning proposal use case runs in-memory without DB."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from home_kitchen.models import Ingredient, MealCreate, Recipe, RecipeSource
from home_kitchen.planning import (
    PlanningProposalService,
    ProposalActorMismatch,
    ProposalConflict,
    ProposalExpired,
    ProposalNotFound,
    create_in_memory_planning_proposal_service,
)


def _sample_recipe(title: str = "Soup") -> Recipe:
    return Recipe(
        id=uuid4(),
        title=title,
        servings=2,
        ingredients=[Ingredient(name="Carrot", quantity=2)],
        instructions=["Simmer ingredients"],
        source=RecipeSource.MANUAL,
    )


def test_in_memory_proposal_lifecycle_and_confirmation():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(recipe)

    start = date.today() + timedelta(days=5)
    proposed = [
        MealCreate(date=start, slot="breakfast", recipe_id=recipe.id, servings=1),
        MealCreate(date=start, slot="lunch", recipe_id=recipe.id, servings=3),
    ]

    proposal = service.create("alice", proposed)
    assert proposal.status == "pending"
    assert len(proposal.recipe_fingerprints[str(recipe.id)]) == 64
    assert proposal.attempt_count == 0
    assert proposal.last_error is None
    assert proposal.applied_meal_ids == []
    assert len(uow.meals) == 0

    # Actor isolation checks
    with pytest.raises(ProposalActorMismatch):
        service.get(proposal.id, "bob")
    with pytest.raises(ProposalNotFound):
        service.get(uuid4(), "alice")

    # Explicit reviewed=True requirement
    with pytest.raises(ValueError, match="reviewed=true"):
        service.confirm(proposal.id, "alice", reviewed=False)
    with pytest.raises(ProposalActorMismatch):
        service.confirm(proposal.id, "bob", reviewed=True)

    # Valid confirmation
    applied = service.confirm(proposal.id, "alice", reviewed=True)
    assert applied.status == "applied"
    assert len(applied.applied_meal_ids) == 2
    assert len(uow.meals) == 2
    assert len(uow.history_entries) == 2

    saved_meals = list(uow.meals.values())
    assert all(m.recipe_id == recipe.id for m in saved_meals)
    assert all(entry["actor_id"] == "alice" for entry in uow.history_entries)
    assert all(
        entry["structured"]["proposal_id"] == str(proposal.id) for entry in uow.history_entries
    )

    # Idempotent re-confirmation returns same result without duplicate writes
    assert service.confirm(proposal.id, "alice", reviewed=True) == applied
    assert len(uow.meals) == 2
    assert len(uow.history_entries) == 2


def test_in_memory_proposal_expiration():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service(ttl_seconds=1)
    uow.add_recipe(recipe)

    start = date.today() + timedelta(days=1)
    proposal = service.create(
        "alice", [MealCreate(date=start, recipe_id=recipe.id, servings=2, label="Middag")]
    )

    # Manually expire the proposal
    uow.proposals[proposal.id].expires_at = datetime.now(UTC) - timedelta(seconds=10)

    with pytest.raises(ProposalExpired):
        service.confirm(proposal.id, "alice", reviewed=True)

    updated = service.get(proposal.id, "alice")
    assert updated.status == "expired"
    assert updated.attempt_count == 1
    assert updated.last_error is not None
    assert len(uow.meals) == 0


def test_in_memory_recipe_modification_triggers_conflict_and_rolls_back():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(recipe)

    start = date.today() + timedelta(days=1)
    proposal = service.create(
        "alice", [MealCreate(date=start, recipe_id=recipe.id, servings=2, label="Middag")]
    )

    # Modify the recipe in the repository so its fingerprint changes
    uow.recipes[recipe.id] = recipe.model_copy(update={"title": "Modified Soup"})

    with pytest.raises(ProposalConflict, match="Source recipes changed"):
        service.confirm(proposal.id, "alice", reviewed=True)

    updated = service.get(proposal.id, "alice")
    assert updated.status == "stale"
    assert updated.attempt_count == 1
    assert "Source recipes changed" in (updated.last_error or "")
    # Savepoint ensures atomic rollback of meals and history
    assert len(uow.meals) == 0
    assert len(uow.history_entries) == 0


def test_in_memory_recipe_deletion_triggers_conflict_and_rolls_back():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(recipe)

    start = date.today() + timedelta(days=1)
    proposal = service.create(
        "alice", [MealCreate(date=start, recipe_id=recipe.id, servings=2, label="Middag")]
    )

    # Delete the recipe
    del uow.recipes[recipe.id]

    with pytest.raises(ProposalConflict, match="unavailable"):
        service.confirm(proposal.id, "alice", reviewed=True)

    updated = service.get(proposal.id, "alice")
    assert updated.status == "stale"
    assert updated.attempt_count == 1
    assert len(uow.meals) == 0


def test_in_memory_stale_proposal_cannot_be_confirmed():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(recipe)

    start = date.today() + timedelta(days=1)
    proposal = service.create(
        "alice", [MealCreate(date=start, recipe_id=recipe.id, servings=2, label="Middag")]
    )

    # Mark proposal stale
    uow.proposals[proposal.id].status = "stale"

    with pytest.raises(ProposalConflict, match="Proposal is stale"):
        service.confirm(proposal.id, "alice", reviewed=True)


def test_in_memory_bounds_and_actor_validation():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(recipe)

    start = date.today()
    valid_meal = MealCreate(date=start, recipe_id=recipe.id, servings=1, label="Test")

    # Empty actor
    with pytest.raises(ValueError, match="authenticated actor"):
        service.create("", [valid_meal])
    with pytest.raises(ValueError, match="authenticated actor"):
        service.get(uuid4(), "")
    with pytest.raises(ValueError, match="authenticated actor"):
        service.confirm(uuid4(), "", reviewed=True)

    # Meal bounds
    with pytest.raises(ValueError, match="between 1 and 90"):
        service.create("alice", [])
    with pytest.raises(ValueError, match="between 1 and 90"):
        service.create("alice", [valid_meal] * 91)

    # Date range bounds
    out_of_range_meal = MealCreate(
        date=start + timedelta(days=367), recipe_id=recipe.id, servings=1, label="Test"
    )
    with pytest.raises(ValueError, match="at most 366 days"):
        service.create("alice", [valid_meal, out_of_range_meal])

    # Service configuration bounds
    with pytest.raises(ValueError, match="TTL"):
        PlanningProposalService(uow, ttl_seconds=0)
    with pytest.raises(ValueError, match="TTL"):
        PlanningProposalService(uow, ttl_seconds=90000)
    with pytest.raises(ValueError, match="bounds"):
        PlanningProposalService(uow, max_meals=0)
    with pytest.raises(ValueError, match="bounds"):
        PlanningProposalService(uow, max_range_days=0)


def test_in_memory_legacy_slot_proposal():
    recipe = _sample_recipe()
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(recipe)

    start = date.today()
    proposed = [MealCreate(date=start, slot="breakfast", recipe_id=recipe.id, servings=2)]
    proposal = service.create("alice", proposed)

    # Check that legacy slot was converted to label Frokost
    loaded = service.get(proposal.id, "alice")
    assert loaded.meals[0].label == "Frokost"

    applied = service.confirm(proposal.id, "alice", reviewed=True)
    assert applied.status == "applied"
    assert uow.meals[applied.applied_meal_ids[0]].label == "Frokost"
