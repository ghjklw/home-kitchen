"""Cookidoo collection adapter and recipe import service.

Fetches user's Cookidoo collections / saved recipes via httpx (with injectable
transport for offline tests), normalizes them into CollectionEntry / CollectionJob
shapes with duplicate detection, and imports selected recipes into Kitchen
preserving source="cookidoo" and source_url without overwriting existing saved recipes.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from home_kitchen.connector_store import ConnectorStore, connector_credentials
from home_kitchen.history import HistoryEntryCreate, HistoryRepository
from home_kitchen.imports import ImportedDraft
from home_kitchen.models import (
    Recipe,
    RecipeSource,
    SourceIngredient,
    infer_recipe_metadata,
)
from home_kitchen.recipes.collections import (
    CollectionEntry,
    CollectionPage,
    source_ingredient,
    source_links,
)
from home_kitchen.service import KitchenService
from home_kitchen.store import entity_values, recipes

_LOG = logging.getLogger(__name__)

DEFAULT_COOKIDOO_NO_URL = "https://cookidoo.international"
_COOKIDOO_INTERNATIONAL_COUNTRIES = frozenset({"", "no", "nb", "nn", "nb-no", "international"})
_COOKIDOO_HOST_RE = re.compile(
    r"(?:[a-z0-9-]+\.)*cookidoo\.(?:[a-z]{2}|com|co\.uk|com\.au|international|vorwerk-digital\.com)"
    r"|(?:[a-z0-9-]+\.)*cookidoo\.thermomix\.com"
)


def _cookidoo_domain_for_country(country: str | None) -> str:
    country_clean = (country or "").strip().lower()
    if country_clean in _COOKIDOO_INTERNATIONAL_COUNTRIES:
        return "cookidoo.international"
    return f"cookidoo.{country_clean}"


def _dialect_insert(table, dialect_name: str):
    if dialect_name == "postgresql":
        return pg_insert(table)
    if dialect_name == "sqlite":
        return sqlite_insert(table)
    raise ValueError(f"unsupported database dialect: {dialect_name}")


class CookidooError(HTTPException):
    """Bokmål HTTP exception for Cookidoo integration failures."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


_COOKIDOO_IMAGE_TRANSFORMATION = "t_web_rdp_recipe_584x480_1_5x"


def _seconds_to_minutes(raw_val: Any) -> int | None:
    if isinstance(raw_val, bool) or raw_val is None:
        return None
    if isinstance(raw_val, (int, float)):
        return max(1, round(raw_val / 60)) if raw_val > 0 else None
    if isinstance(raw_val, str):
        stripped = raw_val.strip()
        try:
            sec = float(stripped)
        except ValueError:
            return None
        return max(1, round(sec / 60)) if sec > 0 else None
    return None


def _extract_cookidoo_labels(raw_val: Any) -> list[str]:
    if isinstance(raw_val, str):
        return [part.strip() for part in raw_val.split(",") if part.strip()]
    if not isinstance(raw_val, list):
        return []
    labels: list[str] = []
    for item in raw_val:
        if isinstance(item, str) and item.strip():
            labels.append(item.strip())
        elif isinstance(item, dict):
            val = (
                item.get("title")
                or item.get("name")
                or item.get("label")
                or item.get("category")
                or item.get("value")
            )
            if isinstance(val, str) and val.strip():
                labels.append(val.strip())
    return labels


def _strip_html(text: str) -> str:
    with_spaces = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    stripped = re.sub(r"<[^>]+>", "", with_spaces)
    return re.sub(r"\s+", " ", html.unescape(stripped)).strip()


def _sanitize_cookidoo_image_url(raw_url: Any, domain: str) -> str | None:
    if not isinstance(raw_url, str):
        return None
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    if "{transformation}" in cleaned:
        cleaned = cleaned.replace("{transformation}", _COOKIDOO_IMAGE_TRANSFORMATION)
    if cleaned.startswith("/"):
        cleaned = f"https://{domain}{cleaned}"
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    return cleaned


def _parse_int_like(val: Any) -> int | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        num = int(round(val))
        return num if num > 0 else None
    if isinstance(val, str):
        match = re.search(r"\d+", val)
        if match:
            num = int(match.group(0))
            return num if num > 0 else None
    return None


def _extract_servings(raw: dict[str, Any]) -> int:
    candidates: list[Any] = [raw.get("servings")]
    if isinstance(raw.get("yield"), dict):
        candidates.append(raw["yield"].get("value"))
    if isinstance(raw.get("servingSize"), dict):
        serving_size = raw["servingSize"]
        if isinstance(serving_size.get("quantity"), dict):
            candidates.append(serving_size["quantity"].get("value"))
        candidates.append(serving_size.get("value"))
    candidates.append(raw.get("recipeYield"))
    for cand in candidates:
        parsed = _parse_int_like(cand)
        if parsed is not None:
            return parsed
    return 4


