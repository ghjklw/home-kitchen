"""Bounded, read-only discovery across public and explicitly connected sources."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Callable, Sequence
from itertools import zip_longest
from typing import Annotated, Any, Literal, Protocol, cast

from pydantic import BaseModel, Field, HttpUrl, StringConstraints, field_validator

from home_kitchen.recipes.models import EvidenceSnippet
from home_kitchen.recipes.ports import HouseholdPreferences

MAX_RESULTS = 10

DiscoveryQuery = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
RecipeSource = Literal["oda", "marmiton", "matprat"]


class RecipeDiscoveryRequest(BaseModel):
    query: DiscoveryQuery
    sources: list[Literal["oda", "marmiton", "matprat"]] | None = None
    limit: int = Field(default=10, ge=1, le=MAX_RESULTS)

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Recipe sources must be unique")
        return value


class RecipeDiscoveryItem(BaseModel):
    source: RecipeSource
    title: str = Field(min_length=1, max_length=300)
    url: HttpUrl | None = None
    image_url: HttpUrl | None = None
    source_attribution: str | None = Field(default=None, max_length=200)
    duration: str | None = Field(default=None, max_length=100)
    difficulty: str | None = Field(default=None, max_length=100)
    importable: bool
    detail: str | None = Field(default=None, max_length=300)


class ProviderSearch(BaseModel):
    status: Literal["ok", "connection_required", "unsupported", "unavailable", "timed_out"] = "ok"
    detail: str | None = Field(default=None, max_length=300)
    index_url: HttpUrl | None = None
    results: list[RecipeDiscoveryItem] = Field(default_factory=list, max_length=MAX_RESULTS)


class RecipeSourceStatus(BaseModel):
    source: RecipeSource
    status: Literal["ok", "connection_required", "unsupported", "unavailable", "timed_out"]
    detail: str | None = Field(default=None, max_length=300)
    index_url: HttpUrl | None = None
    result_count: int = Field(default=0, ge=0, le=MAX_RESULTS)


class RecipeDiscoveryResponse(BaseModel):
    query: str
    results: list[RecipeDiscoveryItem] = Field(default_factory=list, max_length=MAX_RESULTS)
    sources: list[RecipeSourceStatus]
    evidence_ids: list[str] = Field(default_factory=list)


class DiscoveryContext(BaseModel):
    """Household dietary preferences and feedback/rating signals for discovery ranking."""

    dietary_preferences: list[str] = Field(default_factory=list)
    likes: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    evidence: list[EvidenceSnippet] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


_FEEDBACK_RECIPE_TITLE = re.compile(r'^Feedback for recipe "([^"]+)"')
_WORD_RE = re.compile(r"[a-z0-9æøåäöéèêàçùôû-]+", re.IGNORECASE)
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "our",
        "your",
        "was",
        "were",
        "are",
        "not",
        "too",
        "very",
        "more",
        "less",
        "make",
        "made",
        "again",
        "would",
        "rating",
        "feedback",
        "recipe",
        "recipes",
        "notes",
        "note",
        "liked",
        "like",
        "love",
        "loved",
        "prefer",
        "prefers",
        "preferred",
        "favorite",
        "favourite",
        "avoid",
        "dislike",
        "disliked",
        "hate",
        "hated",
        "dinner",
        "meal",
        "meals",
        "dish",
        "food",
        "allergy",
        "allergic",
        "without",
        "med",
        "og",
        "til",
        "som",
        "ikke",
        "liker",
        "unngå",
        "uten",
        "veldig",
        "godt",
        "oppskrift",
    }
)
_NEGATIVE_WORDS = (
    "avoid",
    "dislike",
    "hate",
    "don't like",
    "do not like",
    "not a fan",
    "too salty",
    "never again",
    "allergic",
    "allergy",
    "unngå",
    "liker ikke",
    "tåler ikke",
    "allergi",
)
_POSITIVE_WORDS = (
    "love",
    "loved",
    "like",
    "liked",
    "prefer",
    "prefers",
    "favorite",
    "favourite",
    "great",
    "delicious",
    "enjoyed",
    "godt",
    "liker",
    "favoritt",
)
_AVOID_PREFIXES = ("no ", "avoid ", "without ", "uten ", "ikke ", "allergi mot ")


def _dedupe_preserve(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        cleaned = " ".join(item.split()).strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            ordered.append(cleaned)
    return ordered


def _parse_evidence_payload(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError):
        pass
    return {"kind": "feedback", "message": text, "structured": {}}


def _parse_structured(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    return {}


def build_discovery_context(
    household: HouseholdPreferences, evidence: Sequence[EvidenceSnippet]
) -> DiscoveryContext:
    """Summarize household dietary preferences and recorded feedback/rating snippets."""
    dietary_preferences = _dedupe_preserve(household.dietary_preferences)
    likes: list[str] = []
    dislikes: list[str] = []
    notes: list[str] = []
    relevant_evidence: list[EvidenceSnippet] = []
    evidence_ids: list[str] = []

    for snippet in evidence:
        payload = _parse_evidence_payload(snippet.text)
        kind = payload.get("kind")
        if kind is not None and kind not in {"preference", "feedback", "recipe_rating"}:
            continue

        relevant_evidence.append(snippet)
        evidence_ids.append(snippet.evidence_id)

        message = str(payload.get("message") or "").strip()
        structured = _parse_structured(payload.get("structured"))
        if message:
            notes.append(message)

        targets: list[str] = []
        if message:
            title_match = _FEEDBACK_RECIPE_TITLE.match(message)
            if title_match:
                targets.append(title_match.group(1))
        for key in ("recipe_title", "title", "ingredient", "item", "dish"):
            val = structured.get(key)
            if isinstance(val, str) and val.strip():
                targets.append(val.strip())
        if not targets and message:
            targets.append(message)

        polarity: Literal["like", "dislike"] | None = None
        rating = structured.get("rating")
        if isinstance(rating, (int, float)) and not isinstance(rating, bool):
            if rating >= 4:
                polarity = "like"
            elif rating <= 2:
                polarity = "dislike"
        if polarity is None and isinstance(structured.get("would_make_again"), bool):
            polarity = "like" if structured["would_make_again"] else "dislike"
        if polarity is None and isinstance(structured.get("liked"), bool):
            polarity = "like" if structured["liked"] else "dislike"
        if polarity is None:
            pref_val = str(structured.get("preference") or "").strip().casefold()
            if pref_val in {
                "avoid",
                "dislike",
                "exclude",
                "no",
                "hate",
                "never",
                "allergy",
                "allergic",
            }:
                polarity = "dislike"
            elif pref_val in {"like", "prefer", "love", "favorite", "favourite", "yes"}:
                polarity = "like"

        combined_text = " ".join(
            part for part in (message, str(structured.get("notes") or "")) if part
        ).casefold()
        if polarity is None and combined_text:
            if any(
                marker in combined_text for marker in _NEGATIVE_WORDS
            ) or combined_text.startswith(_AVOID_PREFIXES):
                polarity = "dislike"
            elif any(marker in combined_text for marker in _POSITIVE_WORDS):
                polarity = "like"

        if polarity == "like":
            likes.extend(targets)
        elif polarity == "dislike":
            dislikes.extend(targets)

    return DiscoveryContext(
        dietary_preferences=dietary_preferences,
        likes=_dedupe_preserve(likes),
        dislikes=_dedupe_preserve(dislikes),
        notes=_dedupe_preserve(notes),
        evidence=relevant_evidence,
        evidence_ids=_dedupe_preserve(evidence_ids),
    )


def _phrase_tokens(phrase: str) -> set[str]:
    return {
        token.casefold()
        for token in _WORD_RE.findall(phrase)
        if len(token) >= 3 and token.casefold() not in _STOPWORDS
    }


def _match_weight(title_text: str, title_tokens: set[str], phrase: str) -> int:
    normalized = " ".join(phrase.casefold().split())
    if not normalized:
        return 0
    if len(normalized) >= 3 and normalized in title_text:
        return 4
    overlap = title_tokens & _phrase_tokens(normalized)
    return len(overlap)


def _score_item(item: RecipeDiscoveryItem, context: DiscoveryContext | None) -> int:
    if context is None:
        return 0
    title_text = " ".join(part for part in (item.title, item.detail or "") if part).casefold()
    title_tokens = {token.casefold() for token in _WORD_RE.findall(title_text)}
    score = 0

    for pref in context.dietary_preferences:
        lower = pref.strip().casefold()
        avoided: str | None = None
        for prefix in _AVOID_PREFIXES:
            if lower.startswith(prefix):
                avoided = lower[len(prefix) :].strip()
                break
        if avoided is None and lower.endswith(" allergy"):
            avoided = lower[: -len(" allergy")].strip()
        if avoided:
            score -= 3 * _match_weight(title_text, title_tokens, avoided)
        else:
            score += 2 * _match_weight(title_text, title_tokens, lower)

    for liked in context.likes:
        score += 2 * _match_weight(title_text, title_tokens, liked)

    for disliked in context.dislikes:
        score -= 3 * _match_weight(title_text, title_tokens, disliked)

    return score


def _accepts_context(search_fn: Any) -> bool:
    try:
        params = inspect.signature(search_fn).parameters
    except (TypeError, ValueError):
        return False
    return "context" in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


class DiscoveryProvider(Protocol):
    @property
    def source(self) -> Literal["oda", "marmiton", "matprat"]: ...

    timeout_seconds: float

    async def search(
        self, query: str, limit: int, actor_id: str | None = None
    ) -> ProviderSearch: ...


class RecipeDiscoveryRegistry:
    """Run sources concurrently with independent timeouts and partial-result behavior."""

    def __init__(
        self,
        providers: Sequence[DiscoveryProvider],
        *,
        context_provider: Callable[[str], DiscoveryContext] | None = None,
    ):
        self.providers = {provider.source: provider for provider in providers}
        self.context_provider = context_provider

    async def search(
        self,
        request: RecipeDiscoveryRequest,
        *,
        actor_id: str | None = None,
        context: DiscoveryContext | None = None,
    ) -> RecipeDiscoveryResponse:
        selected = request.sources if request.sources is not None else list(self.providers)
        unknown = [source for source in selected if source not in self.providers]
        if unknown:
            raise ValueError("One or more requested recipe sources are unavailable")

        resolved_context = (
            context
            if context is not None
            else (
                self.context_provider(request.query) if self.context_provider is not None else None
            )
        )

        async def run(source: RecipeSource):
            provider = self.providers[source]
            try:
                if resolved_context is not None and _accepts_context(provider.search):
                    coro = cast(Any, provider).search(
                        request.query, request.limit, actor_id, context=resolved_context
                    )
                else:
                    coro = provider.search(request.query, request.limit, actor_id)
                response = await asyncio.wait_for(
                    coro,
                    timeout=provider.timeout_seconds,
                )
                return source, response
            except TimeoutError:
                return source, ProviderSearch(status="timed_out", detail="Source search timed out.")
            except Exception:
                return source, ProviderSearch(
                    status="unavailable", detail="Source search is temporarily unavailable."
                )

        provider_responses = await asyncio.gather(*(run(source) for source in selected))
        seen_urls: set[str] = set()
        candidates: list[tuple[int, int, int, RecipeDiscoveryItem]] = []
        source_statuses = [
            RecipeSourceStatus(
                source=source,
                status=response.status,
                detail=response.detail,
                index_url=response.index_url,
            )
            for source, response in provider_responses
        ]
        # Interleave each source's ranked results, then apply household feedback/preference ranking
        # with the original interleaved position as tie-breaker, keeping external attribution intact.
        original_index = 0
        for batch in zip_longest(*(response.results for _, response in provider_responses)):
            for index, item in enumerate(batch):
                if item is None:
                    continue
                key = str(item.url) if item.url is not None else None
                if key is not None and key in seen_urls:
                    continue
                if key is not None:
                    seen_urls.add(key)
                candidates.append(
                    (-_score_item(item, resolved_context), original_index, index, item)
                )
                original_index += 1

        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        results: list[RecipeDiscoveryItem] = []
        for _, _, index, item in candidates[: request.limit]:
            results.append(item)
            source_statuses[index].result_count += 1

        return RecipeDiscoveryResponse(
            query=request.query,
            results=results,
            sources=source_statuses,
            evidence_ids=list(resolved_context.evidence_ids)
            if resolved_context is not None
            else [],
        )


def __getattr__(name: str) -> Any:
    if name == "OdaDiscoveryProvider":
        from home_kitchen.recipes.adapters.oda import OdaDiscoveryProvider

        return OdaDiscoveryProvider
    if name in {
        "MARMITON_INDEX",
        "MARMITON_SEARCH",
        "MATPRAT_INDEX",
        "MAX_HTML_NODES",
        "MAX_RESPONSE_BYTES",
        "USER_AGENT",
        "MarmitonDiscoveryProvider",
        "MatpratDiscoveryProvider",
    }:
        from home_kitchen.recipes.adapters import discovery as _discovery_adapters

        return getattr(_discovery_adapters, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
