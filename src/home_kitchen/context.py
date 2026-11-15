"""Authenticated request context follows synchronous handlers into their threads."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class HistoryContext:
    actor_id: str | None = None
    channel: Literal["api", "app", "mcp"] = "api"


_DEFAULT_CONTEXT = HistoryContext()
history_context: ContextVar[HistoryContext] = ContextVar(
    "history_context", default=_DEFAULT_CONTEXT
)
