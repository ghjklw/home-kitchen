"""Tests for versioned, idempotent unit, category, and product reference seeds."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from home_kitchen.catalogue.identity import CanonicalProduct, CanonicalUnit
from home_kitchen.catalogue.identity_repository import PostgresIdentityRepository
from home_kitchen.catalogue.identity_service import CanonicalIdentityService
from home_kitchen.catalogue.seeds import (
    ReferenceProduct,
    ReferenceUnit,
    SeedVersion,
    get_default_walking_order,
    get_reference_categories,
    get_reference_category,
    get_reference_products,
    get_reference_units,
    reference_product_id,
    reference_unit_id,
    register_seed_version,
    reset_seed_registry,
    seed_reference_data,
)
from home_kitchen.settings import DEFAULT_SHOPPING_CATEGORY_ORDER


@pytest.fixture(autouse=True)
def clean_seed_registry():
    reset_seed_registry()
    yield
    reset_seed_registry()


class MemoryIdentityRepository:
    """In-memory test double for IdentityRepository protocol."""

    def __init__(self):
        self.products: dict[tuple[str, str], CanonicalProduct] = {}
        self.units: dict[str, CanonicalUnit] = {}
        self.aliases: dict[tuple[str, str, str, str], set[UUID]] = {}
        self.reviewed: set[tuple[str, str, str, str]] = set()

    def product(self, name: str, variant: str, identity_id: UUID | None = None) -> CanonicalProduct:
        target_id = identity_id or uuid4()
        return self.products.setdefault(
            (name, variant),
            CanonicalProduct(id=target_id, canonical_name=name, variant=variant),
        )

    def unit(self, name: str, identity_id: UUID | None = None) -> CanonicalUnit:
        target_id = identity_id or uuid4()
        return self.units.setdefault(name, CanonicalUnit(id=target_id, canonical_name=name))

    def add_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        key = kind, locale, context, " ".join(alias.split()).casefold()
        if key in self.reviewed:
            return
        self.aliases.setdefault(key, set()).add(identity_id)

    def correct_alias(
        self, kind: str, locale: str, context: str, alias: str, identity_id: UUID
    ) -> None:
        key = kind, locale, context, " ".join(alias.split()).casefold()
        self.aliases[key] = {identity_id}
        self.reviewed.add(key)

    def has_reviewed_alias(self, kind: str, locale: str, context: str, alias: str) -> bool:
        key = kind, locale, context, " ".join(alias.split()).casefold()
        return key in self.reviewed

    def resolve(
        self, kind: str, locale: str, context: str, alias: str
    ) -> list[CanonicalProduct | CanonicalUnit]:
        normalized = " ".join(alias.split()).casefold()
        ids = self.aliases.get((kind, locale, context, normalized), set()) | self.aliases.get(
            (kind, locale, "", normalized), set()
        )
        identities = (
            list(self.products.values()) if kind == "product" else list(self.units.values())
        )
        return [identity for identity in identities if identity.id in ids]


def test_reference_units_definitions_and_physical_semantics():
    units = get_reference_units()
    unit_map = {u.canonical_name: u for u in units}

    # Verify all required core units are present
    required_units = {"g", "kg", "ml", "dl", "l", "stk", "ts", "ss", "klype", "pakke"}
    assert required_units.issubset(unit_map.keys())

    # Mass units preserve base semantics
    assert unit_map["g"].physical_dimension == "mass"
    assert unit_map["g"].base_unit == "g"
    assert "gram" in unit_map["g"].bokmal_aliases
    assert "grams" in unit_map["g"].english_aliases

    assert unit_map["kg"].physical_dimension == "mass"
    assert unit_map["kg"].base_unit == "g"
    assert "kilogram" in unit_map["kg"].bokmal_aliases

    # Volume units preserve base semantics
    for vol in ("ml", "dl", "l"):
        assert unit_map[vol].physical_dimension == "volume"
        assert unit_map[vol].base_unit == "ml"

    # Discrete / informal culinary units do not fabricate physical conversion
    assert unit_map["stk"].physical_dimension == "count"
    assert unit_map["stk"].base_unit is None
    assert "stk." in unit_map["stk"].bokmal_aliases
    assert "item" in unit_map["stk"].english_aliases

    for measure in ("ts", "ss", "klype", "pakke"):
        assert unit_map[measure].physical_dimension == "culinary_measure"
        assert unit_map[measure].base_unit is None

    # Deterministic IDs are unique and stable
    ids = [u.id for u in units]
    assert len(ids) == len(set(ids))
    assert unit_map["g"].id == reference_unit_id("g")


def test_reference_categories_and_walking_order():
    categories = get_reference_categories()
    cat_keys = [c.key for c in categories]

    # Verify alignment with DEFAULT_SHOPPING_CATEGORY_ORDER
    assert tuple(cat_keys) == DEFAULT_SHOPPING_CATEGORY_ORDER

    # Verify walking order matches default order exactly
    walking_order = get_default_walking_order("en")
    assert walking_order == list(DEFAULT_SHOPPING_CATEGORY_ORDER)

    # Bokmål labels match Norwegian store expectations
    produce = get_reference_category("Produce")
    assert produce is not None
    assert produce.display_name("nb") == "Frukt og grønt"
    assert produce.display_name("en") == "Produce"
    assert produce.walking_order == 1

    dairy = get_reference_category("Dairy")
    assert dairy is not None
    assert dairy.display_name("nb") == "Meieri"

    # Lookup by Norwegian name or alias
    assert get_reference_category("Frukt og grønt") == produce
    assert get_reference_category("Meieri og egg") == dairy
    assert get_reference_category("Bakevarer") == get_reference_category("Pantry")

    # Norwegian walking order
    nb_order = get_default_walking_order("nb")
    assert nb_order[0] == "Frukt og grønt"
    assert nb_order[1] == "Bakeri"
    assert nb_order[-1] == "Annet"


def test_reference_products_definitions_and_staples():
    products = get_reference_products()
    product_map = {p.canonical_name: p for p in products}

    # Verify all staple canonical products
    required_staples = {"hvetemel", "egg", "smør", "helmelk", "sukker", "kaffe", "tomat"}
    assert required_staples.issubset(product_map.keys())

    # Check aliases across locales
    assert "flour" in product_map["hvetemel"].english_aliases
    assert "siktet hvetemel" in product_map["hvetemel"].bokmal_aliases
    assert product_map["hvetemel"].category == "Pantry"
    assert product_map["hvetemel"].source_id == "05.010"

    assert "egg" in product_map["egg"].bokmal_aliases
    assert "eggs" in product_map["egg"].english_aliases
    assert product_map["egg"].category == "Dairy"
    assert product_map["egg"].source_id == "02.001"

    assert "butter" in product_map["smør"].english_aliases
    assert "meierismør" in product_map["smør"].bokmal_aliases

    assert "whole milk" in product_map["helmelk"].english_aliases
    assert "hel melk" in product_map["helmelk"].bokmal_aliases

    assert "sugar" in product_map["sukker"].english_aliases
    assert "coffee" in product_map["kaffe"].english_aliases
    assert "tomatoes" in product_map["tomat"].english_aliases
    assert "tomater" in product_map["tomat"].bokmal_aliases

    # Deterministic IDs
    assert product_map["tomat"].id == reference_product_id("tomat", "")


def test_in_memory_seeding_and_resolution():
    repo = MemoryIdentityRepository()
    service = CanonicalIdentityService(repo)

    result = seed_reference_data(service)
    assert 1 in result.versions_applied
    assert result.units_seeded >= 10
    assert result.products_seeded >= 7

    # Unit alias resolution in Bokmål and English
    res_gram = service.resolve("unit", "nb", "", "gram")
    assert res_gram.status == "resolved"
    assert res_gram.candidates[0].canonical_name == "g"

    res_tsp = service.resolve("unit", "en", "recipe", "tsp")
    assert res_tsp.status == "resolved"
    assert res_tsp.candidates[0].canonical_name == "ts"

    res_pack = service.resolve("unit", "en", "", "package")
    assert res_pack.status == "resolved"
    assert res_pack.candidates[0].canonical_name == "pakke"

    # Product alias resolution
    res_tomat = service.resolve("product", "nb", "recipe", "tomater")
    assert res_tomat.status == "resolved"
    assert res_tomat.candidates[0].canonical_name == "tomat"

    res_flour = service.resolve("product", "en", "", "wheat flour")
    assert res_flour.status == "resolved"
    assert res_flour.candidates[0].canonical_name == "hvetemel"


def test_in_memory_idempotency_produces_no_duplicates():
    repo = MemoryIdentityRepository()
    service = CanonicalIdentityService(repo)

    seed_reference_data(service)
    initial_units_count = len(repo.units)
    initial_products_count = len(repo.products)
    initial_aliases_count = sum(len(ids) for ids in repo.aliases.values())

    # Run seeds a second and third time
    seed_reference_data(service)
    seed_reference_data(service)

    assert len(repo.units) == initial_units_count
    assert len(repo.products) == initial_products_count
    assert sum(len(ids) for ids in repo.aliases.values()) == initial_aliases_count

    # Resolution remains unambiguous
    assert service.resolve("product", "nb", "", "egg").status == "resolved"
    assert service.resolve("unit", "nb", "", "ss").status == "resolved"


def test_household_correction_is_never_overwritten_in_memory():
    repo = MemoryIdentityRepository()
    service = CanonicalIdentityService(repo)

    # Initial seed
    seed_reference_data(service)
    standard_tomato = service.resolve("product", "nb", "", "tomat").candidates[0]

    # Household corrects "tomat" to point to a custom product
    custom_tomato = service.create_product("heirloom tomato", "garden")
    service.correct_alias("product", "nb", "", "tomat", custom_tomato.id)

    # Verify household correction is active
    after_correction = service.resolve("product", "nb", "", "tomat")
    assert after_correction.status == "resolved"
    assert after_correction.candidates[0].id == custom_tomato.id
    assert after_correction.candidates[0].id != standard_tomato.id

    # Re-running seeds MUST NOT overwrite or create ambiguity
    seed_reference_data(service)

    rechecked = service.resolve("product", "nb", "", "tomat")
    assert rechecked.status == "resolved"
    assert rechecked.candidates[0].id == custom_tomato.id
    assert ("product", "nb", "", "tomat") in repo.reviewed


def test_version_update_incremental_in_memory():
    repo = MemoryIdentityRepository()
    service = CanonicalIdentityService(repo)

    # Apply Version 1
    seed_reference_data(service, target_version=1)
    v1_products_count = len(repo.products)
    v1_units_count = len(repo.units)

    # Register test Version 2 with a new unit and product
    v2_unit = ReferenceUnit(
        canonical_name="kanne",
        physical_dimension="culinary_measure",
        bokmal_aliases=["kanne", "kanner"],
        english_aliases=["pot", "jug"],
    )
    v2_product = ReferenceProduct(
        canonical_name="havregryn",
        variant="",
        category="Pantry",
        bokmal_aliases=["havregryn"],
        english_aliases=["oats", "rolled oats"],
    )
    v2 = SeedVersion(
        version=2,
        description="Version 2 test additions",
        units=(v2_unit,),
        categories=(),
        products=(v2_product,),
    )
    register_seed_version(v2)

    # Apply Version 2
    res = seed_reference_data(service, target_version=2)
    assert 2 in res.versions_applied
    assert len(repo.units) == v1_units_count + 1
    assert len(repo.products) == v1_products_count + 1

    # Check V2 items resolve
    assert service.resolve("unit", "nb", "", "kanner").status == "resolved"
    assert service.resolve("product", "en", "", "rolled oats").status == "resolved"

    # Replay of V2 produces no duplicates
    seed_reference_data(service, target_version=2)
    assert len(repo.units) == v1_units_count + 1
    assert len(repo.products) == v1_products_count + 1


def test_reference_seeds_have_zero_household_side_effects():
    # Verify reference seeds module does NOT import or reference synthetic fixtures (#111)
    import home_kitchen.catalogue.seeds as seeds_module

    assert not hasattr(seeds_module, "synthetic_kitchen")
    assert not hasattr(seeds_module, "recipes")
    assert not hasattr(seeds_module, "inventory")


# --- Database Integration Tests (uses disposable migrated PostgreSQL schema) ---


def test_database_seeding_idempotency_and_household_isolation(test_database_url):
    engine = create_engine(test_database_url)

    # First load
    first_res = seed_reference_data(engine)
    assert 1 in first_res.versions_applied
    assert first_res.units_seeded >= 10
    assert first_res.products_seeded >= 7
    assert first_res.categories_seeded == 13

    with engine.connect() as conn:
        unit_count = conn.execute(text("SELECT count(*) FROM kitchen_canonical_units")).scalar()
        prod_count = conn.execute(text("SELECT count(*) FROM kitchen_canonical_products")).scalar()
        cat_count = conn.execute(text("SELECT count(*) FROM kitchen_reference_categories")).scalar()
        v_count = conn.execute(
            text("SELECT count(*) FROM kitchen_reference_seed_versions")
        ).scalar()

        # Household tables MUST remain completely empty
        assert conn.execute(text("SELECT count(*) FROM recipes")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM meals")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM items")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM regulars")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM kitchen_inventory")).scalar() == 0
        assert (
            conn.execute(text("SELECT count(*) FROM kitchen_catalogue_sku_preferences")).scalar()
            == 0
        )
        assert (
            conn.execute(text("SELECT count(*) FROM kitchen_catalogue_sku_purchases")).scalar() == 0
        )

        # All seeded aliases MUST have reviewed = false
        unreviewed_prod_aliases = conn.execute(
            text("SELECT count(*) FROM kitchen_product_aliases WHERE reviewed = false")
        ).scalar()
        reviewed_prod_aliases = conn.execute(
            text("SELECT count(*) FROM kitchen_product_aliases WHERE reviewed = true")
        ).scalar()
        assert unreviewed_prod_aliases > 0
        assert reviewed_prod_aliases == 0

    assert unit_count >= 10
    assert prod_count >= 7
    assert cat_count == 13
    assert v_count == 1

    # Replay: second run must be completely idempotent
    second_res = seed_reference_data(engine)
    assert second_res.versions_applied == []
    assert second_res.units_seeded == 0
    assert second_res.products_seeded == 0

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM kitchen_canonical_units")).scalar()
            == unit_count
        )
        assert (
            conn.execute(text("SELECT count(*) FROM kitchen_canonical_products")).scalar()
            == prod_count
        )
        assert (
            conn.execute(text("SELECT count(*) FROM kitchen_reference_categories")).scalar()
            == cat_count
        )


def test_database_household_correction_is_never_overwritten(test_database_url):
    engine = create_engine(test_database_url)
    repo = PostgresIdentityRepository(engine)
    service = CanonicalIdentityService(repo)

    # Initial seed
    seed_reference_data(engine)

    # User corrects "tomat" to point to an heirloom tomato
    custom_tomato = service.create_product("heirloom tomato", "greenhouse")
    service.correct_alias("product", "nb", "", "tomat", custom_tomato.id)

    # Verify correction is marked reviewed=true in database
    with engine.connect() as conn:
        reviewed_count = conn.execute(
            text(
                "SELECT count(*) FROM kitchen_product_aliases "
                "WHERE locale='nb' AND normalized_alias='tomat' AND reviewed=true"
            )
        ).scalar()
        assert reviewed_count == 1

    # Re-run seeds
    seed_reference_data(engine)

    # Verify reviewed alias is intact and no unreviewed competing alias was added
    with engine.connect() as conn:
        aliases = conn.execute(
            text(
                "SELECT product_id, reviewed FROM kitchen_product_aliases "
                "WHERE locale='nb' AND normalized_alias='tomat'"
            )
        ).all()
        assert len(aliases) == 1
        assert aliases[0][0] == custom_tomato.id
        assert aliases[0][1] is True

    # Resolution still resolves to the user's reviewed choice
    resolved = service.resolve("product", "nb", "", "tomat")
    assert resolved.status == "resolved"
    assert resolved.candidates[0].id == custom_tomato.id


def test_database_incremental_version_update(test_database_url):
    engine = create_engine(test_database_url)

    # Apply Version 1
    seed_reference_data(engine, target_version=1)

    with engine.connect() as conn:
        v1_units = conn.execute(text("SELECT count(*) FROM kitchen_canonical_units")).scalar()

    # Register test Version 2
    test_v2 = SeedVersion(
        version=2,
        description="Version 2 test addition for database",
        units=(
            ReferenceUnit(
                canonical_name="brett",
                physical_dimension="count",
                bokmal_aliases=["brett"],
                english_aliases=["tray"],
            ),
        ),
        categories=(),
        products=(),
    )
    register_seed_version(test_v2)

    # Apply Version 2
    res = seed_reference_data(engine, target_version=2)
    assert 2 in res.versions_applied
    assert res.units_seeded == 1

    with engine.connect() as conn:
        v2_units = conn.execute(text("SELECT count(*) FROM kitchen_canonical_units")).scalar()
        assert v2_units == v1_units + 1
        applied_versions = (
            conn.execute(
                text("SELECT version FROM kitchen_reference_seed_versions ORDER BY version")
            )
            .scalars()
            .all()
        )
        assert applied_versions == [1, 2]

    # Replay of Version 2
    replay_res = seed_reference_data(engine, target_version=2)
    assert replay_res.versions_applied == []
