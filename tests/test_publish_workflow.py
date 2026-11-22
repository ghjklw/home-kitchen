"""Guard the main publisher identity contract consumed by release promotion."""

import json
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(".github/workflows/publish.yml").read_text()


def test_only_successful_main_ci_can_publish():
    trigger = WORKFLOW.split("\npermissions:", 1)[0]
    assert "workflows: [Kitchen CI]" in trigger
    assert "types: [completed]" in trigger
    assert "branches: [main]" in trigger
    assert "release:" not in trigger
    assert "github.event.release" not in WORKFLOW
    assert "github.event_name == 'workflow_run'" in WORKFLOW
    assert "github.event.workflow_run.conclusion == 'success'" in WORKFLOW
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in WORKFLOW
    assert "github.event.workflow_run.head_branch == 'main'" in WORKFLOW
    assert "ref: ${{ github.event.workflow_run.head_sha }}" in WORKFLOW


def test_image_identity_stays_bound_to_main_publish_run():
    assert "sha=$(git rev-parse HEAD)" in WORKFLOW
    assert '[[ "$sha" =~ ^[0-9a-f]{40}$ && "$sha" == "$EXPECTED_SHA" ]]' in WORKFLOW
    assert "APP_REVISION=${{ steps.source.outputs.sha }}" in WORKFLOW
    assert "org.opencontainers.image.revision=${{ steps.source.outputs.sha }}" in WORKFLOW
    assert "io.home-kitchen.run-id=${{ github.run_id }}" in WORKFLOW
    assert "io.home-kitchen.run-attempt=${{ github.run_attempt }}" in WORKFLOW
    assert "build-${{ github.run_id }}-${{ github.run_attempt }}" in WORKFLOW
    assert "DIGEST: ${{ steps.image.outputs.digest }}" in WORKFLOW
    assert "name: image-metadata" in WORKFLOW


def record_metadata(monkeypatch, tmp_path, digest):
    for name, value in {
        "SOURCE_SHA": "a" * 40,
        "CHANNEL": "main",
        "DIGEST": digest,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    script = WORKFLOW.split("          python - <<'PY'\n", 1)[1].split("          PY\n", 1)[0]
    exec(compile(textwrap.dedent(script), "publish.yml", "exec"), {})
    return json.loads(Path("image-metadata.json").read_text())


def test_metadata_records_exact_digest_and_original_publisher(monkeypatch, tmp_path):
    digest = "sha256:" + "b" * 64
    assert record_metadata(monkeypatch, tmp_path, digest) == {
        "source_sha": "a" * 40,
        "event": "main",
        "image": "ghcr.io/ghjklw/home-kitchen",
        "digest": digest,
        "version": None,
        "tag": "build-123-2",
    }


@pytest.mark.parametrize("digest", ["", "sha256:" + "b" * 63, "latest"])
def test_metadata_rejects_non_digest_identity(monkeypatch, tmp_path, digest):
    with pytest.raises(AssertionError):
        record_metadata(monkeypatch, tmp_path, digest)


def test_publish_reuses_upstream_ci_instead_of_rerunning_tests():
    import yaml

    workflow = yaml.safe_load(WORKFLOW)
    publish = workflow["jobs"]["publish"]
    assert "services" not in publish
    assert "KITCHEN_TEST_DATABASE_URL" not in publish.get("env", {})
    assert "github.event.workflow_run.conclusion == 'success'" in publish["if"]

    steps = publish["steps"]
    assert not any("setup-node" in step.get("uses", "") for step in steps)
    forbidden_commands = (
        "uv sync",
        "pre-commit run",
        "ruff check",
        "ruff format",
        "ty check",
        "node --test",
        "pytest",
    )
    assert not any(
        command in step.get("run", "")
        for step in steps
        for command in forbidden_commands
    )
    summary = next(
        step for step in steps if step.get("name") == "Record successful upstream CI gate"
    )
    assert summary["env"]["CI_RUN_URL"] == "${{ github.event.workflow_run.html_url }}"
    assert summary["env"]["CI_HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert any("docker/build-push-action@" in step.get("uses", "") for step in steps)


def test_kitchen_ci_remains_the_single_required_test_suite():
    import yaml

    ci = yaml.safe_load(Path(".github/workflows/ci.yml").read_text())
    steps = ci["jobs"]["checks"]["steps"]
    commands = [step.get("run", "") for step in steps]
    assert any("pre-commit run --all-files" in command for command in commands)
    assert sum("pytest -m" in command for command in commands) == 3
