"""Cookidoo collection adapter and recipe import service.

Fetches user's Cookidoo collections / saved recipes via httpx (with injectable
transport for offline tests), normalizes them into CollectionEntry / CollectionJob
shapes with duplicate detection, and imports selected recipes into Kitchen
preserving source="cookidoo" and source_url without overwriting existing saved recipes.
"""

from __future__ import annotations

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

DEFAULT_COOKIDOO_NO_URL = "https://cookidoo.no"
_COOKIDOO_HOST_RE = re.compile(
    r"(?:[a-z0-9-]+\.)*cookidoo\.(?:[a-z]{2}|com|co\.uk|com\.au|vorwerk-digital\.com)"
    r"|(?:[a-z0-9-]+\.)*cookidoo\.thermomix\.com"
)


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


def normalize_cookidoo_recipe(
    raw: dict[str, Any],
    provider: str = "cookidoo",
    country: str = "no",
    locale: str | None = None,
) -> CollectionEntry:
    """Normalize raw Cookidoo API recipe or created recipe into CollectionEntry."""
    country_clean = (country or "no").lower()
    effective_locale = locale or (
        "nb-NO" if country_clean == "no" else f"{country_clean}-{country_clean.upper()}"
    )

    recipe_id = str(
        raw.get("id") or raw.get("recipeId") or raw.get("external_id") or raw.get("recipe_id") or ""
    ).strip()

    title = str(
        raw.get("title") or raw.get("recipeName") or raw.get("name") or "Uten tittel"
    ).strip()

    source_url = raw.get("url") or raw.get("source_url") or raw.get("sourceUrl")
    if not source_url and recipe_id:
        source_url = (
            f"https://cookidoo.{country_clean}/recipes/recipe/{effective_locale}/{recipe_id}"
        )

    image_url = raw.get("imageUrl") or raw.get("image_url") or raw.get("image")
    if isinstance(image_url, str):
        image_url = image_url.strip() or None
        if image_url and image_url.startswith("/"):
            image_url = f"https://cookidoo.{country_clean}{image_url}"
        if image_url:
            parsed_img = urlparse(image_url)
            if parsed_img.scheme != "https" or parsed_img.username or parsed_img.password:
                image_url = None
    else:
        image_url = None

    # Extract servings
    servings = 4
    if "servings" in raw and raw["servings"]:
        try:
            servings = int(raw["servings"])
        except (ValueError, TypeError):
            servings = 4
    elif "yield" in raw and isinstance(raw["yield"], dict):
        try:
            servings = int(raw["yield"].get("value", 4))
        except (ValueError, TypeError):
            servings = 4

    # Extract ingredients (supports list of str or list of dicts)
    raw_ingredients = (
        raw.get("ingredients") or raw.get("ingredient_list") or raw.get("recipeIngredients") or []
    )
    ingredients: list[str] = []
    if isinstance(raw_ingredients, list):
        for item in raw_ingredients:
            if isinstance(item, str) and item.strip():
                ingredients.append(item.strip())
            elif isinstance(item, dict):
                text_val = item.get("text") or item.get("name") or item.get("ingredient")
                if text_val and str(text_val).strip():
                    ingredients.append(str(text_val).strip())

    # Extract instructions (supports list of str or list of dicts)
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
            if isinstance(item, str) and item.strip():
                instructions.append(item.strip())
            elif isinstance(item, dict):
                text_val = item.get("text") or item.get("description") or item.get("instruction")
                if text_val and str(text_val).strip():
                    instructions.append(str(text_val).strip())

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
    if raw.get("tools"):
        metadata["tools"] = raw["tools"]

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
            last_error: Exception | None = None
            for path in endpoint_paths:
                full_url = f"{base_url.rstrip('/')}{path}"
                try:
                    response = await client.get(full_url, params=params, headers=headers)
                    if response.status_code == 401 or response.status_code == 403:
                        raise CookidooError(
                            status_code=401,
                            detail="Cookidoo avviste forespørselen. Vennligst logg inn på nytt under Innstillinger.",
                        )
                    if response.status_code == 404:
                        continue
                    if response.status_code >= 400:
                        raise CookidooError(
                            status_code=response.status_code,
                            detail=f"Cookidoo API returnerte en feil ({response.status_code}). Vennligst prøv igjen senere.",
                        )
                    return response.json()
                except httpx.HTTPError as exc:
                    last_error = exc
                    continue

            if last_error is not None:
                raise CookidooError(
                    status_code=502,
                    detail=f"Nettverksfeil mot Cookidoo: {last_error}",
                ) from last_error

            return {"recipes": [], "has_more": False}

    def _build_request_context(self) -> tuple[dict[str, str], str, str, str]:
        creds, _ = self._get_credentials_or_fail()
        country = str(creds.get("country") or "no").lower()
        token_type = creds.get("token_type", "Bearer")

        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeKitchen/1.0",
        }
        if creds.get("access_token"):
            headers["Authorization"] = f"{token_type} {creds['access_token']}"
        if creds.get("session_cookie"):
            headers["Cookie"] = creds["session_cookie"]

        base_url = self.base_url or f"https://cookidoo.{country}"
        locale = "nb-NO" if country == "no" else f"{country}-{country.upper()}"
        return headers, base_url, country, locale

    async def extract_entry(self, entry: CollectionEntry) -> ImportedDraft:
        """Fetch detailed recipe ingredients and instructions from Cookidoo when missing from list."""
        self.validate_source(entry)
        assert entry.source_url is not None
        headers, base_url, country, locale = self._build_request_context()
        parsed = urlparse(entry.source_url)
        candidate_paths = [
            f"/api/v2/recipes/{entry.id}",
            parsed.path,
        ]
        data = await self._fetch_from_api(candidate_paths, {}, headers, base_url)
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
        )

    async def page(self, actor_id: str, kind: str, page: int, size: int) -> CollectionPage:
        """Fetch a page of recipes for the given collection kind."""
        headers, base_url, country, locale = self._build_request_context()

        if kind in ("saved", "liked", "all"):
            paths = [
                "/api/v2/user/recipes",
                "/api/recipe-collections",
                "/api/v2/user/collections",
                "/api/collections",
                "/recipes/saved",
                "/recipes",
            ]
        elif kind in ("created", "custom"):
            paths = [
                f"/created-recipes/{locale}",
                f"/created-recipes/{country}",
                "/created-recipes",
                "/api/v2/user/custom-recipes",
            ]
        elif kind == "bookmarks":
            paths = [
                "/api/v2/user/bookmarks",
                "/api/v2/user/recipes",
                "/recipes/saved",
            ]
        else:
            raise CookidooError(
                status_code=400,
                detail=f"Ugyldig samlingstype for Cookidoo: {kind}. Velg 'saved', 'created' eller 'bookmarks'.",
            )

        params = {
            "page": page,
            "size": size,
            "limit": size,
            "offset": (page - 1) * size,
        }

        data = await self._fetch_from_api(paths, params, headers, base_url)

        # Extract items from various payload layouts
        raw_recipes: list[dict[str, Any]] = []
        has_more = False

        if isinstance(data, list):
            raw_recipes = [item for item in data if isinstance(item, dict)]
            has_more = len(raw_recipes) >= size
        elif isinstance(data, dict):
            has_more = bool(data.get("hasMore") or data.get("has_more") or False)
            if "recipes" in data and isinstance(data["recipes"], list):
                raw_recipes = data["recipes"]
            elif "createdRecipes" in data and isinstance(data["createdRecipes"], list):
                raw_recipes = data["createdRecipes"]
            elif "items" in data and isinstance(data["items"], list):
                raw_recipes = data["items"]
            elif "data" in data and isinstance(data["data"], list):
                raw_recipes = data["data"]
            elif "collections" in data and isinstance(data["collections"], list):
                for col in data["collections"]:
                    if isinstance(col, dict):
                        col_name = col.get("name") or col.get("title") or "Samling"
                        for rec in col.get("recipes", []):
                            if isinstance(rec, dict):
                                rec_copy = dict(rec)
                                rec_copy.setdefault("badges", []).append(col_name)
                                raw_recipes.append(rec_copy)
            if not has_more and "total" in data:
                try:
                    has_more = int(data["total"]) > (page * size)
                except (ValueError, TypeError):
                    pass

        entries: list[CollectionEntry] = []
        for raw in raw_recipes:
            entry = normalize_cookidoo_recipe(
                raw,
                provider=self.id,
                country=country,
                locale=locale,
            )
            entries.append(entry)

        return CollectionPage(entries=entries, has_more=has_more)


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
