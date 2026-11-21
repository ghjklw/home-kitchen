"""Versioned, idempotent reference seeds for core units, grocery categories and staple products.

Reference seeds maintain stable, reviewable shared definitions across local, UAT and production
environments. They are decoupled from household activity: running seeds never creates purchases,
inventory, recipes, meals, plans, stock or SKU preferences, and never overwrites household
reviewed corrections or aliases.
"""

from __future__ import annotations

import argparse
import os
from typing import Annotated, Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from home_kitchen.catalogue.identity import normalize_identity_text
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService, IdentityRepository

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Deterministic namespace for shared reference rows.
REFERENCE_SEED_NAMESPACE = UUID("c8d765b2-2ff9-4e56-9a57-1965bb742967")


def reference_unit_id(canonical_name: str) -> UUID:
    """Stable, reviewable identifier for a canonical culinary unit."""
    return uuid5(REFERENCE_SEED_NAMESPACE, f"unit:{normalize_identity_text(canonical_name)}")


def reference_product_id(canonical_name: str, variant: str = "") -> UUID:
    """Stable, reviewable identifier for a staple canonical product."""
    norm_name = normalize_identity_text(canonical_name)
    norm_var = normalize_identity_text(variant)
    return uuid5(REFERENCE_SEED_NAMESPACE, f"product:{norm_name}:{norm_var}")


def reference_category_id(key: str) -> UUID:
    """Stable, reviewable identifier for a reference grocery category."""
    return uuid5(REFERENCE_SEED_NAMESPACE, f"category:{normalize_identity_text(key)}")


class ReferenceUnit(BaseModel):
    canonical_name: NonBlank
    physical_dimension: Literal["mass", "volume", "count", "culinary_measure"]
    base_unit: str | None = None
    bokmal_aliases: list[NonBlank] = Field(default_factory=list)
    english_aliases: list[NonBlank] = Field(default_factory=list)

    @property
    def id(self) -> UUID:
        return reference_unit_id(self.canonical_name)


class ReferenceCategory(BaseModel):
    key: NonBlank
    canonical_name: NonBlank
    display_name_nb: NonBlank
    display_name_en: NonBlank
    walking_order: int
    aliases: list[NonBlank] = Field(default_factory=list)

    @property
    def id(self) -> UUID:
        return reference_category_id(self.key)

    def display_name(self, locale: str = "nb") -> str:
        norm = normalize_identity_text(locale)
        if norm.startswith("en"):
            return self.display_name_en
        return self.display_name_nb


class ReferenceProduct(BaseModel):
    canonical_name: NonBlank
    variant: str = ""
    category: NonBlank
    bokmal_aliases: list[NonBlank] = Field(default_factory=list)
    english_aliases: list[NonBlank] = Field(default_factory=list)
    source_id: str | None = None
    source: str | None = None

    @property
    def id(self) -> UUID:
        return reference_product_id(self.canonical_name, self.variant)


class SeedVersion(BaseModel):
    version: int
    description: str
    units: tuple[ReferenceUnit, ...]
    categories: tuple[ReferenceCategory, ...]
    products: tuple[ReferenceProduct, ...]


class SeedResult(BaseModel):
    versions_applied: list[int] = Field(default_factory=list)
    units_seeded: int = 0
    products_seeded: int = 0
    aliases_seeded: int = 0
    categories_seeded: int = 0


# --- Version 1: Core Units, Categories and Staple Products ---

