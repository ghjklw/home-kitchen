from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from home_kitchen.history import (
    HistoryEntryCreate,
    HistoryRepository,
    PostgresKnowledgeRetriever,
)


def test_history_dto_normalizes_and_validates_input():
    record = HistoryEntryCreate(kind="chat", message="  We liked the soup.  ")
    assert record.channel == "api"
    assert record.message == "We liked the soup."
    assert record.structured == {}

    with pytest.raises(ValidationError):
        HistoryEntryCreate(kind="chat", message="  ")
    with pytest.raises(ValidationError):
        HistoryEntryCreate(kind="chat", message="x" * 16_001)
    with pytest.raises(ValidationError):
        HistoryEntryCreate(kind="chat", message="message", channel="web")


def test_history_records_lists_and_participates_in_outer_transaction(test_database_url):
    engine = create_engine(test_database_url)
    repository = HistoryRepository(engine)
    persisted = repository.record(
        HistoryEntryCreate(
            channel="mcp",
            kind="purchase",
            message="Bought oat milk",
            structured={"item": "oat milk", "quantity": 2},
            recipe_id=uuid4(),
            session_id="mcp-session-1",
        ),
        actor_id="household-member-7",
    )
    assert persisted.occurred_at.tzinfo is not None
    row = repository.list(limit=1)[0]
    assert row.id == persisted.id
    assert row.channel == "mcp"
    assert row.kind == "purchase"
    assert row.message == "Bought oat milk"
    assert row.structured == {"item": "oat milk", "quantity": 2}
    assert row.recipe_id == persisted.recipe_id
    assert row.actor_id == "household-member-7"

    rollback_id = None
    with engine.begin() as connection:
        rolled_back = repository.record(
            HistoryEntryCreate(kind="feedback", message="Temporary feedback"),
            connection=connection,
        )
        rollback_id = rolled_back.id
        assert (
            connection.execute(
                text("SELECT count(*) FROM kitchen_history WHERE id = :id"),
                {"id": rollback_id},
            ).scalar_one()
            == 1
        )
        connection.rollback()
    assert all(entry.id != rollback_id for entry in repository.list(limit=100))
    engine.dispose()


def test_history_search_matches_structured_content_filters_kind_and_indexes(test_database_url):
    engine = create_engine(test_database_url)
    repository = HistoryRepository(engine)
    repository.record(
        HistoryEntryCreate(
            kind="preference",
            message="The household prefers quick dinners",
            structured={"ingredient": "chickpeas", "note": "weeknight pantry"},
        )
    )
    repository.record(HistoryEntryCreate(kind="feedback", message="The stew was too salty"))

    matches = repository.search("chickpeas", limit=8)
    assert len(matches) == 1
    assert matches[0].kind == "preference"
    assert matches[0].structured["ingredient"] == "chickpeas"
    assert matches[0].score > 0
    assert repository.search("quick dinners", kinds=["feedback"]) == []
    assert [entry.kind for entry in repository.search("", limit=10)] == [
        entry.kind for entry in repository.list(limit=10)
    ]

    indexes = {index["name"] for index in inspect(engine).get_indexes("kitchen_history")}
    assert "ix_kitchen_history_search_document" in indexes
    assert "ix_kitchen_history_occurred_at" in indexes
    with engine.connect() as connection:
        # A recipe reference is intentionally archival metadata, not a foreign key.
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.table_constraints "
                    "WHERE table_name = 'kitchen_history' AND constraint_type = 'FOREIGN KEY'"
                )
            ).scalar_one()
            == 0
        )

    retriever = PostgresKnowledgeRetriever(repository)
    assert retriever.retrieve("salty", limit=1)[0].kind == "feedback"
    with pytest.raises(ValueError):
        repository.list(limit=101)
    with pytest.raises(ValueError):
        repository.search("word", limit=21)
    engine.dispose()


def test_history_search_boosts_phrase_matches_over_scattered_terms(test_database_url):
    engine = create_engine(test_database_url)
    repository = HistoryRepository(engine)
    repository.record(
        HistoryEntryCreate(
            kind="chat",
            message="We liked quick meals, but dinners can take time",
        )
    )
    repository.record(
        HistoryEntryCreate(kind="preference", message="The household prefers quick dinners")
    )

    matches = repository.search("quick dinners", limit=2)

    assert [match.kind for match in matches] == ["preference", "chat"]
    assert matches[0].score > matches[1].score
    engine.dispose()
