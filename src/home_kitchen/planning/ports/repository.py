"""Port for meal planning proposal persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from home_kitchen.planning.domain.models import ProposalRead, ProposalRecord, ProposalStatus


class ProposalRepository(Protocol):
    """Secondary port for saving, retrieving, and updating proposal records."""

    def save(self, record: ProposalRecord) -> ProposalRead: ...

    def get(self, proposal_id: UUID, actor_id: str, *, lock: bool = False) -> ProposalRead: ...

    def update_status(
        self,
        proposal_id: UUID,
        *,
        status: ProposalStatus,
        attempt_count: int,
        last_error: str | None,
        applied_meal_ids: list[UUID],
    ) -> ProposalRead: ...
