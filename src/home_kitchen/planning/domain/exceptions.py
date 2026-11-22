"""Domain exceptions for meal planning proposals."""


class ProposalConflict(ValueError):
    """Refresh a stale proposal and explicitly review any changed entries."""


class MealEditConflict(RuntimeError):
    """A meal changed after the version supplied by an editor was read."""

    def __init__(self, expected_version: int, current_version: int):
        super().__init__(
            f"Meal was changed by another client; the current version is {current_version}. "
            "Your edits are still available; review the latest version before saving again."
        )
        self.expected_version = expected_version
        self.current_version = current_version


class ProposalExpired(ProposalConflict):
    """The proposal must be recreated and reviewed again."""


class ProposalNotFound(KeyError):
    """No proposal is visible to the requesting actor."""


class ProposalActorMismatch(ProposalNotFound):
    """The proposal belongs to another actor."""