V1_UNITS: tuple[ReferenceUnit, ...] = (
    # Exact mass units
    ReferenceUnit(
        canonical_name="g",
        physical_dimension="mass",
        base_unit="g",
        bokmal_aliases=["g", "gram"],
        english_aliases=["g", "gram", "grams"],
    ),
    ReferenceUnit(
        canonical_name="kg",
        physical_dimension="mass",
        base_unit="g",
        bokmal_aliases=["kg", "kilogram"],
        english_aliases=["kg", "kilogram", "kilograms"],
    ),
    # Exact volume units
    ReferenceUnit(
        canonical_name="ml",
        physical_dimension="volume",
        base_unit="ml",
        bokmal_aliases=["ml", "milliliter"],
        english_aliases=["ml", "milliliter", "milliliters", "millilitre", "millilitres"],
    ),
    ReferenceUnit(
        canonical_name="dl",
        physical_dimension="volume",
        base_unit="ml",
        bokmal_aliases=["dl", "desiliter"],
        english_aliases=["dl", "deciliter", "deciliters", "decilitre", "decilitres", "desiliter"],
    ),
    ReferenceUnit(
        canonical_name="l",
        physical_dimension="volume",
        base_unit="ml",
        bokmal_aliases=["l", "liter"],
        english_aliases=["l", "liter", "liters", "litre", "litres"],
    ),
    # Discrete item / count unit
    ReferenceUnit(
        canonical_name="stk",
        physical_dimension="count",
        base_unit=None,
        bokmal_aliases=["stk", "stk.", "stykke", "stykker", "stykk"],
        english_aliases=["item", "items", "pc", "pcs", "piece", "pieces"],
    ),
    # Culinary informal measures (do not redefine physical unit conversions)
    ReferenceUnit(
        canonical_name="ts",
        physical_dimension="culinary_measure",
        base_unit=None,
        bokmal_aliases=["ts", "teskje", "teskjeer"],
        english_aliases=["tsp", "teaspoon", "teaspoons"],
    ),
    ReferenceUnit(
        canonical_name="ss",
        physical_dimension="culinary_measure",
        base_unit=None,
        bokmal_aliases=["ss", "spiseskje", "spiseskjeer"],
        english_aliases=["tbsp", "tablespoon", "tablespoons"],
    ),
    ReferenceUnit(
        canonical_name="klype",
        physical_dimension="culinary_measure",
        base_unit=None,
        bokmal_aliases=["klype", "klyper"],
        english_aliases=["pinch", "pinches"],
    ),
    ReferenceUnit(
        canonical_name="pakke",
        physical_dimension="culinary_measure",
        base_unit=None,
        bokmal_aliases=["pakke", "pakker", "pk"],
        english_aliases=["pack", "packs", "package", "packages", "packet", "packets"],
    ),
)

V1_CATEGORIES: tuple[ReferenceCategory, ...] = (
    ReferenceCategory(
        key="Produce",
        canonical_name="Produce",
        display_name_nb="Frukt og grønt",
        display_name_en="Produce",
        walking_order=1,
        aliases=["produce", "frukt og grønt", "fruits and vegetables", "grønnsaker", "frukt"],
    ),
    ReferenceCategory(
        key="Bakery",
        canonical_name="Bakery",
        display_name_nb="Bakeri",
        display_name_en="Bakery",
        walking_order=2,
        aliases=["bakery", "bakeri", "brød og bakervarer", "bread"],
    ),
    ReferenceCategory(
        key="Meat",
        canonical_name="Meat",
        display_name_nb="Kjøtt",
        display_name_en="Meat",
        walking_order=3,
        aliases=["meat", "kjøtt"],
    ),
    ReferenceCategory(
        key="Seafood",
        canonical_name="Seafood",
        display_name_nb="Sjømat",
        display_name_en="Seafood",
        walking_order=4,
        aliases=["seafood", "sjømat"],
    ),
    ReferenceCategory(
        key="Fish",
        canonical_name="Fish",
        display_name_nb="Fisk",
        display_name_en="Fish",
        walking_order=5,
        aliases=["fish", "fisk"],
    ),
    ReferenceCategory(
        key="Dairy",
        canonical_name="Dairy",
        display_name_nb="Meieri",
        display_name_en="Dairy",
        walking_order=6,
        aliases=["dairy", "meieri", "melk og meieri", "dairy & eggs", "meieri og egg"],
    ),
    ReferenceCategory(
        key="Chilled",
        canonical_name="Chilled",
        display_name_nb="Kjølevarer",
        display_name_en="Chilled",
        walking_order=7,
        aliases=["chilled", "kjølevarer", "pålegg"],
    ),
    ReferenceCategory(
        key="Pantry",
        canonical_name="Pantry",
        display_name_nb="Tørrvarer",
        display_name_en="Pantry",
        walking_order=8,
        aliases=["pantry", "tørrvarer", "kolonial", "hermetikk", "baking", "bakevarer"],
    ),
    ReferenceCategory(
        key="Spices",
        canonical_name="Spices",
        display_name_nb="Krydder",
        display_name_en="Spices",
        walking_order=9,
        aliases=["spices", "krydder", "krydder og urter", "herbs and spices"],
    ),
    ReferenceCategory(
        key="Drinks",
        canonical_name="Drinks",
        display_name_nb="Drikke",
        display_name_en="Drinks",
        walking_order=10,
        aliases=["drinks", "drikke", "beverages"],
    ),
    ReferenceCategory(
        key="Household",
        canonical_name="Household",
        display_name_nb="Husholdning",
        display_name_en="Household",
        walking_order=11,
        aliases=["household", "husholdning", "rengjøring"],
    ),
    ReferenceCategory(
        key="Frozen",
        canonical_name="Frozen",
        display_name_nb="Frysevarer",
        display_name_en="Frozen",
        walking_order=12,
        aliases=["frozen", "frysevarer", "frossenmat", "frosne varer"],
    ),
    ReferenceCategory(
        key="Other",
        canonical_name="Other",
        display_name_nb="Annet",
        display_name_en="Other",
        walking_order=13,
        aliases=["other", "annet", "diverse"],
    ),
)

