"""Seed a small source-attributed catalogue of generic Norwegian grocery foods.

Revision ID: 0010_catalogue
Revises: 0009_recipe_provenance
"""

from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_catalogue"
down_revision = "0009_recipe_provenance"
branch_labels = None
depends_on = None

SOURCE_URL = "https://www.matvaretabellen.no/api/nb/foods.json"
ATTRIBUTION = "Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no"
DISPLAY_NAMES = {
    "02.001": "Egg",
    "01.291": "Melk",
    "01.272": "Laktosefri helmelk",
    "01.011": "Naturell yoghurt",
    "01.275": "Laktosefri naturell yoghurt",
    "05.010": "Siktet hvetemel",
    "05.021": "Naturris, langkornet",
    "05.332": "Fullkornspasta",
    "06.070": "Hermetiske tomater",
    "06.740": "Frossen brokkoli",
    "06.747": "Brokkoli",
    "06.133": "Røde kidneybønner på boks",
    "03.205": "Kyllingfilet",
}


def upgrade() -> None:
    op.create_table(
        "kitchen_food_catalogue",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_item_id", sa.Text, nullable=False),
        sa.Column("source_name", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("locale", sa.Text, nullable=False),
        sa.Column("food_group_id", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("default_unit", sa.Text),
        sa.Column("search_keywords", postgresql.JSONB, nullable=False),
        sa.Column("source_version", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("source_attribution", sa.Text, nullable=False),
        sa.UniqueConstraint("source", "source_item_id", name="uq_food_catalogue_source_item"),
        sa.CheckConstraint("source = 'matvaretabellen'", name="ck_food_catalogue_source"),
        sa.CheckConstraint("locale = 'nb'", name="ck_food_catalogue_locale"),
        sa.CheckConstraint("source_version = '2026'", name="ck_food_catalogue_version"),
        sa.CheckConstraint(
            "source_url = 'https://www.matvaretabellen.no/api/nb/foods.json'",
            name="ck_food_catalogue_source_url",
        ),
        sa.CheckConstraint(
            "source_attribution = 'Matvaretabellen 2026. Mattilsynet. www.matvaretabellen.no'",
            name="ck_food_catalogue_attribution",
        ),
        sa.CheckConstraint("length(source_item_id) > 0", name="ck_food_catalogue_item_id"),
        sa.CheckConstraint("length(source_name) > 0", name="ck_food_catalogue_source_name"),
        sa.CheckConstraint("length(display_name) > 0", name="ck_food_catalogue_display_name"),
        sa.CheckConstraint(
            "default_unit IS NULL OR length(default_unit) > 0",
            name="ck_food_catalogue_unit",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(search_keywords) = 'array'",
            name="ck_food_catalogue_search_keywords",
        ),
    )
    op.create_index("ix_food_catalogue_display_name", "kitchen_food_catalogue", ["display_name"])

    # Frozen 2026 source names, food groups, and search keywords from the official
    # Norwegian foods endpoint. Categories are app defaults; units remain unknown.
    table = sa.table(
        "kitchen_food_catalogue",
        sa.column("id", sa.Uuid),
        sa.column("source", sa.Text),
        sa.column("source_item_id", sa.Text),
        sa.column("source_name", sa.Text),
        sa.column("display_name", sa.Text),
        sa.column("locale", sa.Text),
        sa.column("food_group_id", sa.Text),
        sa.column("category", sa.Text),
        sa.column("default_unit", sa.Text),
        sa.column("search_keywords", postgresql.JSONB),
        sa.column("source_version", sa.Text),
        sa.column("source_url", sa.Text),
        sa.column("source_attribution", sa.Text),
    )
    rows = [
        ("c567e321-043d-59e1-89a3-b2d30d1f62dd", "02.001", "Egg, rå", "2", "Dairy", []),
        (
            "24555e63-7b8e-51a6-9b38-b4a83ad4e0a7",
            "01.291",
            "Melk, uspesifisert",
            "1.1",
            "Dairy",
            ["mjølk"],
        ),
        (
            "59d597b2-919d-5517-a524-4534d849d23c",
            "01.272",
            "Helmelk, 3,5 % fett, laktosefri",
            "1.1",
            "Dairy",
            ["heilmjølk", "H-mjølk", "H-melk"],
        ),
        (
            "87aa0c06-0d63-5281-8751-4644bda2c97e",
            "01.011",
            "Yoghurt, naturell",
            "1.2",
            "Dairy",
            ["yogurt"],
        ),
        (
            "daf1b8cc-1f17-51b3-a7fc-43f5cbe47d67",
            "01.275",
            "Yoghurt, naturell, laktosefri",
            "1.2",
            "Dairy",
            ["yogurt"],
        ),
        (
            "a339016f-f3af-5f2d-bb7d-7272612256b3",
            "01.175",
            "Matfløte, 18 % fett",
            "1.3",
            "Dairy",
            ["matfløyte"],
        ),
        (
            "3d0af986-af62-51b9-836c-e2127e9ecde6",
            "01.185",
            "Gulost, uspesifisert",
            "1.4.7",
            "Dairy",
            ["kvitost", "hvitost"],
        ),
        (
            "62a08400-e019-559a-af3e-53bef3e701c1",
            "08.237",
            "Smør, usaltet",
            "8.1",
            "Dairy",
            ["meierismør"],
        ),
        ("7b7e4d26-9554-5fd7-aad9-2f62eb8e24b8", "05.003", "Havregryn", "5.2", "Pantry", []),
        (
            "2cd2c922-1b0a-5825-9300-4f7d78c3df14",
            "05.010",
            "Hvetemel, siktet",
            "5.1",
            "Pantry",
            ["kveitemjøl", "sikta"],
        ),
        (
            "3a946cff-bd11-5d70-b48f-75c2815d0a92",
            "05.006",
            "Hvetemel, sammalt, fint/grovt",
            "5.1",
            "Pantry",
            ["kveitemjøl"],
        ),
        (
            "ee2aa2c3-cef8-5051-92d0-3ec779187e4f",
            "05.021",
            "Ris, upolert, naturris, langkornet, tørr",
            "5.2",
            "Pantry",
            ["fullkornris", "fullkornsris"],
        ),
        (
            "764850be-963c-5dc0-a9ee-92a1fc73410d",
            "05.332",
            "Pasta, fullkorn, tørr",
            "5.2",
            "Pantry",
            [],
        ),
        (
            "38878c39-c254-5beb-a9ec-c1cdf3b1c483",
            "05.342",
            "Brød, grovt (50-75 %), kjøpt",
            "5.6.4",
            "Bakery",
            ["grovbrød"],
        ),
        (
            "1c545a86-f24f-5f56-88fc-d5854805059f",
            "06.036",
            "Gulrot, norsk, rå",
            "6.2",
            "Produce",
            ["rotfrukt", "rotgrønnsaker"],
        ),
        (
            "4b666428-e245-5ecb-a2b9-23873e15851c",
            "06.743",
            "Gulrot, importert, fryst",
            "6.2",
            "Frozen",
            ["rotfrukt", "grønnsaker"],
        ),
        (
            "37815748-33ea-5593-9ed1-927616d3371e",
            "06.069",
            "Tomat, norsk, rå",
            "6.2",
            "Produce",
            ["grønnsaker"],
        ),
        (
            "788fa8bb-1a15-503b-bd99-f22f54ee9902",
            "06.070",
            "Tomat, hermetisk",
            "6.6",
            "Pantry",
            ["hermetikk"],
        ),
        (
            "edd85302-a618-5590-9976-0e83d2fa2207",
            "06.010",
            "Agurk, norsk, rå",
            "6.2",
            "Produce",
            ["grønnsaker"],
        ),
        (
            "dec5e990-44d6-52ed-a8bd-ce0a05c85bb5",
            "06.747",
            "Brokkoli, importert, rå",
            "6.2",
            "Produce",
            ["grønnsaker"],
        ),
        (
            "7ca8d642-137f-539d-a771-7169d8a35c03",
            "06.740",
            "Brokkoli, importert, fryst",
            "6.2",
            "Frozen",
            ["grønnsaker"],
        ),
        (
            "d90431a5-2735-557e-a1ac-c0e5a3b6b10a",
            "06.042",
            "Løk, gul, norsk, rå",
            "6.2",
            "Produce",
            ["lauk", "grønnsaker", "kepa"],
        ),
        (
            "06b17051-ec0f-59bd-af68-fd927741ead0",
            "06.003",
            "Potet, høstpotet, rå",
            "15.1",
            "Produce",
            [],
        ),
        ("8be7d42a-a6fe-59ce-9e90-772ceee5ff6d", "06.136", "Søtpotet, rå", "6.2", "Produce", []),
        (
            "cbb208ca-6ca4-56ca-ad9c-3bb6910111ae",
            "06.048",
            "Paprika, rød, rå",
            "6.2",
            "Produce",
            ["grønnsaker"],
        ),
        (
            "5734dfef-5b29-5e13-bf20-893600f2fa30",
            "06.038",
            "Hvitløk, rå",
            "6.2",
            "Produce",
            ["kvitlauk"],
        ),
        (
            "cd541880-b62c-5adc-b087-fb5d47d781f1",
            "06.529",
            "Eple, norsk, uspesifisert, rå",
            "13.2",
            "Produce",
            ["frukt"],
        ),
        (
            "3326fef2-7602-55de-91c4-0342f90ee3b7",
            "06.525",
            "Banan, rå",
            "13.2",
            "Produce",
            ["frukt"],
        ),
        (
            "8a0de167-bb94-55d8-8e20-c3a9f2147048",
            "06.550",
            "Sitron, rå",
            "13.2",
            "Produce",
            ["sitrusfrukt"],
        ),
        (
            "10c7ff2d-34ff-5100-8792-33ce8ca7280d",
            "03.205",
            "Kylling, filet, uten skinn, rå",
            "3.1",
            "Meat",
            ["fugl", "fjørfe"],
        ),
        (
            "a5d33efb-5a70-5a8e-b016-6b26d30d9f17",
            "03.397",
            "Kjøttdeig, storfe, 13 % fett, uten salt og vann, rå",
            "3.8",
            "Meat",
            [],
        ),
        (
            "5620c8b2-f495-5a0f-b90a-bbaaa2d1f448",
            "04.220",
            "Laks, oppdrett, rå",
            "4.1.1",
            "Seafood",
            ["fisk"],
        ),
        (
            "c27147bd-bc5d-5f79-bc51-82ce8ff0742b",
            "04.327",
            "Torsk, uspesifisert, rå",
            "4.1.2",
            "Seafood",
            [],
        ),
        (
            "93fd4daa-36db-508d-8f64-37ac98bcbc28",
            "06.133",
            "Kidneybønner, røde, hermetisk",
            "12",
            "Pantry",
            ["belgfrukt", "hermetikk"],
        ),
        (
            "fff44a1b-0c2f-528c-a2aa-24d565480897",
            "06.131",
            "Linser, røde, hermetisk",
            "12",
            "Pantry",
            ["belgfrukt", "hermetikk"],
        ),
        (
            "c4f0ae4f-ede1-5b06-a137-90cc398a84e7",
            "06.736",
            "Mais, hermetisk",
            "6.6",
            "Pantry",
            ["hermetikk"],
        ),
        (
            "9a17e98f-d620-50ff-b596-238064e6dab9",
            "08.112",
            "Olivenolje, extra virgin, jomfruolje",
            "8.2",
            "Pantry",
            ["vegetabilsk"],
        ),
    ]
    op.bulk_insert(
        table,
        [
            {
                "id": UUID(row[0]),
                "source": "matvaretabellen",
                "source_item_id": row[1],
                "source_name": row[2],
                "display_name": DISPLAY_NAMES.get(row[1], row[2]),
                "locale": "nb",
                "food_group_id": row[3],
                "category": row[4],
                "default_unit": None,
                "search_keywords": row[5],
                "source_version": "2026",
                "source_url": SOURCE_URL,
                "source_attribution": ATTRIBUTION,
            }
            for row in rows
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_food_catalogue_display_name", table_name="kitchen_food_catalogue")
    op.drop_table("kitchen_food_catalogue")
