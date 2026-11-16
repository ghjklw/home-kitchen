"""Domain exceptions for meal planning proposals."""


class ProposalConflict(ValueError):
    """Refresh a stale proposal and explicitly review any changed entries."""


class ProposalExpired(ProposalConflict):
    """The proposal must be recreated and reviewed again."""


class ProposalNotFound(KeyError):
    """No proposal is visible to the requesting actor."""


class ProposalActorMismatch(ProposalNotFound):
    """The proposal belongs to another actor."""
