"""Domain layer for meal planning proposals."""

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
    "ProposalActorMismatch",
    "ProposalConflict",
    "ProposalExpired",
    "ProposalNotFound",
    "ProposalRead",
    "ProposalRecord",
    "ProposalStatus",
    "_fingerprint",
    "fingerprint_meal",
    "fingerprint_recipe",
    "validate_actor",
    "validate_proposal_bounds",
    "validate_service_bounds",
]
