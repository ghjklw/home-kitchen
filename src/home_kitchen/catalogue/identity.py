"""Canonical product and unit identities, separate from source evidence."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from home_kitchen.models import NonBlank

AliasSourceType = Literal["reviewed_correction", "automatic_suggestion", "imported_alias"]
VALID_ALIAS_SOURCE_TYPES: frozenset[str] = frozenset(
    {"reviewed_correction", "automatic_suggestion", "imported_alias"}
)


class CanonicalProduct(BaseModel):
    id: UUID
    canonical_name: NonBlank
    variant: str = ""


class CanonicalUnit(BaseModel):
    id: UUID
    canonical_name: NonBlank


class AliasInput(BaseModel):
    alias: NonBlank
    locale: NonBlank
    context: str = ""
    source_type: AliasSourceType | None = None


class AliasCorrectionInput(BaseModel):
    kind: Literal["product", "unit"]
    identity_id: UUID
    alias: NonBlank
    locale: NonBlank = "nb"
    context: str = "recipe"
    reviewed: bool = False


class IdentityCorrectionEvent(BaseModel):
    id: UUID
    occurred_at: AwareDatetime | datetime
    household_id: UUID | None = None
    actor_id: str
    channel: Literal["app", "api", "mcp", "system"] = "api"
    kind: Literal["product", "unit"]
    locale: str
    context: str
    normalized_alias: str
    alias: str
    previous_identity_id: UUID | None = None
    new_identity_id: UUID
    previous_reviewed: bool | None = None
    new_reviewed: bool = True
    source_type: AliasSourceType = "reviewed_correction"


class IdentityCorrectionCitation(BaseModel):
    """Auditable provenance citation for a product or unit identity mapping correction."""

    event_id: UUID
    kind: Literal["product", "unit"]
    alias: str
    locale: str
    context: str
    new_identity_id: UUID
    previous_identity_id: UUID | None = None
    actor_id: str
    channel: Literal["app", "api", "mcp", "system"] = "api"
    source_type: AliasSourceType = "reviewed_correction"
    occurred_at: AwareDatetime | datetime
    household_id: UUID | None = None

    @classmethod
    def from_event(cls, event: IdentityCorrectionEvent) -> "IdentityCorrectionCitation":
        return cls(
            event_id=event.id,
            kind=event.kind,
            alias=event.alias,
            locale=event.locale,
            context=event.context,
            new_identity_id=event.new_identity_id,
            previous_identity_id=event.previous_identity_id,
            actor_id=event.actor_id,
            channel=event.channel,
            source_type=event.source_type,
            occurred_at=event.occurred_at,
            household_id=event.household_id,
        )


class ProductIdentityCreate(BaseModel):
    canonical_name: NonBlank
    variant: str = ""


class UnitIdentityCreate(BaseModel):
    canonical_name: NonBlank


class AliasResolution(BaseModel):
    status: Literal["unmapped", "resolved", "ambiguous"]
    candidates: list[CanonicalProduct | CanonicalUnit] = Field(default_factory=list)
    source_type: AliasSourceType | None = None
    source_types: list[AliasSourceType] = Field(default_factory=list)
    reviewed: bool | None = None
    household_scoped: bool = False


class UnmappedIngredient(BaseModel):
    recipe_id: UUID
    recipe_title: str
    ingredient_index: int
    name: NonBlank
    quantity: Decimal | None = None
    unit: str | None = None
    source_unit_expression: str | None = None
    canonical_product_id: UUID | None = None
    canonical_unit_id: UUID | None = None


class MapIngredientInput(BaseModel):
    recipe_id: UUID
    ingredient_index: int = Field(ge=0)
    canonical_product_id: UUID | None = None
    canonical_unit_id: UUID | None = None
    add_alias: bool = True
    locale: str = Field(default="nb", min_length=2, max_length=20)
    context: str = Field(default="recipe", max_length=100)


def normalize_identity_text(value: str) -> str:
    return " ".join(value.split()).casefold()


INGREDIENT_ATTRIBUTE_TOKENS: frozenset[str] = frozenset(
    {
        "økologisk",
        "økologiske",
        "øko",
        "oko",
        "organic",
        "bio",
        "biologique",
        "biologiques",
        "fresh",
        "fersk",
        "ferske",
        "ferskt",
        "frais",
        "fraîche",
        "fraîches",
        "fraiche",
        "fraiches",
        "fryst",
        "fryste",
        "frossen",
        "frossent",
        "frosne",
        "frozen",
        "surgelé",
        "surgelée",
        "surgelés",
        "surgelées",
        "hakket",
        "hakkede",
        "hakkete",
        "finhakket",
        "finhakkede",
        "grovhakket",
        "grovhakkede",
        "chopped",
        "minced",
        "diced",
        "sliced",
        "skivet",
        "skivede",
        "finskåret",
        "finskårne",
        "revet",
        "revne",
        "revene",
        "grated",
        "shredded",
        "râpé",
        "râpée",
        "râpés",
        "râpées",
        "haché",
        "hachée",
        "hachés",
        "hachées",
        "knust",
        "knuste",
        "crushed",
        "peeled",
        "skrelt",
        "skrelte",
        "skrellede",
        "strimlet",
        "strimlede",
        "julienned",
        "halvert",
        "halverte",
        "halved",
        "stekt",
        "stekte",
        "kokt",
        "kokte",
        "cuit",
        "cuite",
        "cuits",
        "cuites",
        "smeltet",
        "melted",
        "raw",
        "rå",
        "rått",
        "råe",
        "cru",
        "crue",
        "crus",
        "crues",
        "stor",
        "store",
        "stort",
        "liten",
        "lite",
        "lita",
        "små",
        "large",
        "small",
        "medium",
        "middels",
        "grand",
        "grande",
        "grands",
        "grandes",
        "petit",
        "petite",
        "petits",
        "petites",
        "moyen",
        "moyenne",
    }
)
_PUNCTUATION_SEPARATOR_RE = re.compile(r"[,;()\[\]]+")


def _tokenize_identity_phrase(value: str) -> list[str]:
    cleaned = _PUNCTUATION_SEPARATOR_RE.sub(" ", value)
    tokens: list[str] = []
    for raw in normalize_identity_text(cleaned).split():
        token = raw.strip(".:")
        if token:
            tokens.append(token)
    return tokens


def strip_ingredient_attributes(value: str) -> tuple[str, list[str]]:
    """Strip common qualifier/preparation tokens while retaining the base food name."""
    tokens = _tokenize_identity_phrase(value)
    if not tokens:
        return "", []
    kept: list[str] = []
    attributes: list[str] = []
    for token in tokens:
        if token in INGREDIENT_ATTRIBUTE_TOKENS:
            attributes.append(token)
        else:
            kept.append(token)
    if not kept:
        return normalize_identity_text(value), []
    return " ".join(kept), attributes


def strip_variant_attributes(variant: str) -> str:
    """Drop non-structural qualifier tokens from a proposed product variant."""
    tokens = _tokenize_identity_phrase(variant)
    kept = [token for token in tokens if token not in INGREDIENT_ATTRIBUTE_TOKENS]
    return " ".join(kept)


def _singular_stem_variants(phrase: str) -> list[str]:
    words = phrase.split()
    if not words:
        return []
    last = words[-1]
    stems: list[str] = []
    if last.endswith("ies") and len(last) > 4:
        stems.append(last[:-3] + "y")
    elif last.endswith("oes") and len(last) > 4:
        stems.append(last[:-2])
    elif last.endswith("s") and len(last) > 3 and not last.endswith(("ss", "us", "is", "as", "os")):
        stems.append(last[:-1])
    if last.endswith("er") and len(last) > 4:
        stems.append(last[:-2])
        stems.append(last[:-1])
    prefix = words[:-1]
    return [" ".join([*prefix, stem]) for stem in stems if len(stem) >= 3]


def candidate_identity_names(value: str) -> list[str]:
    """Return ordered normalized lookup candidates (exact, cleaned, stripped, singular stems)."""
    exact = normalize_identity_text(value)
    if not exact:
        return []
    candidates: list[str] = [exact]
    tokens = _tokenize_identity_phrase(value)
    cleaned = " ".join(tokens)
    if cleaned and cleaned not in candidates:
        candidates.append(cleaned)
    stripped, _ = strip_ingredient_attributes(value)
    if stripped and stripped not in candidates:
        candidates.append(stripped)
    for stem_source in (stripped, cleaned, exact):
        if not stem_source:
            continue
        for stem in _singular_stem_variants(stem_source):
            if stem not in candidates:
                candidates.append(stem)
    return candidates