def _format_ingredient_item(item: Any) -> str | None:
    if isinstance(item, str):
        cleaned = _strip_html(item)
        return cleaned or None
    if not isinstance(item, dict):
        return None
    if any(k in item for k in ("ingredientNotation", "unitNotation", "quantity", "preparation")):
        qty_obj = item.get("quantity")
        qty_val = qty_obj.get("value") if isinstance(qty_obj, dict) else qty_obj
        qty_str = ""
        if isinstance(qty_val, (int, float)) and not isinstance(qty_val, bool):
            qty_str = str(int(qty_val)) if float(qty_val).is_integer() else str(qty_val)
        elif isinstance(qty_val, str) and qty_val.strip():
            qty_str = qty_val.strip()
        unit_str = str(item.get("unitNotation") or item.get("unit") or "").strip()
        name_str = str(
            item.get("ingredientNotation")
            or item.get("text")
            or item.get("name")
            or item.get("ingredient")
            or ""
        ).strip()
        prep_str = str(item.get("preparation") or "").strip()
        main = " ".join(part for part in (qty_str, unit_str, name_str) if part)
        if main and prep_str and prep_str not in main:
            line = f"{main}, {prep_str}"
        else:
            line = main or prep_str
        cleaned = _strip_html(line)
        return cleaned or None
    text_val = item.get("text") or item.get("name") or item.get("ingredient")
    if text_val and str(text_val).strip():
        cleaned = _strip_html(str(text_val))
        return cleaned or None
    return None


def _format_instruction_item(item: Any) -> str | None:
    if isinstance(item, str):
        cleaned = _strip_html(item)
        return cleaned or None
    if isinstance(item, dict):
        text_val = (
            item.get("formattedText")
            or item.get("text")
            or item.get("description")
            or item.get("instruction")
        )
        if text_val and str(text_val).strip():
            cleaned = _strip_html(str(text_val))
            return cleaned or None
    return None


def normalize_cookidoo_recipe(
    raw: dict[str, Any],
    provider: str = "cookidoo",
    country: str = "no",
    locale: str | None = None,
) -> CollectionEntry:
    """Normalize raw Cookidoo API recipe or created recipe into CollectionEntry."""
    if isinstance(raw.get("recipeContent"), dict):
        content = raw["recipeContent"]
        raw = {**content, **{k: v for k, v in raw.items() if k != "recipeContent"}}

    country_clean = (country or "no").strip().lower() or "no"
    domain = _cookidoo_domain_for_country(country_clean)
    effective_locale = locale or (
        "nb-NO"
        if country_clean in _COOKIDOO_INTERNATIONAL_COUNTRIES
        else f"{country_clean}-{country_clean.upper()}"
    )

    recipe_id = str(
        raw.get("id") or raw.get("recipeId") or raw.get("external_id") or raw.get("recipe_id") or ""
    ).strip()

    raw_title = raw.get("title") or raw.get("recipeName") or raw.get("name") or "Uten tittel"
    title = _strip_html(str(raw_title)) or "Uten tittel"

    source_url = raw.get("url") or raw.get("source_url") or raw.get("sourceUrl")
    if not source_url and recipe_id:
        source_url = f"https://{domain}/recipes/recipe/{effective_locale}/{recipe_id}"

    raw_image = raw.get("imageUrl") or raw.get("image_url") or raw.get("image")
    if not raw_image and isinstance(raw.get("descriptiveAssets"), list):
        for asset in raw["descriptiveAssets"]:
            if isinstance(asset, dict):
                cand = (
                    asset.get("square")
                    or asset.get("landscape")
                    or asset.get("portrait")
                    or asset.get("url")
                )
                if cand:
                    raw_image = cand
                    break
    if not raw_image:
        raw_image = raw.get("listThumbnail") or raw.get("landscapeImage")
    image_url = _sanitize_cookidoo_image_url(raw_image, domain)

    servings = _extract_servings(raw)

    # Extract ingredients (flat list or grouped recipeIngredientGroups)
    raw_ingredients = (
        raw.get("ingredients")
        or raw.get("ingredient_list")
        or raw.get("recipeIngredients")
        or raw.get("recipeIngredient")
        or []
    )
    ingredients: list[str] = []
    if isinstance(raw_ingredients, list):
        for item in raw_ingredients:
            formatted = _format_ingredient_item(item)
            if formatted:
                ingredients.append(formatted)
    if not ingredients and isinstance(raw.get("recipeIngredientGroups"), list):
        for group in raw["recipeIngredientGroups"]:
            if not isinstance(group, dict):
                continue
            group_items = group.get("recipeIngredients") or group.get("ingredients") or []
            if isinstance(group_items, list):
                for item in group_items:
                    formatted = _format_ingredient_item(item)
                    if formatted:
                        ingredients.append(formatted)

    # Extract instructions (flat list or grouped recipeStepGroups)
    raw_instructions = (
        raw.get("instructions")
        or raw.get("steps")
        or raw.get("step_list")
        or raw.get("recipeInstructions")
        or []
    )
    instructions: list[str] = []
    if isinstance(raw_instructions, list):
        for item in raw_instructions:
            formatted = _format_instruction_item(item)
            if formatted:
                instructions.append(formatted)
    if not instructions and isinstance(raw.get("recipeStepGroups"), list):
        for group in raw["recipeStepGroups"]:
            if not isinstance(group, dict):
                continue
            group_steps = group.get("recipeSteps") or group.get("steps") or []
            if isinstance(group_steps, list):
                for step in group_steps:
                    formatted = _format_instruction_item(step)
                    if formatted:
                        instructions.append(formatted)

    badges = list(raw.get("badges") or ["Lagret i Cookidoo"])

    metadata: dict[str, Any] = {
        "servings": servings,
        "ingredients": ingredients,
        "instructions": instructions,
        "recipe_id": recipe_id,
        "provider": provider,
    }
    if raw.get("prepTime") or raw.get("prep_time"):
        metadata["prep_time"] = raw.get("prepTime") or raw.get("prep_time")
    if raw.get("totalTime") or raw.get("total_time"):
        metadata["total_time"] = raw.get("totalTime") or raw.get("total_time")
    raw_times = raw.get("times")
    if isinstance(raw_times, list):
        for t in raw_times:
            if not isinstance(t, dict):
                continue
            t_type = str(t.get("type") or "").strip()
            qty = t.get("quantity")
            val = qty.get("value") if isinstance(qty, dict) else t.get("value")
            if val is None:
                continue
            if t_type == "prepTime" and "prep_time" not in metadata:
                metadata["prep_time"] = val
            elif t_type == "totalTime" and "total_time" not in metadata:
                metadata["total_time"] = val
    elif isinstance(raw_times, dict):
        if raw_times.get("prepTime") is not None and "prep_time" not in metadata:
            metadata["prep_time"] = raw_times["prepTime"]
        if raw_times.get("totalTime") is not None and "total_time" not in metadata:
            metadata["total_time"] = raw_times["totalTime"]
    if raw.get("tools"):
        metadata["tools"] = raw["tools"]

    raw_seconds = (
        metadata.get("total_time")
        if metadata.get("total_time") is not None
        else metadata.get("prep_time")
    )
    prep_minutes = _seconds_to_minutes(raw_seconds)
    if prep_minutes is None and metadata.get("prep_time") is not None:
        prep_minutes = _seconds_to_minutes(metadata.get("prep_time"))
    categories = _extract_cookidoo_labels(raw.get("categories") or raw.get("recipeCategory"))
    tags = _extract_cookidoo_labels(raw.get("tags") or raw.get("keywords"))
    recipe_type, prep_time_minutes, prep_category = infer_recipe_metadata(
        title=title,
        ingredients=ingredients,
        categories=categories,
        tags=tags,
        prep_time_minutes=prep_minutes,
        servings=servings,
    )

    return CollectionEntry(
        id=recipe_id,
        provider=provider,
        title=title,
        source_url=source_url,
        image_url=image_url,
        badges=badges,
        metadata=metadata,
        servings=servings,
        ingredients=ingredients,
        instructions=instructions,
        recipe_type=recipe_type,
        prep_time_minutes=prep_time_minutes,
        prep_category=prep_category,
    )