V1_PRODUCTS: tuple[ReferenceProduct, ...] = (
    ReferenceProduct(
        canonical_name="hvetemel",
        variant="",
        category="Pantry",
        bokmal_aliases=["hvetemel", "siktet hvetemel", "hvete mel"],
        english_aliases=["flour", "wheat flour", "all-purpose flour"],
        source_id="05.010",
        source="matvaretabellen",
    ),
    ReferenceProduct(
        canonical_name="egg",
        variant="",
        category="Dairy",
        bokmal_aliases=["egg"],
        english_aliases=["egg", "eggs"],
        source_id="02.001",
        source="matvaretabellen",
    ),
    ReferenceProduct(
        canonical_name="smør",
        variant="",
        category="Dairy",
        bokmal_aliases=["smør", "meierismør"],
        english_aliases=["butter"],
        source="matvaretabellen",
    ),
    ReferenceProduct(
        canonical_name="helmelk",
        variant="",
        category="Dairy",
        bokmal_aliases=["helmelk", "hel melk"],
        english_aliases=["whole milk"],
        source_id="01.272",
        source="matvaretabellen",
    ),
    ReferenceProduct(
        canonical_name="sukker",
        variant="",
        category="Pantry",
        bokmal_aliases=["sukker", "hvitt sukker"],
        english_aliases=["sugar", "white sugar"],
    ),
    ReferenceProduct(
        canonical_name="kaffe",
        variant="",
        category="Pantry",
        bokmal_aliases=["kaffe", "filtermalt kaffe", "filterkaffe"],
        english_aliases=["coffee", "ground coffee"],
    ),
    ReferenceProduct(
        canonical_name="tomat",
        variant="",
        category="Produce",
        bokmal_aliases=["tomat", "tomater"],
        english_aliases=["tomato", "tomatoes"],
    ),
)

VERSION_1_SEED = SeedVersion(
    version=1,
    description="Core Norwegian/English culinary units, grocery categories and staple products",
    units=V1_UNITS,
    categories=V1_CATEGORIES,
    products=V1_PRODUCTS,
)

REFERENCE_SEED_VERSION = 1
LATEST_SEED_VERSION = 1
_SEED_REGISTRY: dict[int, SeedVersion] = {1: VERSION_1_SEED}


def register_seed_version(seed_version: SeedVersion) -> None:
    """Register a seed version for incremental migration/seed testing."""
    _SEED_REGISTRY[seed_version.version] = seed_version


def reset_seed_registry() -> None:
    """Reset the seed registry to official versions only (e.g. after testing)."""
    global _SEED_REGISTRY
    _SEED_REGISTRY = {1: VERSION_1_SEED}


def get_seed_version(version: int) -> SeedVersion | None:
    return _SEED_REGISTRY.get(version)


def get_reference_units(version: int | None = None) -> tuple[ReferenceUnit, ...]:
    v = version or LATEST_SEED_VERSION
    manifest = _SEED_REGISTRY.get(v)
    return manifest.units if manifest else ()


def get_reference_categories(version: int | None = None) -> tuple[ReferenceCategory, ...]:
    v = version or LATEST_SEED_VERSION
    manifest = _SEED_REGISTRY.get(v)
    return manifest.categories if manifest else ()


def get_reference_category(
    key_or_name: str, version: int | None = None
) -> ReferenceCategory | None:
    norm = normalize_identity_text(key_or_name)
    for cat in get_reference_categories(version):
        if (
            normalize_identity_text(cat.key) == norm
            or normalize_identity_text(cat.canonical_name) == norm
            or normalize_identity_text(cat.display_name_nb) == norm
            or normalize_identity_text(cat.display_name_en) == norm
            or any(normalize_identity_text(alias) == norm for alias in cat.aliases)
        ):
            return cat
    return None


