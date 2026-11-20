"""Meal planning proposal vertical slice adhering to layered architecture boundaries."""

from home_kitchen.planning.adapters.postgres import (
    PostgresPlanningProposalAdapter,
    PostgresPlanningSession,
    PostgresPlanningUnitOfWork,
    _proposals,
)
from home_kitchen.planning.application.service import PlanningProposalService
from home_kitchen.planning.bootstrap import (
    create_in_memory_planning_proposal_service,
    create_planning_proposal_service,
)
from home_kitchen.planning.domain.exceptions import (
    ProposalActorMismatch,
    ProposalConflict,
    ProposalExpired,
    ProposalNotFound,
)
from home_kitchen.planning.domain.models import ProposalRead, ProposalRecord, ProposalStatus
from home_kitchen.planning.domain.rules import (
    _fingerprint,
    fingerprint_meal,
    fingerprint_recipe,
    validate_actor,
    validate_proposal_bounds,
    validate_service_bounds,
)

__all__ = [
    "PlanningProposalService",
    "PostgresPlanningProposalAdapter",
    "PostgresPlanningSession",
    "PostgresPlanningUnitOfWork",
    "ProposalActorMismatch",
    "ProposalConflict",
    "ProposalExpired",
    "ProposalNotFound",
    "ProposalRead",
    "ProposalRecord",
    "ProposalStatus",
    "_fingerprint",
    "_proposals",
    "create_in_memory_planning_proposal_service",
    "create_planning_proposal_service",
    "fingerprint_meal",
    "fingerprint_recipe",
    "validate_actor",
    "validate_proposal_bounds",
    "validate_service_bounds",
]
