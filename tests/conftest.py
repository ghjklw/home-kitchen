"""Each database test owns an isolated migrated PostgreSQL schema."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


class _RedactedDatabaseURL(str):
    """Keep a connectable URL string whose debug representation hides secrets."""

    def __repr__(self):
        try:
            safe_url = make_url(str(self)).render_as_string(hide_password=True)
        except Exception:
            safe_url = "<redacted database URL>"
        return f"{type(self).__name__}({safe_url!r})"


@pytest.fixture
def test_database_url():
    base_url = os.environ.get("KITCHEN_TEST_DATABASE_URL")
    if not base_url:
        pytest.fail("Set KITCHEN_TEST_DATABASE_URL to a disposable PostgreSQL database")
    base_url = _RedactedDatabaseURL(base_url)
    schema = "test_" + uuid4().hex
    admin = create_engine(base_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
    database_url = _RedactedDatabaseURL(url.render_as_string(hide_password=False))
    try:
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        # Alembic and SQLAlchemy accept URL objects directly; preserve their
        # password-redacted repr in any setup exception or pytest traceback.
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        yield database_url
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(autouse=True)
def settings_database_url(monkeypatch):
    # Pure configuration/AI tests do not connect; database tests pass the schema URL.
    monkeypatch.setenv(
        "KITCHEN_DATABASE_URL",
        os.environ.get("KITCHEN_TEST_DATABASE_URL", "postgresql+psycopg://localhost/kitchen_test"),
    )
