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


def _raise_database_fixture_error(stage, schema):
    raise RuntimeError(
        f"Disposable PostgreSQL test fixture failed during {stage} (schema={schema})"
    ) from None


@pytest.fixture
def test_database_url():
    base_url = os.environ.get("KITCHEN_TEST_DATABASE_URL")
    if not base_url:
        pytest.fail("Set KITCHEN_TEST_DATABASE_URL to a disposable PostgreSQL database")
    base_url = _RedactedDatabaseURL(base_url)
    schema = "test_" + uuid4().hex
    admin = None
    schema_created = False
    try:
        try:
            admin = create_engine(base_url)
        except Exception:
            _raise_database_fixture_error("create admin engine", schema)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        except Exception:
            _raise_database_fixture_error("create schema", schema)
        try:
            url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
            database_url = _RedactedDatabaseURL(url.render_as_string(hide_password=False))
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            # Alembic and SQLAlchemy accept URL objects directly; preserve their
            # password-redacted repr in any setup exception or pytest traceback.
            config.attributes["database_url"] = url
            command.upgrade(config, "head")
        except Exception:
            _raise_database_fixture_error("Alembic migration", schema)
        yield database_url
    finally:
        if admin is not None:
            try:
                if schema_created:
                    with admin.begin() as connection:
                        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            except Exception:
                _raise_database_fixture_error("drop schema", schema)
            finally:
                try:
                    admin.dispose()
                except Exception:
                    _raise_database_fixture_error("dispose admin engine", schema)


@pytest.fixture(autouse=True)
def settings_database_url(monkeypatch):
    # Pure configuration/AI tests do not connect; database tests pass the schema URL.
    monkeypatch.setenv(
        "KITCHEN_DATABASE_URL",
        os.environ.get("KITCHEN_TEST_DATABASE_URL", "postgresql+psycopg://localhost/kitchen_test"),
    )