def get_default_walking_order(locale: str = "en", version: int | None = None) -> list[str]:
    categories = sorted(get_reference_categories(version), key=lambda c: c.walking_order)
    norm_locale = normalize_identity_text(locale)
    if norm_locale.startswith("nb"):
        return [c.display_name_nb for c in categories]
    return [c.key for c in categories]


def get_reference_products(version: int | None = None) -> tuple[ReferenceProduct, ...]:
    v = version or LATEST_SEED_VERSION
    manifest = _SEED_REGISTRY.get(v)
    return manifest.products if manifest else ()


# --- Persistence and Schema Operations ---

REFERENCE_CATEGORIES_DDL = """
CREATE TABLE IF NOT EXISTS kitchen_reference_categories (
    id UUID PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    canonical_name TEXT NOT NULL,
    display_name_nb TEXT NOT NULL,
    display_name_en TEXT NOT NULL,
    walking_order INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""

REFERENCE_SEED_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS kitchen_reference_seed_versions (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);
"""


def _ensure_reference_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(REFERENCE_CATEGORIES_DDL))
        connection.execute(text(REFERENCE_SEED_VERSIONS_DDL))


def get_applied_seed_versions(engine: Engine) -> set[int]:
    _ensure_reference_tables(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT version FROM kitchen_reference_seed_versions ORDER BY version")
        ).scalars()
        return set(rows)


def _seed_categories_sql(engine: Engine, categories: tuple[ReferenceCategory, ...]) -> int:
    inserted = 0
    with engine.begin() as connection:
        for cat in categories:
            result = connection.execute(
                text("""
                INSERT INTO kitchen_reference_categories (id, key, canonical_name, display_name_nb, display_name_en, walking_order)
                VALUES (:id, :key, :canonical_name, :display_name_nb, :display_name_en, :walking_order)
                ON CONFLICT (key) DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    display_name_nb = EXCLUDED.display_name_nb,
                    display_name_en = EXCLUDED.display_name_en,
                    walking_order = EXCLUDED.walking_order
                WHERE kitchen_reference_categories.canonical_name IS DISTINCT FROM EXCLUDED.canonical_name
                   OR kitchen_reference_categories.display_name_nb IS DISTINCT FROM EXCLUDED.display_name_nb
                   OR kitchen_reference_categories.display_name_en IS DISTINCT FROM EXCLUDED.display_name_en
                   OR kitchen_reference_categories.walking_order IS DISTINCT FROM EXCLUDED.walking_order
                """),
                {
                    "id": cat.id,
                    "key": cat.key,
                    "canonical_name": cat.canonical_name,
                    "display_name_nb": cat.display_name_nb,
                    "display_name_en": cat.display_name_en,
                    "walking_order": cat.walking_order,
                },
            )
            inserted += result.rowcount
    return inserted


def _is_reviewed(repo: Any, kind: str, locale: str, context: str, alias: str) -> bool:
    checker = getattr(repo, "has_reviewed_alias", None)
    if callable(checker):
        return bool(checker(kind, locale, context, alias))
    reviewed_set = getattr(repo, "reviewed", None)
    if isinstance(reviewed_set, set):
        key = (
            kind,
            normalize_identity_text(locale),
            normalize_identity_text(context),
            normalize_identity_text(alias),
        )
        return key in reviewed_set
    return False


def _add_imported_alias(
    repo: IdentityRepository, kind: str, locale: str, alias: str, identity_id: UUID
) -> None:
    try:
        repo.add_alias(
            kind,
            locale,
            "",
            alias,
            identity_id,
            source_type="imported_alias",  # ty: ignore[unknown-argument]
        )
    except TypeError:
        repo.add_alias(kind, locale, "", alias, identity_id)


def _seed_units_into_repo(
    repo: IdentityRepository, units: tuple[ReferenceUnit, ...]
) -> tuple[int, int]:
    units_seeded = 0
    aliases_seeded = 0
    for u in units:
        try:
            created = repo.unit(u.canonical_name, identity_id=u.id)
        except TypeError:
            created = repo.unit(u.canonical_name)
        units_seeded += 1
        for alias in u.bokmal_aliases:
            if not _is_reviewed(repo, "unit", "nb", "", alias):
                _add_imported_alias(repo, "unit", "nb", alias, created.id)
                aliases_seeded += 1
        for alias in u.english_aliases:
            if not _is_reviewed(repo, "unit", "en", "", alias):
                _add_imported_alias(repo, "unit", "en", alias, created.id)
                aliases_seeded += 1
    return units_seeded, aliases_seeded


