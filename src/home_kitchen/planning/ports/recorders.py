"""Port for recording planning history events."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from home_kitchen.history import HistoryChannel


class HistoryRecorder(Protocol):
    """Secondary port for recording planning history entries."""

    def record_planning(
        self,
        *,
        actor_id: str,
        channel: HistoryChannel,
        message: str,
        recipe_id: UUID,
        structured: dict[str, Any],
    ) -> None: ...
