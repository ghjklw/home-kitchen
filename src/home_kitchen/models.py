"""Validated household kitchen data."""

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
SourceIndex = Annotated[int, Field(ge=0)]


class PurchaseEstimate(BaseModel):
    """A separate shopping amount, with explicit approximation evidence."""

    quantity: Quantity
    unit: NonBlank
    approximate: bool
    provenance: NonBlank
    source_url: HttpUrl | None = None

    @field_validator("unit")
    @classmethod
    def normalize_estimate_unit(cls, value: str) -> str:
        return value.casefold()


class Ingredient(BaseModel):
    name: NonBlank
    quantity: Quantity | None
    unit: NonBlank | None = "item"
    canonical_product_id: UUID | None = None
    canonical_unit_id: UUID | None = None
    source_unit_expression: NonBlank | None = None
    category: NonBlank = "Other"
    source_indices: list[SourceIndex] = Field(default_factory=list)
    purchase_estimate: PurchaseEstimate | None = None

    @model_validator(mode="wrap")
    @classmethod
    def retain_source_unit_expression(cls, value: Any, handler: Any) -> Any:
        ingredient = handler(value)
        if isinstance(value, dict) and "source_unit_expression" not in value:
            object.__setattr__(ingredient, "source_unit_expression", value.get("unit"))
        return ingredient

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None

    @model_validator(mode="after")
    def measured_ingredient_requires_unit(self) -> "Ingredient":
        if self.quantity is not None and self.unit is None:
            raise ValueError("Measured ingredients require a unit")
        return self


class RecipeSource(StrEnum):
    MANUAL = "manual"
    IMPORTED = "imported"
    AI = "ai"
    COOKIDOO = "cookidoo"


class RecipeType(StrEnum):
    MIDDAG = "middag"
    DESSERT = "dessert"
    FROKOST = "frokost"
    LUNSJ = "lunsj"
    TILBEHOR = "tilbehør"
    BAKST = "bakst"
    ANNET = "annet"


class PrepCategory(StrEnum):
    RASK = "rask"
    NORMAL = "normal"
    LANGTID = "langtid"
    BATCH_COOKING = "batch_cooking"


def normalize_recipe_type_value(value: Any) -> Any:
    """Normalize recipe_type strings (case, whitespace, ASCII 'tilbehor' alias)."""
    if isinstance(value, str):
        value = value.strip().casefold()
        if not value:
            return None
        if value == "tilbehor":
            return RecipeType.TILBEHOR
    return value


def normalize_prep_category_value(value: Any) -> Any:
    """Normalize prep_category strings (case, whitespace, batch-cooking aliases)."""
    if isinstance(value, str):
        value = value.strip().casefold()
        if not value:
            return None
        if value in ("batch-cooking", "batchcooking", "batch cooking"):
            return PrepCategory.BATCH_COOKING
    return value


def normalize_recipe_source_value(value: Any) -> Any:
    """Normalize recipe source strings (case and whitespace)."""
    if isinstance(value, str):
        value = value.strip().casefold()
        if not value:
            return None
    return value


def derive_prep_category(
    prep_time_minutes: int | None, prep_category: PrepCategory | None
) -> PrepCategory | None:
    """Auto-derive prep_category from prep_time_minutes when omitted."""
    if prep_category is None and prep_time_minutes is not None:
        if prep_time_minutes <= 30:
            return PrepCategory.RASK
        if prep_time_minutes <= 75:
            return PrepCategory.NORMAL
        return PrepCategory.LANGTID
    return prep_category


