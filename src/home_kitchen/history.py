"""Durable household history and retrieval evidence for future RAG use."""

from __future__ import annotations

import builtins
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, Field, JsonValue, StringConstraints
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from home_kitchen.context import history_context

HistoryChannel = Literal["app", "api", "mcp"]
HistoryKind = Literal[
    "chat", "feedback", "preference", "purchase", "recipe_rating", "planning", "domain_event"
]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
HistoryMessage = Annotated[NonBlank, StringConstraints(max_length=16_000)]
SessionId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
SearchQuery = Annotated[str, StringConstraints(max_length=1_000)]


class HistoryEntryCreate(BaseModel):
    channel: HistoryChannel = "api"
    kind: HistoryKind
    message: HistoryMessage
    structured: dict[str, JsonValue] = Field(default_factory=dict)
    recipe_id: UUID | None = None
    session_id: SessionId | None = None


class HistoryEntry(HistoryEntryCreate):
    id: UUID
    occurred_at: AwareDatetime
    actor_id: str | None = None


class RetrievedEvidence(BaseModel):
    id: UUID
    kind: HistoryKind
    message: str
    structured: dict[str, JsonValue]
    occurred_at: AwareDatetime
    score: float = Field(ge=0)


class KnowledgeRetriever(Protocol):
    def retrieve(self, query: str, limit: int = 8) -> list[RetrievedEvidence]: ...


class HistoryRepository:
    """PostgreSQL history repository; retrieval can be replaced behind its protocol."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection: Connection | None = None,
    ) -> HistoryEntry:
        """Persist one entry, participating in a caller transaction when supplied."""
        entry = HistoryEntry(
            id=uuid4(),
            occurred_at=datetime.now(UTC),
            actor_id=actor_id or history_context.get().actor_id,
            **data.model_dump(),
        )
        statement = text(
            "INSERT INTO kitchen_history "
            "(id, occurred_at, channel, kind, message, structured, recipe_id, session_id, actor_id) "
            "VALUES (:id, :occurred_at, :channel, :kind, :message, CAST(:structured AS jsonb), "
            ":recipe_id, :session_id, :actor_id)"
        )
        params = {
            **entry.model_dump(),
            "structured": json.dumps(entry.structured),
            "occurred_at": entry.occurred_at,
        }
        if connection is not None:
            connection.execute(statement, params)
        else:
            with self.engine.begin() as owned_connection:
                owned_connection.execute(statement, params)
        return entry

    def list(self, limit: int = 30) -> builtins.list[HistoryEntry]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, occurred_at, channel, kind, message, structured, recipe_id, "
                    "session_id, actor_id FROM kitchen_history "
                    "ORDER BY occurred_at DESC, id DESC LIMIT :limit"
                ),
                {"limit": limit},
            ).mappings()
            return [HistoryEntry.model_validate(dict(row)) for row in rows]

    def search(
        self,
        query: SearchQuery,
        limit: int = 8,
        kinds: Sequence[HistoryKind] | None = None,
    ) -> builtins.list[RetrievedEvidence]:
        if len(query) > 1000:
            raise ValueError("query must not exceed 1000 characters")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        query = query.strip()
        if kinds is not None and not kinds:
            return []
        params: dict[str, object] = {"limit": limit}
        kind_clause = ""
        if kinds is not None:
            kind_clause = " AND kind = ANY(:kinds)"
            params["kinds"] = list(kinds)

        if query:
            params["query"] = query
            statement = text(
                "SELECT id, kind, message, structured, occurred_at, "
                "ts_rank_cd(search_document, websearch_to_tsquery('simple', :query)) AS score "
                "FROM kitchen_history "
                "WHERE search_document @@ websearch_to_tsquery('simple', :query)"
                + kind_clause
                + " ORDER BY score DESC, occurred_at DESC, id DESC LIMIT :limit"
            )
        else:
            statement = text(
                "SELECT id, kind, message, structured, occurred_at, 0.0 AS score "
                "FROM kitchen_history WHERE TRUE"
                + kind_clause
                + " ORDER BY occurred_at DESC, id DESC LIMIT :limit"
            )
        with self.engine.connect() as connection:
            rows = connection.execute(statement, params).mappings()
            return [RetrievedEvidence.model_validate(dict(row)) for row in rows]


class PostgresKnowledgeRetriever:
    """Current full-text adapter for the replaceable knowledge retrieval protocol."""

    def __init__(self, repository: HistoryRepository):
        self.repository = repository

    def retrieve(self, query: str, limit: int = 8) -> list[RetrievedEvidence]:
        return self.repository.search(query, limit=limit)