def _seed_products_into_repo(
    repo: IdentityRepository, products: tuple[ReferenceProduct, ...]
) -> tuple[int, int]:
    products_seeded = 0
    aliases_seeded = 0
    for p in products:
        try:
            created = repo.product(p.canonical_name, p.variant, identity_id=p.id)
        except TypeError:
            created = repo.product(p.canonical_name, p.variant)
        products_seeded += 1
        for alias in p.bokmal_aliases:
            if not _is_reviewed(repo, "product", "nb", "", alias):
                _add_imported_alias(repo, "product", "nb", alias, created.id)
                aliases_seeded += 1
        for alias in p.english_aliases:
            if not _is_reviewed(repo, "product", "en", "", alias):
                _add_imported_alias(repo, "product", "en", alias, created.id)
                aliases_seeded += 1
    return products_seeded, aliases_seeded


def seed_reference_data(
    target: Engine | IdentityRepository | CanonicalIdentityService,
    target_version: int | None = None,
) -> SeedResult:
    """Apply versioned reference seeds idempotently into an Engine or IdentityRepository.

    - Repeated executions add no duplicate rows.
    - Household corrections (reviewed=true) are preserved and never overwritten.
    - No household activity (recipes, meals, inventory, purchases, preferences) is created.
    """
    max_version = target_version or max(_SEED_REGISTRY.keys(), default=1)
    result = SeedResult()

    if isinstance(target, Engine):
        _ensure_reference_tables(target)
        repo = PostgresIdentityRepository(target)
        applied_versions = get_applied_seed_versions(target)

        # Count existing canonical rows before to accurately report newly seeded items
        for v_num in sorted(_SEED_REGISTRY.keys()):
            if v_num > max_version or v_num in applied_versions:
                continue
            manifest = _SEED_REGISTRY[v_num]
            cat_seeded = _seed_categories_sql(target, manifest.categories)
            u_seeded, u_alias = _seed_units_into_repo(repo, manifest.units)
            p_seeded, p_alias = _seed_products_into_repo(repo, manifest.products)

            with target.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO kitchen_reference_seed_versions (version) VALUES (:v) ON CONFLICT (version) DO NOTHING"
                    ),
                    {"v": v_num},
                )

            result.versions_applied.append(v_num)
            result.categories_seeded += cat_seeded
            result.units_seeded += u_seeded
            result.products_seeded += p_seeded
            result.aliases_seeded += u_alias + p_alias

        return result

    # Repository or CanonicalIdentityService target
    active_repo = target.repository if isinstance(target, CanonicalIdentityService) else target
    for v_num in sorted(_SEED_REGISTRY.keys()):
        if v_num > max_version:
            continue
        manifest = _SEED_REGISTRY[v_num]
        u_seeded, u_alias = _seed_units_into_repo(active_repo, manifest.units)
        p_seeded, p_alias = _seed_products_into_repo(active_repo, manifest.products)
        result.versions_applied.append(v_num)
        result.units_seeded += u_seeded
        result.products_seeded += p_seeded
        result.aliases_seeded += u_alias + p_alias

    return result


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Install versioned reference seeds for Home Kitchen."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("KITCHEN_DATABASE_URL"),
        help="Database URL (defaults to KITCHEN_DATABASE_URL)",
    )
    parser.add_argument(
        "--version",
        type=int,
        default=None,
        help="Target seed version to apply (defaults to latest)",
    )
    parsed = parser.parse_args(args)

    if not parsed.database_url:
        raise SystemExit(
            "Error: Database URL must be specified via --database-url or KITCHEN_DATABASE_URL"
        )

    engine = create_engine(parsed.database_url)
    res = seed_reference_data(engine, target_version=parsed.version)
    print(
        f"Reference seeds applied: versions={res.versions_applied}, "
        f"units={res.units_seeded}, products={res.products_seeded}, "
        f"aliases={res.aliases_seeded}, categories={res.categories_seeded}"
    )


if __name__ == "__main__":
    main()
