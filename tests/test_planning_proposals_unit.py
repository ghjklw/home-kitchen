"""Unit tests proving the extracted meal planning proposal use case runs in-memory without DB."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest

from home_kitchen.models import Ingredient, Meal, MealCreate, MealEntry, Recipe, RecipeSource
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


def test_in_memory_proposal_additions_and_explicit_edits_on_shared_date():
    soup = _sample_recipe("Soup")
    pasta = _sample_recipe("Pasta")
    start = date.today() + timedelta(days=3)
    existing_meal = Meal(
        id=uuid4(),
        date=start,
        recipe_id=soup.id,
        servings=2,
        label="Middag",
        scheduled_time=time(17, 0),
    )
    untouched_meal = Meal(
        id=uuid4(),
        date=start,
        recipe_id=pasta.id,
        servings=2,
        label="Frokost",
    )
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(soup)
    uow.add_recipe(pasta)
    uow.meals[existing_meal.id] = existing_meal
    uow.meals[untouched_meal.id] = untouched_meal

    entries = [
        MealEntry(
            meal_id=existing_meal.id,
            date=start,
            recipe_id=pasta.id,
            servings=4,
            label="Middag",
            scheduled_time=time(18, 0),
        ),
        MealEntry(date=start, recipe_id=pasta.id, servings=2, label="Lunsj"),
        MealEntry(date=start + timedelta(days=1), recipe_id=pasta.id, servings=3),
    ]
    proposal = service.create("alice", entries)
    assert len(proposal.additions) == 2
    assert len(proposal.edits) == 1
    assert proposal.edits[0].meal_id == existing_meal.id
    assert proposal.existing_meals[str(existing_meal.id)] == existing_meal
    # Pending proposal does not mutate saved meals
    assert uow.meals[existing_meal.id] == existing_meal
    assert len(uow.meals) == 2

    applied = service.confirm(proposal.id, "alice", reviewed=True)
    assert applied.status == "applied"
    assert len(applied.applied_meal_ids) == 3
    assert len(uow.meals) == 4
    assert uow.meals[existing_meal.id].recipe_id == pasta.id
    assert uow.meals[existing_meal.id].servings == 4
    assert uow.meals[existing_meal.id].scheduled_time == time(18, 0)
    assert uow.meals[untouched_meal.id] == untouched_meal
    actions = [entry["structured"]["action"] for entry in uow.history_entries]
    assert actions == ["edit", "add", "add"]


def test_in_memory_proposal_rollback_on_missing_or_modified_target_meal_id():
    soup = _sample_recipe("Soup")
    pasta = _sample_recipe("Pasta")
    start = date.today() + timedelta(days=4)
    target = Meal(id=uuid4(), date=start, recipe_id=soup.id, servings=2, label="Middag")
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(soup)
    uow.add_recipe(pasta)
    uow.meals[target.id] = target

    # Missing meal_id rejected at creation time without writing
    with pytest.raises(ProposalConflict, match="Meal"):
        service.create(
            "alice",
            [
                MealEntry(date=start, recipe_id=pasta.id, servings=2),
                MealEntry(meal_id=uuid4(), date=start, recipe_id=pasta.id, servings=4),
            ],
        )
    assert list(uow.meals.keys()) == [target.id]

    # Target meal deleted after proposal creation -> confirm fails and rolls back additions
    proposal = service.create(
        "alice",
        [
            MealEntry(date=start, recipe_id=pasta.id, servings=2),
            MealEntry(meal_id=target.id, date=start, recipe_id=pasta.id, servings=4),
        ],
    )
    del uow.meals[target.id]
    with pytest.raises(ProposalConflict, match="unavailable"):
        service.confirm(proposal.id, "alice", reviewed=True)
    assert len(uow.meals) == 0
    assert len(uow.history_entries) == 0
    assert service.get(proposal.id, "alice").status == "stale"

    # Target meal modified concurrently -> confirm fails and rolls back additions
    uow.meals[target.id] = target
    proposal2 = service.create(
        "alice",
        [
            MealEntry(date=start, recipe_id=pasta.id, servings=2),
            MealEntry(meal_id=target.id, date=start, recipe_id=pasta.id, servings=4),
        ],
    )
    modified = target.model_copy(update={"servings": 6})
    uow.meals[target.id] = modified
    with pytest.raises(ProposalConflict, match="Target meals changed"):
        service.confirm(proposal2.id, "alice", reviewed=True)
    assert len(uow.meals) == 1
    assert uow.meals[target.id] == modified
    assert len(uow.history_entries) == 0


def test_in_memory_proposal_atomic_rollback_on_mid_apply_failure_and_cancel():
    soup = _sample_recipe("Soup")
    start = date.today() + timedelta(days=5)
    target = Meal(id=uuid4(), date=start, recipe_id=soup.id, servings=2, label="Middag")
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(soup)
    uow.meals[target.id] = target

    # Duplicate meal_id edit is rejected without mutating saved meals
    with pytest.raises(ValueError, match="more than once"):
        service.create(
            "alice",
            [
                MealEntry(meal_id=target.id, date=start, recipe_id=soup.id, servings=3),
                MealEntry(meal_id=target.id, date=start, recipe_id=soup.id, servings=4),
            ],
        )
    assert uow.meals[target.id].servings == 2

    # Creating a proposal and cancelling (never confirming) leaves saved plan untouched
    cancelled = service.create(
        "alice",
        [
            MealEntry(date=start, recipe_id=soup.id, servings=3),
            MealEntry(meal_id=target.id, date=start, recipe_id=soup.id, servings=5),
        ],
    )
    assert service.get(cancelled.id, "alice").status == "pending"
    assert len(uow.meals) == 1
    assert uow.meals[target.id].servings == 2

    # Inject a failure during history write on the second entry (after addition & edit ran)
    original_record = uow.history_entries.append
    calls = 0

    class FailOnSecondHistory:
        def __get__(self, obj, objtype=None):
            return self

    with pytest.MonkeyPatch.context() as mp:
        from home_kitchen.planning.adapters.memory import InMemoryHistoryRecorder

        def fail_second(self_rec, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("history write failed")
            original_record({"kwargs": kwargs})

        mp.setattr(InMemoryHistoryRecorder, "record_planning", fail_second)
        with pytest.raises(RuntimeError, match="history write failed"):
            service.confirm(cancelled.id, "alice", reviewed=True)

    # Both the addition and the edit rolled back cleanly
    assert len(uow.meals) == 1
    assert uow.meals[target.id].servings == 2
    assert len(uow.history_entries) == 0


def test_rest_router_proposal_additions_edits_validation_and_rollback():
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from home_kitchen.feature_api import MealPlanProposalCreate, feature_router

    with pytest.raises(ValueError, match="Provide meal_entries or meals"):
        MealPlanProposalCreate()
    with pytest.raises(ValueError, match="not both"):
        MealPlanProposalCreate(
            meals=[MealEntry(date=date(2026, 12, 1), recipe_id=uuid4(), servings=2)],
            meal_entries=[MealEntry(date=date(2026, 12, 1), recipe_id=uuid4(), servings=2)],
        )

    soup = _sample_recipe("Soup")
    start = date(2026, 12, 10)
    existing = Meal(id=uuid4(), date=start, recipe_id=soup.id, servings=2, label="Middag")
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(soup)
    uow.meals[existing.id] = existing

    app = FastAPI()

    @app.middleware("http")
    async def attach_actor(request: Request, call_next):
        request.state.actor_id = "alice"
        return await call_next(request)

    features = SimpleNamespace(plan_proposals=service)
    app.include_router(feature_router(features, SimpleNamespace()))  # type: ignore[arg-type]

    with TestClient(app) as client:
        missing_edit = client.post(
            "/api/meal-plan-proposals",
            json={
                "meal_entries": [
                    {
                        "meal_id": str(uuid4()),
                        "date": start.isoformat(),
                        "recipe_id": str(soup.id),
                        "servings": 4,
                    }
                ]
            },
        )
        assert missing_edit.status_code == 409

        created = client.post(
            "/api/meal-plan-proposals",
            json={
                "meal_entries": [
                    {
                        "meal_id": str(existing.id),
                        "date": start.isoformat(),
                        "recipe_id": str(soup.id),
                        "servings": 5,
                        "label": "Middag",
                    },
                    {
                        "meal_id": None,
                        "date": start.isoformat(),
                        "recipe_id": str(soup.id),
                        "servings": 2,
                        "label": "Lunsj",
                    },
                ]
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert len(payload["additions"]) == 1
        assert len(payload["edits"]) == 1
        assert payload["edits"][0]["meal_id"] == str(existing.id)
        assert payload["existing_meals"][str(existing.id)]["servings"] == 2

        confirmed = client.post(
            f"/api/meal-plan-proposals/{payload['id']}/confirm", json={"reviewed": True}
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "applied"
        assert len(uow.meals) == 2
        assert uow.meals[existing.id].servings == 5
        assert uow.meals[existing.id].version == existing.version + 1


@pytest.mark.asyncio
async def test_mcp_tools_support_meal_entries_with_additions_and_edits():
    from types import SimpleNamespace

    from fastmcp import Client, FastMCP

    from home_kitchen.context import HistoryContext, history_context
    from home_kitchen.feature_mcp import install_feature_tools

    soup = _sample_recipe("Soup")
    start = date(2026, 12, 12)
    existing = Meal(id=uuid4(), date=start, recipe_id=soup.id, servings=2, label="Middag")
    service, uow = create_in_memory_planning_proposal_service()
    uow.add_recipe(soup)
    uow.meals[existing.id] = existing

    mcp = FastMCP("test")
    install_feature_tools(mcp, SimpleNamespace(plan_proposals=service), SimpleNamespace())  # type: ignore[arg-type]

    token = history_context.set(HistoryContext(actor_id="alice", channel="mcp"))
    try:
        async with Client(mcp) as client:
            proposed = await client.call_tool(
                "propose_meal_plan",
                {
                    "meal_entries": [
                        {
                            "meal_id": str(existing.id),
                            "date": start.isoformat(),
                            "recipe_id": str(soup.id),
                            "servings": 4,
                        },
                        {
                            "meal_id": None,
                            "date": start.isoformat(),
                            "recipe_id": str(soup.id),
                            "servings": 2,
                        },
                    ]
                },
            )
            data = proposed.structured_content
            assert len(data["additions"]) == 1
            assert len(data["edits"]) == 1
            assert uow.meals[existing.id].servings == 2

            fetched = await client.call_tool("get_meal_plan_proposal", {"proposal_id": data["id"]})
            assert len(fetched.structured_content["edits"]) == 1

            confirmed = await client.call_tool(
                "confirm_meal_plan", {"proposal_id": data["id"], "reviewed": True}
            )
            assert confirmed.structured_content["status"] == "applied"
            assert len(uow.meals) == 2
            assert uow.meals[existing.id].servings == 4
            assert uow.meals[existing.id].version == existing.version + 1
    finally:
        history_context.reset(token)
