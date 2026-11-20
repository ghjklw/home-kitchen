"""Recipe acquisition contracts shared by transports and provider adapters."""

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from home_kitchen.imports import ImportedDraft
from home_kitchen.models import (
    AdaptationProvenance,
    GuidedCookingStep,
    NonBlank,
    PhotoSource,
    PrepCategory,
    PurchaseEstimate,
    Quantity,
    RecipeCreate,
    RecipeImageAttribution,
    RecipeType,
    SourceIndex,
    SourceIngredient,
    derive_prep_category,
    normalize_prep_category_value,
    normalize_recipe_type_value,
    validate_source_metadata,
)
from home_kitchen.settings import Household


class EvidenceSnippet(BaseModel):
    evidence_id: str
    text: str


class RecipeContext(BaseModel):
    household: Household
    knowledge: list[EvidenceSnippet] = Field(default_factory=list)
    actor_id: str | None = None


class DraftIngredient(BaseModel):
    name: NonBlank
    quantity: Quantity | None = None
    unit: NonBlank | None = None
    category: NonBlank = "Other"
    source_indices: list[SourceIndex] = Field(default_factory=list)
    purchase_estimate: PurchaseEstimate | None = None


class EditableRecipeDraft(BaseModel):
    """Proposed fields, deliberately distinct from a complete saveable recipe."""

    title: NonBlank
    servings: int = Field(gt=0)
    ingredients: list[DraftIngredient] = Field(min_length=1)
    instructions: list[NonBlank] = Field(min_length=1)
    source: Literal["ai"] = "ai"
    source_url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    source_ingredients: list[SourceIngredient] = Field(default_factory=list)
    provenance: AdaptationProvenance | None = None
    photo_source: PhotoSource | None = None
    image_attribution: RecipeImageAttribution | None = None
    source_language: NonBlank | None = None
    recipe_type: RecipeType | None = None
    prep_time_minutes: int | None = Field(default=None, ge=1, le=2880)
    prep_category: PrepCategory | None = None
    guided_steps: list[GuidedCookingStep] = Field(default_factory=list)

    @field_validator("recipe_type", mode="before")
    @classmethod
    def normalize_recipe_type(cls, value: Any) -> Any:
        return normalize_recipe_type_value(value)

    @field_validator("prep_category", mode="before")
    @classmethod
    def normalize_prep_category(cls, value: Any) -> Any:
        return normalize_prep_category_value(value)

    @model_validator(mode="after")
    def valid_references(self) -> "EditableRecipeDraft":
        validate_source_metadata(
            self.source_ingredients,
            [item.source_indices for item in self.ingredients],
            self.source_url,
            self.provenance,
        )
        self.prep_category = derive_prep_category(self.prep_time_minutes, self.prep_category)
        return self


class RecipeDraftRequest(BaseModel):
    prompt: NonBlank | None = None
    url: HttpUrl | None = None
    recipe: RecipeCreate | None = None
    imported: ImportedDraft | None = None
    draft: EditableRecipeDraft | None = None

    def require_ai_source(self) -> None:
        if (
            sum(value is not None for value in (self.url, self.recipe, self.imported, self.draft))
            != 1
        ):
            raise ValueError(
                "AI adaptation requires exactly one source: recipe, draft, imported or url"
            )


class RecipeDraft(BaseModel):
    provider: str
    recipe: RecipeCreate | None = None
    imported: ImportedDraft | None = None
    adapted: EditableRecipeDraft | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    requires_review: Literal[True] = True

    @model_validator(mode="after")
    def one_payload(self) -> "RecipeDraft":
        if sum(value is not None for value in (self.recipe, self.imported, self.adapted)) != 1:
            raise ValueError(
                "A recipe draft requires exactly one recipe, imported or adapted payload"
            )
        return self


class RecipeProviderInfo(BaseModel):
    id: str
    label: str
    capabilities: list[str]
