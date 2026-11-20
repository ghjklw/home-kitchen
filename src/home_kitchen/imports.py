"""Fetch only public supported recipe hosts. Import produces a reviewable draft."""

import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, HttpUrl
from recipe_scrapers import scrape_html
from starlette.concurrency import run_in_threadpool

from home_kitchen.models import (
    GuidedCookingStep,
    PrepCategory,
    RecipeType,
    infer_recipe_metadata,
)

ODA_HOSTS = {"oda.com", "www.oda.com"}
ALLOWED_HOSTS = {"matprat.no", "www.matprat.no", "marmiton.org", "www.marmiton.org", *ODA_HOSTS}
MAX_BYTES = 2_000_000
MAX_JSONLD_NODES = 5_000
MAX_JSONLD_DEPTH = 32
USER_AGENT = "HomeKitchenBot/0.1 (+https://github.com/ghjklw/home-kitchen)"
_ODA_RECIPE_PATH = re.compile(r"^/no/recipes/[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*/?$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)


class ImportedDraft(BaseModel):
    title: str
    servings: int
    ingredients: list[str]
    instructions: list[str]
    source_url: HttpUrl
    image_url: HttpUrl | None = None
    recipe_type: RecipeType | None = None
    prep_time_minutes: int | None = None
    prep_category: PrepCategory | None = None
    guided_steps: list[GuidedCookingStep] = Field(default_factory=list)


def validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        parsed = None
        port = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Import supports public HTTPS Matprat, Marmiton, and Oda recipe pages")
    if parsed.hostname in ODA_HOSTS and (
        not _ODA_RECIPE_PATH.fullmatch(parsed.path) or parsed.query or parsed.fragment
    ):
        raise ValueError("Oda imports require an exact public Norwegian recipe page URL")


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.documents: list[object] = []
        self._reading = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "script":
            attributes = {name.casefold(): value for name, value in attrs}
            if (attributes.get("type") or "").split(";", 1)[
                0
            ].strip().casefold() == "application/ld+json":
                self._reading = True
                self._parts = []

    def handle_data(self, data):
        if self._reading:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() == "script" and self._reading:
            content = "".join(self._parts).strip()
            if content:
                try:
                    self.documents.append(json.loads(content))
                except (json.JSONDecodeError, ValueError, RecursionError):
                    pass
            self._reading = False
            self._parts = []


def _recipe_objects(value):
    stack = [(value, 0)]
    visited = 0
    while stack and visited < MAX_JSONLD_NODES:
        current, depth = stack.pop()
        visited += 1
        if depth > MAX_JSONLD_DEPTH:
            continue
        if isinstance(current, list):
            stack.extend((child, depth + 1) for child in reversed(current[:MAX_JSONLD_NODES]))
        elif isinstance(current, dict):
            kinds = current.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if isinstance(kinds, list) and "Recipe" in kinds:
                yield current
            children = [child for child in current.values() if isinstance(child, (dict, list))]
            stack.extend((child, depth + 1) for child in reversed(children))


def _oda_text_steps(value) -> list[str]:
    if value is None:
        return []
    steps = []
    stack = [(value, 0)]
    visited = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > MAX_JSONLD_NODES or depth > MAX_JSONLD_DEPTH:
            raise ValueError("Oda recipe instructions exceed supported limits")
        if isinstance(current, str):
            text = current.strip()
            if not text:
                raise ValueError("Oda recipe instructions contain a blank step")
            steps.append(text)
        elif isinstance(current, list):
            if len(current) > MAX_JSONLD_NODES - visited:
                raise ValueError("Oda recipe instructions exceed supported limits")
            stack.extend((item, depth + 1) for item in reversed(current))
        elif isinstance(current, dict):
            if "text" in current:
                child = current["text"]
            elif current.get("@type") == "HowToSection" and "itemListElement" in current:
                child = current["itemListElement"]
            elif "name" in current:
                child = current["name"]
            else:
                raise ValueError("Oda recipe contains an unsupported instruction entry")
            stack.append((child, depth + 1))
        else:
            raise ValueError("Oda recipe contains an unsupported instruction entry")
    return steps


def _oda_servings(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if not isinstance(value, str):
        return 0
    match = re.fullmatch(r"\s*([1-9][0-9]*)\s*(?:porsjoner?|servings?)?\s*", value, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _parse_schema_duration_minutes(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = int(round(value))
        return num if num > 0 else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        num = int(stripped)
        return num if num > 0 else None
    match = _ISO_DURATION_RE.fullmatch(stripped)
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    total = days * 1440 + hours * 60 + minutes + (1 if seconds >= 30 else 0)
    return total if total > 0 else None


def _extract_schema_labels(value) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str):
                out.extend(part.strip() for part in item.split(",") if part.strip())
        return out
    return []


def _parse_oda_recipe(html_text: str, url: str) -> ImportedDraft:
    parser = _JsonLdParser()
    parser.feed(html_text)
    recipes = [recipe for doc in parser.documents for recipe in _recipe_objects(doc)]
    if len(recipes) != 1:
        raise ValueError("Oda page did not contain one unambiguous structured recipe")
    recipe = recipes[0]
    title = recipe.get("name")
    raw_ingredients = recipe.get("recipeIngredient")
    ingredients = []
    if isinstance(raw_ingredients, list) and all(
        isinstance(item, str) and item.strip() for item in raw_ingredients
    ):
        ingredients = [item.strip() for item in raw_ingredients]
    instructions = _oda_text_steps(recipe.get("recipeInstructions"))
    servings = _oda_servings(recipe.get("recipeYield"))
    if (
        not isinstance(title, str)
        or not title.strip()
        or not ingredients
        or any(not item for item in ingredients)
        or not instructions
        or servings < 1
    ):
        raise ValueError("Oda page does not expose a complete structured recipe")

    raw_image = recipe.get("image")
    if isinstance(raw_image, list):
        raw_image = raw_image[0] if raw_image else None
    if isinstance(raw_image, dict):
        raw_image = raw_image.get("url") or raw_image.get("contentUrl")
    image = None
    if isinstance(raw_image, str):
        try:
            candidate = HttpUrl(html.unescape(raw_image))
            parsed_image = urlparse(str(candidate))
            if (
                parsed_image.scheme == "https"
                and parsed_image.hostname
                and (
                    parsed_image.hostname == "oda.com" or parsed_image.hostname.endswith(".oda.com")
                )
                and parsed_image.port in (None, 443)
                and not parsed_image.username
                and not parsed_image.password
            ):
                image = candidate
        except (ValueError, TypeError):
            pass
    raw_minutes = _parse_schema_duration_minutes(recipe.get("totalTime"))
    if raw_minutes is None:
        raw_minutes = _parse_schema_duration_minutes(recipe.get("prepTime"))
    recipe_type, prep_time_minutes, prep_category = infer_recipe_metadata(
        title=title.strip(),
        ingredients=ingredients,
        categories=_extract_schema_labels(recipe.get("recipeCategory")),
        tags=_extract_schema_labels(recipe.get("keywords")),
        prep_time_minutes=raw_minutes,
        servings=servings,
    )
    return ImportedDraft(
        title=title.strip(),
        servings=servings,
        ingredients=ingredients,
        instructions=instructions,
        source_url=HttpUrl(url),
        image_url=image,
        recipe_type=recipe_type,
        prep_time_minutes=prep_time_minutes,
        prep_category=prep_category,
    )


def _scraper_optional(fn) -> object:
    try:
        return fn()
    except Exception:
        return None


def parse_recipe(html: str, url: str) -> ImportedDraft:
    validate_url(url)
    if urlparse(url).hostname in ODA_HOSTS:
        return _parse_oda_recipe(html, url)
    scraper = scrape_html(html, org_url=url)
    # Keep source ingredient text for human review: quantities are not guessed.
    title = scraper.title()
    ingredients = scraper.ingredients() or scraper.schema.ingredients()
    yield_text = scraper.yields()
    match = re.search(r"\d+", yield_text)
    servings = max(1, int(match.group()) if match else 1)
    try:
        image = HttpUrl(scraper.image())
        if image.scheme != "https" or image.username or image.password:
            image = None
    except (ValueError, TypeError, AttributeError):
        image = None
    raw_total_time = _scraper_optional(scraper.total_time)
    if raw_total_time is None and hasattr(scraper, "prep_time"):
        raw_total_time = _scraper_optional(scraper.prep_time)
    categories: list[str] = []
    for getter_name in ("category", "cuisine"):
        getter = getattr(scraper, getter_name, None)
        if callable(getter):
            categories.extend(_extract_schema_labels(_scraper_optional(getter)))
    recipe_type, prep_time_minutes, prep_category = infer_recipe_metadata(
        title=title,
        ingredients=ingredients,
        categories=categories,
        prep_time_minutes=raw_total_time if isinstance(raw_total_time, (int, float)) else None,
        servings=servings,
    )
    return ImportedDraft(
        title=title,
        servings=servings,
        ingredients=ingredients,
        instructions=[step.strip() for step in scraper.instructions().splitlines() if step.strip()],
        source_url=HttpUrl(url),
        image_url=image,
        recipe_type=recipe_type,
        prep_time_minutes=prep_time_minutes,
        prep_category=prep_category,
    )


async def import_recipe(url: str) -> ImportedDraft:
    validate_url(url)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            validate_url(url)
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", ""):
                    raise ValueError("Recipe source did not return HTML")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ValueError("Recipe page exceeds import size limit")
                return await run_in_threadpool(parse_recipe, body.decode("utf-8"), url)
    raise ValueError("Recipe source redirected too many times")