class CookidooCollectionProvider:
    """CollectionProvider implementation for Cookidoo / Vorwerk TM6."""

    id = "cookidoo"

    def __init__(
        self,
        store: ConnectorStore,
        household_id: UUID,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ):
        self.store = store
        self.household_id = household_id
        self.transport = transport
        self.base_url = base_url

    def _get_credentials_or_fail(self) -> tuple[dict[str, Any], Any]:
        """Read household's Cookidoo credentials; fail closed with Bokmål error if not connected or expired."""
        meta = self.store.get_connection_metadata(self.household_id, "cookidoo")
        creds = self.store.get_credentials(self.household_id, "cookidoo")

        if meta is None or creds is None:
            raise CookidooError(
                status_code=503,
                detail="Cookidoo er ikke tilkoblet. Koble til Cookidoo under Innstillinger.",
            )

        if meta.is_expired:
            raise CookidooError(
                status_code=401,
                detail="Tilkoblingen til Cookidoo har utløpt. Koble til på nytt under Innstillinger.",
            )

        token = creds.get("access_token") or creds.get("session_cookie")
        if not token or not str(token).strip():
            raise CookidooError(
                status_code=401,
                detail="Ugyldig påloggingsinformasjon for Cookidoo. Koble til på nytt under Innstillinger.",
            )

        return creds, meta

    def generation(self, actor_id: str) -> object:
        """Generation token to fence collection state across disconnects/reconnects."""
        creds, meta = self._get_credentials_or_fail()
        token = creds.get("access_token") or creds.get("session_cookie") or ""
        token_prefix = str(token)[:12]
        return f"{meta.updated_at.isoformat()}:{token_prefix}"

    def lock_generation(self, connection, actor_id: str, generation: object) -> None:
        """Lock and verify Cookidoo credentials within transaction."""
        stmt = select(
            connector_credentials.c.updated_at,
            connector_credentials.c.expires_at,
            connector_credentials.c.encrypted_credentials,
        ).where(
            connector_credentials.c.household_id == self.household_id,
            connector_credentials.c.provider_id == "cookidoo",
        )
        if connection.dialect.name == "postgresql":
            stmt = stmt.with_for_update()
        row = connection.execute(stmt).one_or_none()

        if row is None:
            raise ValueError("Cookidoo-tilkoblingen ble fjernet; forhåndsvis samlingen på nytt")

        if row.expires_at is not None:
            now = datetime.now(UTC)
            row_exp = (
                row.expires_at.replace(tzinfo=UTC)
                if row.expires_at.tzinfo is None
                else row.expires_at.astimezone(UTC)
            )
            if row_exp < now:
                raise ValueError(
                    "Cookidoo-tilkoblingen har utløpt; koble til på nytt under Innstillinger"
                )

        creds = self.store.decrypt(row.encrypted_credentials)
        token = creds.get("access_token") or creds.get("session_cookie") or ""
        row_updated = (
            row.updated_at.replace(tzinfo=UTC)
            if row.updated_at.tzinfo is None
            else row.updated_at.astimezone(UTC)
        )
        current_gen = f"{row_updated.isoformat()}:{str(token)[:12]}"
        if current_gen != generation:
            raise ValueError("Cookidoo-tilkoblingen er endret; forhåndsvis samlingen på nytt")

    def validate_source(self, entry: CollectionEntry) -> None:
        """Validate Cookidoo source URL format and domain."""
        if entry.source_url is None:
            raise ValueError("Cookidoo-oppskriften mangler kildelenke")
        try:
            parsed = urlparse(entry.source_url)
            port = parsed.port
        except (TypeError, ValueError):
            parsed = None
            port = None

        if (
            parsed is None
            or parsed.scheme != "https"
            or not parsed.hostname
            or port not in (None, 443)
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Ugyldig vertsadresse for Cookidoo-oppskrift")

        hostname = parsed.hostname.lower()
        if not _COOKIDOO_HOST_RE.fullmatch(hostname):
            raise ValueError("Cookidoo-kildelenken har ugyldig vertsadresse")

        valid_path = (
            parsed.path.startswith("/recipes/recipe/")
            or parsed.path.startswith("/recipes/")
            or parsed.path.startswith("/created-recipes/")
        )
        if not valid_path:
            raise ValueError("Cookidoo-kildelenken samsvarer ikke med oppskriftens identitet")

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path_or_url: str,
        headers: dict[str, str],
        base_url: str,
        *,
        params: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        if path_or_url.startswith(("https://", "http://")):
            parsed_target = urlparse(path_or_url)
            base_host = (urlparse(base_url).hostname or "").lower()
            target_host = (parsed_target.hostname or "").lower()
            if parsed_target.scheme != "https" or (
                target_host != base_host and not _COOKIDOO_HOST_RE.fullmatch(target_host)
            ):
                raise CookidooError(
                    status_code=502,
                    detail="Cookidoo returnerte en ugyldig pagineringslenke.",
                )
            full_url = path_or_url
        else:
            rel = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
            full_url = f"{base_url.rstrip('/')}{rel}"

        try:
            response = await client.get(full_url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise CookidooError(
                status_code=502,
                detail=f"Nettverksfeil mot Cookidoo: {exc}",
            ) from exc

        if response.status_code in (401, 403):
            raise CookidooError(
                status_code=401,
                detail="Cookidoo avviste forespørselen. Vennligst logg inn på nytt under Innstillinger.",
            )
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code >= 400:
            raise CookidooError(
                status_code=response.status_code,
                detail=f"Cookidoo API returnerte en feil ({response.status_code}). Vennligst prøv igjen senere.",
            )
        try:
            return response.json()
        except ValueError:
            if allow_not_found:
                _LOG.warning("Cookidoo returned non-JSON response for %s", path_or_url)
                return None
            raise CookidooError(
                status_code=502,
                detail="Cookidoo returnerte et ugyldig svar (forventet JSON).",
            ) from None

    async def _fetch_from_api(
        self,
        endpoint_paths: list[str],
        params: dict[str, Any],
        headers: dict[str, str],
        base_url: str,
    ) -> dict[str, Any] | list[Any]:
        """Call Cookidoo API with fallback paths."""
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=15.0,
        ) as client:
            last_error: CookidooError | None = None
            for path in endpoint_paths:
                try:
                    data = await self._get_json(
                        client,
                        path,
                        headers,
                        base_url,
                        params=params or None,
                        allow_not_found=True,
                    )
                except CookidooError as exc:
                    if exc.status_code == 401:
                        raise
                    if exc.status_code == 502:
                        last_error = exc
                        continue
                    raise
                if data is not None:
                    return data

            if last_error is not None:
                raise last_error

            return {"recipes": [], "has_more": False}

    @staticmethod
    def _extract_next_link(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("links", "_links"):
            links = data.get(key)
            if isinstance(links, dict):
                nxt = links.get("next")
                if isinstance(nxt, dict) and isinstance(nxt.get("href"), str) and nxt["href"]:
                    return nxt["href"]
                if isinstance(nxt, str) and nxt:
                    return nxt
            elif isinstance(links, list):
                for item in links:
                    if (
                        isinstance(item, dict)
                        and item.get("rel") == "next"
                        and isinstance(item.get("href"), str)
                        and item["href"]
                    ):
                        return item["href"]
        return None

    async def _fetch_paginated_collection(
        self,
        client: httpx.AsyncClient,
        initial_path: str,
        headers: dict[str, str],
        base_url: str,
        *,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        next_target: str | None = initial_path
        for _ in range(max_pages):
            if not next_target or next_target in seen_urls:
                break
            seen_urls.add(next_target)
            payload = await self._get_json(client, next_target, headers, base_url)
            if not isinstance(payload, dict):
                break
            pages.append(payload)
            next_target = self._extract_next_link(payload)
        return pages

    def _build_request_context(self) -> tuple[dict[str, str], str, str, str]:
        creds, _ = self._get_credentials_or_fail()
        country = str(creds.get("country") or "no").strip().lower() or "no"
        token_type = creds.get("token_type", "Bearer")

        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeKitchen/1.0",
        }
        if creds.get("access_token"):
            headers["Authorization"] = f"{token_type} {creds['access_token']}"
        if creds.get("session_cookie"):
            headers["Cookie"] = creds["session_cookie"]

        domain = _cookidoo_domain_for_country(country)
        base_url = self.base_url or f"https://{domain}"
        locale = (
            "nb-NO"
            if country in _COOKIDOO_INTERNATIONAL_COUNTRIES
            else f"{country}-{country.upper()}"
        )
        return headers, base_url, country, locale

    async def _hydrate_recipe_summaries(
        self,
        client: httpx.AsyncClient,
        items: list[dict[str, Any]],
        headers: dict[str, str],
        base_url: str,
        country: str,
    ) -> list[CollectionEntry]:
        domain = _cookidoo_domain_for_country(country)
        sem = asyncio.Semaphore(5)

        async def _resolve_one(item: dict[str, Any]) -> CollectionEntry:
            recipe_id = str(item["id"]).strip()
            badges = list(dict.fromkeys(item.get("badges") or ["Lagret i Cookidoo"]))
            fallback_thumb = _sanitize_cookidoo_image_url(item.get("thumbnail"), domain)
            raw_inline = item.get("raw")
            inline_entry: CollectionEntry | None = None
            if isinstance(raw_inline, dict) and (
                raw_inline.get("title") or raw_inline.get("recipeName") or raw_inline.get("name")
            ):
                merged_raw = dict(raw_inline)
                merged_raw["badges"] = badges
                inline_entry = normalize_cookidoo_recipe(
                    merged_raw, provider=self.id, country=country, locale="en"
                )
                if inline_entry.image_url is not None:
                    return inline_entry

            async with sem:
                recipe_data: dict[str, Any] | list[Any] | None = None
                try:
                    recipe_data = await self._get_json(
                        client,
                        f"/recipes/recipe/en/{recipe_id}",
                        headers,
                        base_url,
                        allow_not_found=True,
                    )
                except CookidooError as exc:
                    if exc.status_code == 401:
                        raise
                    recipe_data = None

            if isinstance(recipe_data, dict):
                raw_recipe = (
                    recipe_data.get("recipe")
                    if isinstance(recipe_data.get("recipe"), dict)
                    else recipe_data
                )
                if isinstance(raw_recipe, dict):
                    merged_raw = dict(raw_inline) if isinstance(raw_inline, dict) else {}
                    merged_raw.update(raw_recipe)
                    merged_raw.setdefault("id", recipe_id)
                    merged_raw["badges"] = badges
                    entry = normalize_cookidoo_recipe(
                        merged_raw, provider=self.id, country=country, locale="en"
                    )
                    if not entry.image_url and fallback_thumb:
                        entry.image_url = fallback_thumb
                    return entry

            if inline_entry is not None:
                if not inline_entry.image_url and fallback_thumb:
                    inline_entry.image_url = fallback_thumb
                return inline_entry

            primary_badge = badges[0] if badges else "Cookidoo"
            return CollectionEntry(
                id=recipe_id,
                provider=self.id,
                title=f"{primary_badge} ({recipe_id})",
                source_url=f"https://{domain}/recipes/recipe/en/{recipe_id}",
                image_url=fallback_thumb,
                badges=badges,
                metadata={"recipe_id": recipe_id, "provider": self.id},
            )

        return list(await asyncio.gather(*(_resolve_one(item) for item in items)))

    async def extract_entry(self, entry: CollectionEntry) -> ImportedDraft:
        """Fetch detailed recipe ingredients and instructions from Cookidoo when missing from list."""
        self.validate_source(entry)
        assert entry.source_url is not None

        inline_ingredients = entry.ingredients or entry.metadata.get("ingredients") or []
        inline_instructions = entry.instructions or entry.metadata.get("instructions") or []
        if inline_ingredients and inline_instructions:
            servings = entry.servings or entry.metadata.get("servings") or 4
            recipe_type, prep_time_minutes, prep_category = infer_recipe_metadata(
                title=entry.title,
                ingredients=inline_ingredients,
                categories=entry.badges,
                prep_time_minutes=entry.prep_time_minutes,
                servings=servings,
                recipe_type=entry.recipe_type,
                prep_category=entry.prep_category,
            )
            return ImportedDraft(
                title=entry.title,
                servings=servings,
                ingredients=list(inline_ingredients),
                instructions=list(inline_instructions),
                source_url=entry.source_url,
                image_url=entry.image_url,
                recipe_type=recipe_type,
                prep_time_minutes=prep_time_minutes,
                prep_category=prep_category,
            )

        headers, base_url, country, locale = self._build_request_context()
        parsed = urlparse(entry.source_url)

        if parsed.path.startswith("/created-recipes/"):
            async with httpx.AsyncClient(transport=self.transport, timeout=15.0) as client:
                pages = await self._fetch_paginated_collection(
                    client, "/created-recipes/en", headers, base_url
                )
            for page_data in pages:
                items = page_data.get("items") or page_data.get("createdRecipes") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_id = str(
                        item.get("recipeId") or item.get("id") or item.get("recipe_id") or ""
                    ).strip()
                    if item_id == entry.id:
                        detail = normalize_cookidoo_recipe(
                            item, provider=self.id, country=country, locale="en"
                        )
                        if not detail.ingredients or not detail.instructions:
                            raise ValueError(
                                "Cookidoo-oppskriften mangler ingredienser eller fremgangsmåte"
                            )
                        return ImportedDraft(
                            title=detail.title or entry.title,
                            servings=detail.servings or entry.servings or 4,
                            ingredients=detail.ingredients,
                            instructions=detail.instructions,
                            source_url=entry.source_url,
                            image_url=detail.image_url or entry.image_url,
                            recipe_type=detail.recipe_type or entry.recipe_type,
                            prep_time_minutes=(
                                detail.prep_time_minutes
                                if detail.prep_time_minutes is not None
                                else entry.prep_time_minutes
                            ),
                            prep_category=detail.prep_category or entry.prep_category,
                        )
            raise ValueError("Fant ikke den egne Cookidoo-oppskriften for import")

        candidate_paths = [
            f"/recipes/recipe/en/{entry.id}",
            parsed.path,
            f"/api/v2/recipes/{entry.id}",
        ]
        deduped_paths = list(dict.fromkeys(p for p in candidate_paths if p))
        data = await self._fetch_from_api(deduped_paths, {}, headers, base_url)
        raw_recipe = (
            data.get("recipe")
            if isinstance(data, dict) and isinstance(data.get("recipe"), dict)
            else data
        )
        if not isinstance(raw_recipe, dict):
            raise ValueError("Cookidoo returnerte ugyldig oppskriftsformat")
        detail = normalize_cookidoo_recipe(
            raw_recipe,
            provider=self.id,
            country=country,
            locale=locale,
        )
        if not detail.ingredients or not detail.instructions:
            raise ValueError("Cookidoo-oppskriften mangler ingredienser eller fremgangsmåte")
        return ImportedDraft(
            title=detail.title or entry.title,
            servings=detail.servings or entry.servings or 4,
            ingredients=detail.ingredients,
            instructions=detail.instructions,
            source_url=entry.source_url,
            image_url=detail.image_url or entry.image_url,
            recipe_type=detail.recipe_type or entry.recipe_type,
            prep_time_minutes=(
                detail.prep_time_minutes
                if detail.prep_time_minutes is not None
                else entry.prep_time_minutes
            ),
            prep_category=detail.prep_category or entry.prep_category,
        )

    @staticmethod
    def _collect_managed_list_items(
        detail: dict[str, Any],
        col_title: str,
        col_thumb: Any,
        by_id: dict[str, dict[str, Any]],
    ) -> None:
        def _record(recipe_id: str, raw: dict[str, Any] | None = None) -> None:
            rid = recipe_id.strip()
            if not rid:
                return
            slot = by_id.get(rid)
            if slot is None:
                slot = {"id": rid, "badges": [col_title], "thumbnail": col_thumb, "raw": raw}
                by_id[rid] = slot
            else:
                if col_title not in slot["badges"]:
                    slot["badges"].append(col_title)
                if not slot.get("raw") and raw:
                    slot["raw"] = raw
                if not slot.get("thumbnail") and col_thumb:
                    slot["thumbnail"] = col_thumb

        chapters = detail.get("chapters")
        if isinstance(chapters, list):
            for chapter in chapters:
                if not isinstance(chapter, dict):
                    continue
                for rec in chapter.get("recipes") or []:
                    if isinstance(rec, str):
                        _record(rec)
                    elif isinstance(rec, dict):
                        rid = str(
                            rec.get("id") or rec.get("recipeId") or rec.get("recipe_id") or ""
                        )
                        _record(rid, rec)
                for rid in chapter.get("recipeIds") or []:
                    if isinstance(rid, str):
                        _record(rid)

        for rec in detail.get("recipes") or []:
            if isinstance(rec, str):
                _record(rec)
            elif isinstance(rec, dict):
                rid = str(rec.get("id") or rec.get("recipeId") or rec.get("recipe_id") or "")
                _record(rid, rec)

        for rid in detail.get("recipeIds") or []:
            if isinstance(rid, str):
                _record(rid)

    async def page(self, actor_id: str, kind: str, page: int, size: int) -> CollectionPage:
        """Fetch a page of recipes for the given collection kind."""
        headers, base_url, country, _locale = self._build_request_context()
        domain = _cookidoo_domain_for_country(country)
        start = max(0, (page - 1) * size)
        end = start + size

        if kind in ("saved", "liked", "all"):
            async with httpx.AsyncClient(transport=self.transport, timeout=15.0) as client:
                pages = await self._fetch_paginated_collection(
                    client, "/organize/en/api/custom-list", headers, base_url
                )
                by_id: dict[str, dict[str, Any]] = {}
                for payload in pages:
                    customlists = payload.get("customlists")
                    if not isinstance(customlists, list):
                        continue
                    for cl in customlists:
                        if not isinstance(cl, dict):
                            continue
                        list_title = str(
                            cl.get("title") or cl.get("name") or "Lagret i Cookidoo"
                        ).strip()
                        list_thumb = cl.get("listThumbnail") or cl.get("thumbnail")
                        self._collect_managed_list_items(cl, list_title, list_thumb, by_id)
                all_items = list(by_id.values())
                page_items = all_items[start:end]
                entries = await self._hydrate_recipe_summaries(
                    client, page_items, headers, base_url, country
                )
                return CollectionPage(entries=entries, has_more=len(all_items) > end)

        if kind in ("created", "custom"):
            async with httpx.AsyncClient(transport=self.transport, timeout=15.0) as client:
                pages = await self._fetch_paginated_collection(
                    client, "/created-recipes/en", headers, base_url
                )
            all_entries: list[CollectionEntry] = []
            seen_ids: set[str] = set()
            for payload in pages:
                items = (
                    payload.get("items")
                    or payload.get("createdRecipes")
                    or payload.get("recipes")
                    or []
                )
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_copy = dict(item)
                    rid = str(
                        item_copy.get("recipeId")
                        or item_copy.get("id")
                        or item_copy.get("recipe_id")
                        or ""
                    ).strip()
                    if not rid or rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    item_copy.setdefault("url", f"https://{domain}/created-recipes/en/{rid}")
                    item_copy.setdefault("badges", ["Opprettet i Cookidoo"])
                    all_entries.append(
                        normalize_cookidoo_recipe(
                            item_copy,
                            provider=self.id,
                            country=country,
                            locale="en",
                        )
                    )
            return CollectionPage(
                entries=all_entries[start:end],
                has_more=len(all_entries) > end,
            )

        if kind == "bookmarks":
            async with httpx.AsyncClient(transport=self.transport, timeout=15.0) as client:
                pages = await self._fetch_paginated_collection(
                    client, "/organize/en/api/managed-list", headers, base_url
                )
                managed_lists: list[dict[str, Any]] = []
                for payload in pages:
                    ml_items = payload.get("managedlists")
                    if isinstance(ml_items, list):
                        managed_lists.extend(m for m in ml_items if isinstance(m, dict))

                sem = asyncio.Semaphore(5)

                async def _fetch_managed_detail(
                    ml: dict[str, Any],
                ) -> tuple[dict[str, Any], dict[str, Any] | None]:
                    col_id = str(ml.get("collectionId") or ml.get("id") or "").strip()
                    if not col_id:
                        return ml, None
                    async with sem:
                        detail = await self._get_json(
                            client,
                            f"/organize/en/api/managed-list/{col_id}",
                            headers,
                            base_url,
                            allow_not_found=True,
                        )
                    return ml, detail if isinstance(detail, dict) else None

                detail_results = await asyncio.gather(
                    *(_fetch_managed_detail(ml) for ml in managed_lists)
                )
                by_id: dict[str, dict[str, Any]] = {}
                for ml, detail in detail_results:
                    if not detail:
                        continue
                    col_title = str(
                        detail.get("title") or ml.get("title") or "Bokmerker i Cookidoo"
                    ).strip()
                    col_thumb = detail.get("landscapeImage") or ml.get("landscapeImage")
                    self._collect_managed_list_items(detail, col_title, col_thumb, by_id)

                all_items = list(by_id.values())
                page_items = all_items[start:end]
                entries = await self._hydrate_recipe_summaries(
                    client, page_items, headers, base_url, country
                )
                return CollectionPage(entries=entries, has_more=len(all_items) > end)

        raise CookidooError(
            status_code=400,
            detail=f"Ugyldig samlingstype for Cookidoo: {kind}. Velg 'saved', 'created' eller 'bookmarks'.",
        )


class CookidooRecipeService:
    """Service for managing Cookidoo recipe preview, inspection, and atomic import."""

    def __init__(
        self,
        kitchen: KitchenService,
        history: HistoryRepository,
        store: ConnectorStore,
        household_id: UUID,
        *,
        collection_provider: CookidooCollectionProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ):
        self.kitchen = kitchen
        self.history = history
        self.store = store
        self.household_id = household_id
        self.provider = collection_provider or CookidooCollectionProvider(
            store=store,
            household_id=household_id,
            transport=transport,
            base_url=base_url,
        )

    def check_duplicate(self, entry: CollectionEntry, connection=None) -> tuple[UUID | None, bool]:
        """Check whether the entry was already imported or linked."""

        def lookup(conn):
            linked = conn.execute(
                select(source_links.c.recipe_id).where(
                    source_links.c.provider == "cookidoo",
                    source_links.c.external_recipe_id == entry.id,
                )
            ).scalar_one_or_none()
            if linked is not None:
                return linked, True

            if entry.source_url is not None:
                existing_recipe = conn.execute(
                    select(recipes.c.id)
                    .where(recipes.c.source_url == entry.source_url)
                    .order_by(recipes.c.position)
                    .limit(1)
                ).scalar_one_or_none()
                if existing_recipe is not None:
                    return existing_recipe, True

            return None, False

        if connection is not None:
            return lookup(connection)
        with self.kitchen.store.engine.connect() as conn:
            return lookup(conn)

    def import_recipe_atomic(
        self,
        actor_id: str,
        entry: CollectionEntry,
        reviewed: bool,
    ) -> tuple[UUID, bool]:
        """Atomically import a single Cookidoo recipe without overwriting existing recipes."""
        if not reviewed:
            raise ValueError("Gjennomgå og bekreft oppskriften før import.")

        self.provider.validate_source(entry)

        with self.kitchen.store.engine.begin() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"recipe-source:cookidoo:{entry.id}"},
                )

            existing_id, is_duplicate = self.check_duplicate(entry, connection)

            if is_duplicate and existing_id is not None:
                # Retain existing saved recipe without overwriting
                link_stmt = _dialect_insert(source_links, connection.dialect.name)
                connection.execute(
                    link_stmt.values(
                        provider="cookidoo",
                        external_recipe_id=entry.id,
                        recipe_id=existing_id,
                        source_url=entry.source_url or "",
                        metadata={"badges": entry.badges, **entry.metadata},
                        created_at=datetime.now(UTC),
                    ).on_conflict_do_nothing(
                        index_elements=[source_links.c.provider, source_links.c.external_recipe_id]
                    )
                )
                return existing_id, True

            # Create new recipe draft preserving source="cookidoo" and source_url
            recipe_id = uuid4()
            servings = entry.servings or entry.metadata.get("servings", 4)
            ingredients = entry.ingredients or entry.metadata.get("ingredients", [])
            instructions = entry.instructions or entry.metadata.get("instructions", [])

            # Ensure minimal non-empty instructions/ingredients for Kitchen model
            safe_ingredients = ingredients if ingredients else ["1 porsjon ingredienser"]
            safe_instructions = (
                instructions if instructions else ["Følg veiledning i Cookidoo / TM6"]
            )
            raw_seconds = (
                entry.metadata.get("total_time")
                if entry.metadata.get("total_time") is not None
                else entry.metadata.get("prep_time")
            )
            raw_minutes = (
                entry.prep_time_minutes
                if entry.prep_time_minutes is not None
                else _seconds_to_minutes(raw_seconds)
            )
            inferred_type, inferred_minutes, inferred_prep_category = infer_recipe_metadata(
                title=entry.title,
                ingredients=safe_ingredients,
                categories=entry.badges,
                prep_time_minutes=raw_minutes,
                servings=servings,
                recipe_type=entry.recipe_type,
                prep_category=entry.prep_category,
            )

            new_recipe = Recipe(
                id=recipe_id,
                title=entry.title,
                servings=servings,
                ingredients=[
                    source_ingredient(raw, idx) for idx, raw in enumerate(safe_ingredients)
                ],
                instructions=safe_instructions,
                source=RecipeSource.COOKIDOO,
                source_url=entry.source_url,
                image_url=entry.image_url,
                source_ingredients=[
                    SourceIngredient(index=idx, text=raw)
                    for idx, raw in enumerate(safe_ingredients)
                ],
                source_language="nob",
                recipe_type=inferred_type,
                prep_time_minutes=inferred_minutes,
                prep_category=inferred_prep_category,
            )

            connection.execute(insert(recipes).values(**entity_values(new_recipe)))

            # Record history domain event
            self.history.record(
                HistoryEntryCreate(
                    kind="domain_event",
                    message="Importerte Cookidoo-oppskrift",
                    recipe_id=recipe_id,
                    structured={
                        "provider": "cookidoo",
                        "external_recipe_id": entry.id,
                        "source_url": entry.source_url,
                        "badges": entry.badges,
                    },
                ),
                actor_id=actor_id,
                connection=connection,
            )

            # Link in source_links
            link_stmt = _dialect_insert(source_links, connection.dialect.name)
            connection.execute(
                link_stmt.values(
                    provider="cookidoo",
                    external_recipe_id=entry.id,
                    recipe_id=recipe_id,
                    source_url=entry.source_url or "",
                    metadata={"badges": entry.badges, **entry.metadata},
                    created_at=datetime.now(UTC),
                ).on_conflict_do_nothing(
                    index_elements=[source_links.c.provider, source_links.c.external_recipe_id]
                )
            )

            return recipe_id, False