_BATCH_KEYWORDS_RE = re.compile(
    r"(?:\b(?:batch(?:\s*cooking)?|meal[\s-]?prep|ukens|lasagn[ae])\b|gryte)",
    re.IGNORECASE,
)
_RECIPE_TYPE_PATTERNS: tuple[tuple[RecipeType, re.Pattern[str]], ...] = (
    (
        RecipeType.DESSERT,
        re.compile(
            r"(?:\b(?:cr[eè]me|glace|flan|gelato)\b|"
            r"(?:desserts?|tiramisu|mousses?|compotes?|kompott|sorbets?|iskrem|"
            r"pudding(?:er)?|fromasj|panna\s+cotta|clafoutis|crumble))\b",
            re.IGNORECASE,
        ),
    ),
    (
        RecipeType.BAKST,
        re.compile(
            r"(?:\b(?:cakes?|breads?|pain|pies?|tarts?|buns?|loaves|loaf|scones?|"
            r"cookies?|brownies?|pastr(?:y|ies)|dough|baking)\b|"
            r"(?:kake[rn]?|kaker|muffins?|brød|brioches?|bolle[rn]?|boller|"
            r"rundstykke[rn]?|rundstykker|kjeks|bakst|focaccia|croissants?|"
            r"baguettes?|gâteaux?|gateaux?|pâtisserie|patisserie|boulangerie|"
            r"gougères?|gougeres?|kanelsnurr(?:er)?|skolebrød|gjærbakst|terte|"
            r"madeleines?|financiers?|sablés?|sables?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        RecipeType.FROKOST,
        re.compile(
            r"(?:\b(?:breakfast|brunch|oatmeal|porridge|pancakes?|waffles?|"
            r"omelettes?|omelet|crêpes?|crepes?)\b|"
            r"(?:frokost|smoothies?|granola|müsli|musli|muesli|grøt|havregrøt|"
            r"pannekake[rn]?|pannekaker|eggerøre|omelett|chiapudding|"
            r"petit[\s-]d[eé]jeuner))\b",
            re.IGNORECASE,
        ),
    ),
    (
        RecipeType.LUNSJ,
        re.compile(
            r"(?:\b(?:lunch|soups?|salads?|sandwich(?:es)?|wraps?|toast|panini|"
            r"bagels?|gazpacho|bruschetta|veloutés?|veloutes?|potages?|soupes?|"
            r"salades?|déjeuner|dejeuner)\b|"
            r"(?:lunsj|suppe[rn]?|supper|salat(?:er)?|quiches?|tartines?|smørbrød))\b",
            re.IGNORECASE,
        ),
    ),
    (
        RecipeType.TILBEHOR,
        re.compile(
            r"(?:\b(?:sauces?|dips?|sides?|side\s+dish(?:es)?|jams?|marmalades?|"
            r"stocks?|broths?|gravy|relish|condiments?|vinaigrettes?|confitures?|"
            r"bouillons?|accompagnements?)\b|"
            r"(?:tilbehør|tilbehor|saus(?:er)?|chutneys?|dressings?|aioli|aïoli|"
            r"majones|mayonnaise|pesto|salsa|guacamole|hummus|houmous|tzatziki|"
            r"syltetøy|syltetoy|marinade[rn]?))\b",
            re.IGNORECASE,
        ),
    ),
)
_CATEGORY_ONLY_PATTERNS: tuple[tuple[RecipeType, re.Pattern[str]], ...] = (
    (
        RecipeType.ANNET,
        re.compile(
            r"\b(?:annet|other|drikke|drinks?|beverages?|boissons?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        RecipeType.MIDDAG,
        re.compile(
            r"\b(?:middag|dinner|mains?|main\s+dish(?:es)?|hovedrett(?:er)?|plats?\s+principa(?:l|ux))\b",
            re.IGNORECASE,
        ),
    ),
)
_PRIMARY_TITLE_SPLIT_RE = re.compile(r"\s+(?:med|with|avec)\s+", re.IGNORECASE)


def _flatten_labels(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        return [values.strip()] if values.strip() else []
    out: list[str] = []
    for item in values:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _coerce_recipe_type(value: RecipeType | str | None) -> RecipeType | None:
    if value is None:
        return None
    if isinstance(value, RecipeType):
        return value
    normalized = normalize_recipe_type_value(value)
    if isinstance(normalized, RecipeType):
        return normalized
    if isinstance(normalized, str):
        try:
            return RecipeType(normalized)
        except ValueError:
            return None
    return None


def _coerce_prep_category(value: PrepCategory | str | None) -> PrepCategory | None:
    if value is None:
        return None
    if isinstance(value, PrepCategory):
        return value
    normalized = normalize_prep_category_value(value)
    if isinstance(normalized, PrepCategory):
        return normalized
    if isinstance(normalized, str):
        try:
            return PrepCategory(normalized)
        except ValueError:
            return None
    return None


def _clamp_prep_time_minutes(value: int | float | str | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = int(round(value))
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            return None
        num = int(stripped)
    else:
        return None
    if num <= 0:
        return None
    return max(1, min(2880, num))


def infer_recipe_metadata(
    title: str,
    ingredients: Iterable[str] | None = None,
    categories: Iterable[str] | str | None = None,
    tags: Iterable[str] | str | None = None,
    prep_time_minutes: int | float | None = None,
    servings: int | None = None,
    recipe_type: RecipeType | str | None = None,
    prep_category: PrepCategory | str | None = None,
) -> tuple[RecipeType, int | None, PrepCategory | None]:
    """Infer and normalize (recipe_type, prep_time_minutes, prep_category) for imported recipes."""
    del ingredients  # Reserved for future signals; avoid false positives on pantry staples.
    clamped_minutes = _clamp_prep_time_minutes(prep_time_minutes)
    label_list = _flatten_labels(categories) + _flatten_labels(tags)
    label_text = " ".join(label_list)
    title_clean = (title or "").strip()

    resolved_prep_category = _coerce_prep_category(prep_category)
    if resolved_prep_category is None:
        batch_text = f"{title_clean} {label_text}".strip()
        is_batch_servings = (
            isinstance(servings, int) and not isinstance(servings, bool) and servings >= 6
        )
        if is_batch_servings or (batch_text and _BATCH_KEYWORDS_RE.search(batch_text)):
            resolved_prep_category = PrepCategory.BATCH_COOKING
        else:
            resolved_prep_category = derive_prep_category(clamped_minutes, None)

    resolved_type = _coerce_recipe_type(recipe_type)
    if resolved_type is None and label_text:
        for candidate_type, pattern in (*_RECIPE_TYPE_PATTERNS, *_CATEGORY_ONLY_PATTERNS):
            if pattern.search(label_text):
                resolved_type = candidate_type
                break

    if resolved_type is None and title_clean:
        primary_title = _PRIMARY_TITLE_SPLIT_RE.split(title_clean, maxsplit=1)[0].strip()
        target_title = primary_title or title_clean
        for candidate_type, pattern in _RECIPE_TYPE_PATTERNS:
            if pattern.search(target_title):
                resolved_type = candidate_type
                break

    if resolved_type is None:
        resolved_type = RecipeType.MIDDAG

    return resolved_type, clamped_minutes, resolved_prep_category


class SourceIngredient(BaseModel):
    index: SourceIndex
    text: NonBlank


class GuidedCookingStep(BaseModel):
    """Structured Thermomix / Cookidoo guided-cooking step metadata."""

    step_index: int = Field(ge=0)
    text: NonBlank
    time_seconds: int | None = Field(default=None, ge=0)
    temperature: str | None = Field(default=None, max_length=64)
    speed: str | None = Field(default=None, max_length=64)
    direction: Literal["forward", "reverse"] | None = None
    reverse: bool | None = None
    blade: str | None = Field(default=None, max_length=120)
    accessory: str | None = Field(default=None, max_length=120)
    mode: str | None = Field(default=None, max_length=120)
    ingredient_references: list[str] = Field(default_factory=list)
    ingredient_indices: list[SourceIndex] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_direction(self) -> "GuidedCookingStep":
        if self.direction == "reverse" and self.reverse is None:
            self.reverse = True
        elif self.direction == "forward" and self.reverse is None:
            self.reverse = False
        elif self.reverse is True and self.direction is None:
            self.direction = "reverse"
        elif self.reverse is False and self.direction is None:
            self.direction = "forward"
        return self


class AdaptationProvenance(BaseModel):
    """Attribution supplied for review, never certification of source authenticity."""

    kind: Literal["ai_adaptation"] = "ai_adaptation"
    original_source: RecipeSource
    source_url: HttpUrl | None = None


def validate_source_metadata(
    source_ingredients: list[SourceIngredient],
    ingredient_references: list[list[int]],
    source_url: HttpUrl | None,
    provenance: AdaptationProvenance | None,
) -> None:
    indices = [item.index for item in source_ingredients]
    if len(set(indices)) != len(indices):
        raise ValueError("Source ingredient indices must be unique")
    if any(index not in indices for references in ingredient_references for index in references):
        raise ValueError("Ingredient source_indices must reference source_ingredients")
    if provenance is not None and provenance.source_url != source_url:
        raise ValueError("Provenance source_url must match recipe source_url")


class PhotoSource(BaseModel):
    """Private source identity; caller-supplied book attribution is unverified."""

    id: UUID
    book_title: NonBlank | None = Field(default=None, max_length=300)
    page: NonBlank | None = Field(default=None, max_length=80)


class RecipeImageAttribution(BaseModel):
    """Provider attribution for an explicitly selected public recipe image."""

    provider: Literal["wikimedia_commons"] = "wikimedia_commons"
    provider_id: NonBlank = Field(max_length=80)
    query: NonBlank = Field(max_length=200)
    title: NonBlank = Field(max_length=300)
    image_url: HttpUrl
    source_page_url: HttpUrl
    artist: str | None = Field(default=None, max_length=500)
    credit: str | None = Field(default=None, max_length=500)
    license_name: NonBlank = Field(max_length=100)
    license_url: HttpUrl
    attribution_required: bool
    attribution: str | None = Field(default=None, max_length=500)
    obligations: list[NonBlank] = Field(default_factory=list, max_length=4)


class RecipeCreate(BaseModel):
    title: NonBlank
    servings: int = Field(gt=0)
    ingredients: list[Ingredient] = Field(min_length=1)
    instructions: list[NonBlank] = Field(min_length=1)
    source_url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    source: RecipeSource = RecipeSource.MANUAL
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
    def valid_source_metadata(self) -> "RecipeCreate":
        validate_source_metadata(
            self.source_ingredients,
            [item.source_indices for item in self.ingredients],
            self.source_url,
            self.provenance,
        )
        if self.image_attribution and self.image_url != self.image_attribution.image_url:
            # A caller may be editing the image URL while echoing a prior Recipe
            # response. Drop stale credit; only the selection service may attach it.
            self.image_attribution = None
        self.prep_category = derive_prep_category(self.prep_time_minutes, self.prep_category)
        return self

    @field_validator("image_url")
    @classmethod
    def safe_image_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value and (value.scheme != "https" or value.username or value.password):
            raise ValueError("Recipe images must use public HTTPS URLs without credentials")
        return value


class RecipeActivityStats(BaseModel):
    """Derived recipe activity metrics and explicit feedback summary."""

    times_planned: int = Field(default=0, ge=0)
    times_cooked: int = Field(default=0, ge=0)
    times_ordered: int = Field(default=0, ge=0)
    latest_rating: Annotated[int, Field(ge=1, le=5)] | None = None
    average_rating: float | None = None
    would_make_again: bool | None = None
    feedback_count: int = Field(default=0, ge=0)


class Recipe(RecipeCreate):
    id: UUID
    source_language: NonBlank | None = "und"
    activity_stats: RecipeActivityStats | None = None
    stats: RecipeActivityStats | None = None

    @model_validator(mode="before")
    @classmethod
    def default_source_language(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("source_language") is None:
            data = dict(data)
            data["source_language"] = "und"
        return data

    @model_validator(mode="after")
    def sync_stats(self) -> "Recipe":
        if self.activity_stats is not None and self.stats is None:
            self.stats = self.activity_stats
        elif self.stats is not None and self.activity_stats is None:
            self.activity_stats = self.stats
        return self


class RecipeTranslation(BaseModel):
    """Stored and versioned translation for a specific recipe and locale."""

    id: UUID
    recipe_id: UUID
    target_locale: NonBlank
    recipe_version: NonBlank
    title: NonBlank
    instructions: list[NonBlank]
    ingredients: list[Ingredient]
    provider: NonBlank
    model: NonBlank
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_stale: bool = False


class RecipeTranslationRequest(BaseModel):
    target_locale: NonBlank
    force: bool = False


class RecipeTranslationUpdate(BaseModel):
    """Manual edit payload for a saved recipe translation."""

    title: NonBlank
    ingredients: list[Ingredient] = Field(min_length=1)
    instructions: list[NonBlank] = Field(min_length=1)


class MealCreate(BaseModel):
    date: date
    recipe_id: UUID
    servings: int = Field(gt=0)
    label: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None
    ) = None
    scheduled_time: time | None = None

    @field_validator("scheduled_time")
    @classmethod
    def validate_local_scheduled_time(cls, value: time | None) -> time | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("Scheduled time must be a local time without a timezone")
        return value

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_slot(cls, value: Any) -> Any:
        """Translate old breakfast/lunch/dinner payloads and pending proposals."""
        if isinstance(value, dict) and "slot" in value:
            data = dict(value)
            slot = data.pop("slot")
            if data.get("label") is None and slot is not None:
                data["label"] = {
                    "breakfast": "Frokost",
                    "lunch": "Lunsj",
                    "dinner": "Middag",
                }.get(str(slot).casefold(), str(slot))
            return data
        return value


class Meal(MealCreate):
    id: UUID


class PlannedMealRecipe(BaseModel):
    """Cooking quantities for one meal, alongside its unchanged source recipe."""

    meal: Meal
    source_recipe: Recipe
    servings: int = Field(gt=0)
    ingredients: list[Ingredient]


class ShoppingItemCreate(BaseModel):
    name: NonBlank
    quantity: Quantity = Decimal("1")
    unit: NonBlank = "item"
    category: NonBlank = "Other"

    @field_validator("unit")
    @classmethod
    def normalize_unit(cls, value: str) -> str:
        return value.casefold()


class ShoppingItem(ShoppingItemCreate):
    id: UUID
    completed: bool = False


class RegularItem(ShoppingItemCreate):
    id: UUID
    interval_days: int = Field(default=7, gt=0)
    last_purchased: date | None = None


class ShoppingSourceQuantity(BaseModel):
    """One source's planned contribution, independent of any actual purchase."""

    kind: Literal["meal", "capture", "regular"]
    source_id: UUID
    quantity: Quantity
    unit: NonBlank


class ShoppingLine(BaseModel):
    name: NonBlank
    quantity: Quantity
    unit: NonBlank
    category: NonBlank = "Other"
    origins: list[str]
    source_quantities: list[ShoppingSourceQuantity] = Field(default_factory=list)
    approximate: bool = False
    provenance: NonBlank | None = None
    source_url: HttpUrl | None = None


class UnquantifiedNeed(BaseModel):
    """An explicit requirement whose quantity still needs human review."""

    name: NonBlank
    unit: NonBlank | None = None
    category: NonBlank = "Other"
    origins: list[str]
    source_indices: list[SourceIndex] = Field(default_factory=list)


class WeekShopping(BaseModel):
    lines: list[ShoppingLine]
    suggestions: list[RegularItem]
    unquantified: list[UnquantifiedNeed] = Field(default_factory=list)
