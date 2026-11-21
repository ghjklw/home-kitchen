"""Each database test owns an isolated migrated PostgreSQL schema."""

import json
import os
from pathlib import Path
from urllib.parse import quote, quote_plus
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


def _raise_database_fixture_error(stage, schema, error_type="unknown error"):
    raise RuntimeError(
        "Disposable PostgreSQL test fixture failed during "
        f"{stage} (schema={schema}; error={error_type})"
    ) from None


def _database_password_replacements():
    database_url = os.environ.get("KITCHEN_TEST_DATABASE_URL")
    if not database_url:
        return ()
    try:
        password = make_url(database_url).password
    except Exception:
        return ()
    if not password:
        return ()
    representations = {
        password,
        quote(password, safe=""),
        quote_plus(password, safe=""),
        repr(password)[1:-1],
        ascii(password)[1:-1],
        json.dumps(password, ensure_ascii=True)[1:-1],
    }
    return tuple(sorted(representations, key=len, reverse=True))


def _redact_diagnostic_text(value, secrets):
    if not isinstance(value, str):
        return value
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


def _redact_traceback(traceback, secrets):
    if traceback is None:
        return None
    traceback.extraline = _redact_diagnostic_text(traceback.extraline, secrets)
    for entry in traceback.reprentries:
        if hasattr(entry, "lines"):
            entry.lines = tuple(_redact_diagnostic_text(line, secrets) for line in entry.lines)
        locals_repr = getattr(entry, "reprlocals", None)
        if locals_repr is not None:
            locals_repr.lines = tuple(
                _redact_diagnostic_text(line, secrets) for line in locals_repr.lines
            )
        args_repr = getattr(entry, "reprfuncargs", None)
        if args_repr is not None:
            args_repr.args = tuple(
                (name, _redact_diagnostic_text(str(value), secrets))
                for name, value in args_repr.args
            )
        fileloc = getattr(entry, "reprfileloc", None)
        if fileloc is not None:
            fileloc.message = _redact_diagnostic_text(fileloc.message, secrets)
    return traceback


def _redact_crash(crash, secrets):
    if crash is not None:
        crash.message = _redact_diagnostic_text(crash.message, secrets)
    return crash


def _redact_pytest_report(report, secrets):
    if not secrets:
        return
    report.sections = [
        (name, _redact_diagnostic_text(content, secrets)) for name, content in report.sections
    ]
    longrepr = report.longrepr
    if isinstance(longrepr, str):
        report.longrepr = _redact_diagnostic_text(longrepr, secrets)
        return
    chain = getattr(longrepr, "chain", None)
    if chain is not None:
        longrepr.chain = tuple(
            (
                _redact_traceback(traceback, secrets),
                _redact_crash(crash, secrets),
                _redact_diagnostic_text(description, secrets),
            )
            for traceback, crash, description in chain
        )
    traceback = getattr(longrepr, "reprtraceback", None)
    if traceback is not None:
        _redact_traceback(traceback, secrets)
    _redact_crash(getattr(longrepr, "reprcrash", None), secrets)
    if hasattr(longrepr, "sections"):
        longrepr.sections = [
            (name, _redact_diagnostic_text(content, secrets), sep)
            for name, content, sep in longrepr.sections
        ]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    _redact_pytest_report(outcome.get_result(), _database_password_replacements())


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: DB-backed or disposable infrastructure integration tests",
    )
    config.addinivalue_line(
        "markers",
        "mocked_provider: offline tests using mocked, stubbed, or synthetic provider/AI transports (not live acceptance evidence)",
    )
    config.addinivalue_line(
        "markers",
        "live_acceptance: live provider acceptance tests against real external endpoints or credentials (excluded from offline CI)",
    )


def pytest_collection_modifyitems(items):
    for item in items:
        if "test_database_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def test_database_url():
    base_url = os.environ.get("KITCHEN_TEST_DATABASE_URL")
    if not base_url:
        pytest.fail("Set KITCHEN_TEST_DATABASE_URL to a disposable PostgreSQL database")
    base_url = _RedactedDatabaseURL(base_url)
    schema = "test_" + uuid4().hex
    admin = None
    schema_created = False
    setup_failure = False
    try:
        try:
            admin = create_engine(base_url)
        except Exception as error:
            error_type = type(error).__name__
            setup_failure = True
            del error
            _raise_database_fixture_error("create admin engine", schema, error_type)
        try:
            with admin.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        except Exception as error:
            error_type = type(error).__name__
            setup_failure = True
            del error
            _raise_database_fixture_error("create schema", schema, error_type)
        try:
            url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
            database_url = _RedactedDatabaseURL(url.render_as_string(hide_password=False))
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            # Alembic and SQLAlchemy accept URL objects directly; preserve their
            # password-redacted repr in any setup exception or pytest traceback.
            config.attributes["database_url"] = url
            command.upgrade(config, "head")
        except Exception as error:
            error_type = type(error).__name__
            setup_failure = True
            del error
            _raise_database_fixture_error("Alembic migration", schema, error_type)
        yield database_url
    finally:
        if admin is not None:
            try:
                if schema_created:
                    with admin.begin() as connection:
                        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            except Exception:
                if not setup_failure:
                    _raise_database_fixture_error("drop schema", schema)
            finally:
                try:
                    admin.dispose()
                except Exception as error:
                    error_type = type(error).__name__
                    del error
                    if not setup_failure:
                        _raise_database_fixture_error("dispose admin engine", schema, error_type)


@pytest.fixture(autouse=True)
def settings_database_url(monkeypatch):
    # Pure configuration/AI tests do not connect; database tests pass the schema URL.
    monkeypatch.setenv(
        "KITCHEN_DATABASE_URL",
        os.environ.get("KITCHEN_TEST_DATABASE_URL")
        or "postgresql+psycopg://localhost/kitchen_test",
    )
