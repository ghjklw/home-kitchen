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

from sqlalchemy.engine import make_url


class _Connection:
    def execute(self, *_args, **_kwargs):
        return None


class _Transaction:
    def __enter__(self):
        return _Connection()

    def __exit__(self, *_args):
        return False


class _Engine:
    def begin(self):
        return _Transaction()

    def dispose(self):
        return None


def pytest_runtest_setup(item):
    fixture = item._fixtureinfo.name2fixturedefs["test_database_url"][-1].func
    fixture_module = sys.modules[fixture.__module__]
    fixture_module.create_engine = lambda _url: _Engine()

    def upgrade(config, _revision):
        if os.environ["REDACTION_PROBE_MODE"] == "setup_failure":
            url = config.attributes["database_url"]
            options = make_url(url).query["options"]
            schema = options.split("search_path=", 1)[1]
            raise RuntimeError(
                f"migration setup stage=upgrade schema={schema} database_url={url!r}"
            )

    fixture_module.command.upgrade = upgrade
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


def _run_nested_pytest(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    project = tmp_path / mode
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests_dir / "conftest.py")
    shutil.copyfile(Path(__file__).parents[1] / "alembic.ini", project / "alembic.ini")
    (project / "redaction_probe.py").write_text(_PLUGIN_SOURCE)
    (tests_dir / "test_probe.py").write_text(_PROBE_SOURCE)

    env = {
        "HOME": str(project),
        "KITCHEN_TEST_DATABASE_URL": (
            f"postgresql+psycopg://fixture-user:{_PASSWORD_MARKER}@127.0.0.1:1/kitchen_test"
        ),
        "PATH": os.defpath,
        "PYTHONPATH": str(project),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "REDACTION_PROBE_MODE": mode,
    }
    return subprocess.run(
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


def test_setup_and_test_failure_output_redacts_database_password(tmp_path: Path) -> None:
    for mode, stage in (
        ("setup_failure", "stage=upgrade"),
        ("test_failure", "stage=consumer"),
    ):
        result = _run_nested_pytest(tmp_path, mode)
        output = result.stdout + result.stderr

        assert result.returncode != 0, "the nested pytest probe must fail deliberately"
        assert _PASSWORD_MARKER not in output, "database password marker leaked into pytest output"
        assert stage in output, "failure stage should remain visible"
        assert "schema=test_" in output, "isolated schema context should remain visible"
