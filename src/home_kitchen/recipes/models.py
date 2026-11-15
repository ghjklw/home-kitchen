"""Recipe acquisition contracts shared by transports and provider adapters."""

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

from home_kitchen.imports import ImportedDraft
from home_kitchen.models import NonBlank, RecipeCreate
from home_kitchen.settings import Household


class EvidenceSnippet(BaseModel):
    evidence_id: str
    text: str


class RecipeContext(BaseModel):
    household: Household
    knowledge: list[EvidenceSnippet] = Field(default_factory=list)


class RecipeDraftRequest(BaseModel):
    prompt: NonBlank | None = None
    url: HttpUrl | None = None
    recipe: RecipeCreate | None = None


class RecipeDraft(BaseModel):
    provider: str
    recipe: RecipeCreate | None = None
    imported: ImportedDraft | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    requires_review: Literal[True] = True

    @model_validator(mode="after")
    def one_payload(self) -> "RecipeDraft":
        if (self.recipe is None) == (self.imported is None):
            raise ValueError("A recipe draft requires exactly one recipe or imported payload")
        return self


class RecipeProviderInfo(BaseModel):
    id: str
    label: str
    capabilities: list[str]
