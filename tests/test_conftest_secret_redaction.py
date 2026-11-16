"""Exercise database fixture failures through pytest with synthetic credentials."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_PASSWORD_MARKER = "__DB_SECRET_REDACTION_CANARY__"

_PLUGIN_SOURCE = """
import os
import sys
from pathlib import Path

import sqlalchemy
from sqlalchemy.exc import OperationalError


class _Connection:
    def execute(self, *_args, **_kwargs):
        return None


class _Transaction:
    def __enter__(self):
        return _Connection()

    def __exit__(self, *_args):
        return False


def pytest_runtest_setup(item):
    fixture = item._fixtureinfo.name2fixturedefs["test_database_url"][-1].func
    fixture_module = sys.modules[fixture.__module__]
    mode = os.environ["REDACTION_PROBE_MODE"]
    original_create_engine = sqlalchemy.create_engine
    engine_count = 0

    def create_probe_engine(url, **kwargs):
        nonlocal engine_count
        engine_count += 1
        engine = original_create_engine(url, **kwargs)
        if mode == "setup_failure" or (mode == "alembic_failure" and engine_count == 2):
            def fail_connect(*args, **connect_params):
                Path(os.environ["REDACTION_CONNECT_REACHED"]).write_text("reached")
                raise OperationalError(
                    "synthetic connect failure",
                    connect_params,
                    RuntimeError("synthetic driver failure"),
                )

            # Patch only dialect.connect; no socket or database access occurs.
            engine.dialect.connect = fail_connect
        elif mode in {"alembic_failure", "test_failure"}:
            # The admin engine's schema DDL is a no-op in these probes. The
            # Alembic engine remains a real SQLAlchemy Engine in alembic_failure.
            engine.begin = lambda: _Transaction()
        return engine

    fixture_module.create_engine = create_probe_engine
    sqlalchemy.create_engine = create_probe_engine

    if mode == "test_failure":
        fixture_module.command.upgrade = lambda *_args: None
"""

_PROBE_SOURCE = """
import os

import pytest
from sqlalchemy.engine import make_url


def test_fixture_failure_output(test_database_url):
    options = make_url(test_database_url).query["options"]
    schema = options.split("search_path=", 1)[1]
    if os.environ["REDACTION_PROBE_MODE"] == "test_failure":
        pytest.fail(
            f"deliberate test stage=consumer schema={schema} "
            f"database_url={test_database_url!r}"
        )
"""


def _run_nested_pytest(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    project = tmp_path / mode
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests_dir / "conftest.py")
    shutil.copytree(Path(__file__).parents[1] / "migrations", project / "migrations")
    (project / "src").mkdir()
    shutil.copytree(
        Path(__file__).parents[1] / "src" / "home_kitchen",
        project / "src" / "home_kitchen",
    )
    shutil.copyfile(Path(__file__).parents[1] / "alembic.ini", project / "alembic.ini")
    (project / "redaction_probe.py").write_text(_PLUGIN_SOURCE)
    (tests_dir / "test_probe.py").write_text(_PROBE_SOURCE)
    connect_reached = project / "connect-reached"

    env = {
        "HOME": str(project),
        "KITCHEN_TEST_DATABASE_URL": (
            f"postgresql+psycopg://fixture-user:{_PASSWORD_MARKER}@127.0.0.1:1/kitchen_test"
        ),
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join((str(project / "src"), str(project))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "REDACTION_PROBE_MODE": mode,
        "REDACTION_CONNECT_REACHED": str(connect_reached),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "redaction_probe",
            "--showlocals",
            "-q",
            "tests/test_probe.py",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, connect_reached


def test_setup_migration_and_consumer_failure_output_redacts_password(
    tmp_path: Path,
) -> None:
    for mode, stage, needs_driver_failure in (
        ("setup_failure", "create schema", True),
        ("alembic_failure", "Alembic migration", True),
        ("test_failure", "stage=consumer", False),
    ):
        result, connect_reached = _run_nested_pytest(tmp_path, mode)
        output = result.stdout + result.stderr

        assert result.returncode != 0, "the nested pytest probe must fail deliberately"
        assert _PASSWORD_MARKER not in output, "database password marker leaked into pytest output"
        assert stage in output, "failure stage should remain visible"
        assert "schema=test_" in output, "isolated schema context should remain visible"
        if needs_driver_failure:
            assert connect_reached.exists(), "probe must reach the patched dialect.connect"
