"""Explicit, structured recipe feedback saved to household history."""

from typing import Annotated, Any, Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from home_kitchen.context import HistoryChannel
from home_kitchen.history import HistoryEntry, HistoryEntryCreate
from home_kitchen.models import Recipe


class RecipeTitleReader(Protocol):
    """Narrow port for verifying recipe existence and title when recording feedback."""

    def get_recipe(self, recipe_id: UUID) -> Recipe: ...


class HistoryRecordPort(Protocol):
    """Narrow port for recording recipe feedback entries."""

    def record(
        self,
        data: HistoryEntryCreate,
        actor_id: str | None = None,
        connection: Any = None,
    ) -> HistoryEntry: ...


FeedbackNotes = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class RecipeFeedbackCreate(BaseModel):
    """At least one explicit rating, would-make-again answer, or note."""

    model_config = ConfigDict(extra="forbid")

    rating: Annotated[StrictInt, Field(ge=1, le=5)] | None = None
    would_make_again: StrictBool | None = None
    notes: FeedbackNotes | None = None

    @model_validator(mode="after")
    def require_feedback(self) -> "RecipeFeedbackCreate":
        if self.rating is None and self.would_make_again is None and self.notes is None:
            raise ValueError("Provide a rating, would-make-again answer, or note")
        return self


class RecipeFeedbackService:
    def __init__(self, kitchen: RecipeTitleReader, history: HistoryRecordPort):
        self.kitchen = kitchen
        self.history = history

    def record(
        self,
        recipe_id: UUID,
        data: RecipeFeedbackCreate,
        channel: HistoryChannel,
        actor_id: str | None = None,
    ) -> HistoryEntry:
        recipe = self.kitchen.get_recipe(recipe_id)
        structured = data.model_dump(exclude_none=True)
        details = [f'Feedback for recipe "{recipe.title}".']
        if data.rating is not None:
            details.append(f"Rating: {data.rating}/5.")
        if data.would_make_again is not None:
            answer = "yes" if data.would_make_again else "no"
            details.append(f"Would make again: {answer}.")
        if data.notes is not None:
            details.append(f"Notes: {data.notes}")
        return self.history.record(
            HistoryEntryCreate(
                channel=channel,
                kind="recipe_rating",
                message=" ".join(details),
                structured=structured,
                recipe_id=recipe_id,
            ),
            actor_id=actor_id,
        )
