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
    assert '[[ "$sha" =~ ^[0-9a-f]{40}$ ]]' in WORKFLOW
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
