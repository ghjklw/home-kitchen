"""Guard the immutable UAT-to-production release promotion workflow and helper."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_WORKFLOW_PATH = REPO_ROOT / ".github/workflows/release-image.yml"
RELEASE_WORKFLOW_TEXT = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
RELEASE_SCRIPT_PATH = REPO_ROOT / ".github/scripts/release_promotion.py"


def _load_release_promotion_module():
    spec = importlib.util.spec_from_file_location("release_promotion", RELEASE_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_promotion"] = module
    spec.loader.exec_module(module)
    return module


release_promotion = _load_release_promotion_module()

SAMPLE_SHA = "a" * 40


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _make_oci_index_fixtures(
    *,
    repository: str = "ghjklw/home-kitchen",
    revision: str = SAMPLE_SHA,
    run_id: str = "98765",
    run_attempt: str = "1",
):
    config_bytes = json.dumps(
        {
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": f"https://github.com/{repository}",
                    "org.opencontainers.image.revision": revision,
                    "io.home-kitchen.run-id": run_id,
                    "io.home-kitchen.run-attempt": run_attempt,
                }
            }
        }
    ).encode("utf-8")
    config_digest = _digest_of(config_bytes)

    amd64_manifest_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": config_digest, "size": len(config_bytes)},
            "layers": [],
        }
    ).encode("utf-8")
    amd64_digest = _digest_of(amd64_manifest_bytes)

    index_bytes = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": amd64_digest,
                    "size": len(amd64_manifest_bytes),
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
                {
                    "digest": "sha256:" + "f" * 64,
                    "size": 100,
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                },
            ],
        }
    ).encode("utf-8")
    index_digest = _digest_of(index_bytes)

    manifests = {
        index_digest: (index_bytes, "application/vnd.oci.image.index.v1+json"),
        amd64_digest: (amd64_manifest_bytes, "application/vnd.oci.image.manifest.v1+json"),
    }
    blobs = {config_digest: config_bytes}
    return index_digest, index_bytes, manifests, blobs


def test_release_workflow_requires_manual_dispatch_and_production_environment():
    workflow = yaml.safe_load(RELEASE_WORKFLOW_TEXT)
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert list(triggers.keys()) == ["workflow_dispatch"]
    for forbidden in ("push", "pull_request", "workflow_run", "release"):
        assert forbidden not in triggers

    inputs = triggers["workflow_dispatch"]["inputs"]
    expected_inputs = {
        "release_tag",
        "source_sha",
        "source_digest",
        "uat_health_verified",
        "uat_migrations_verified",
        "uat_backup_recovery_verified",
        "uat_consumer_verified",
        "uat_evidence_ref",
        "confirm_production_promotion",
    }
    assert expected_inputs.issubset(inputs.keys())
    for gate in (
        "uat_health_verified",
        "uat_migrations_verified",
        "uat_backup_recovery_verified",
        "uat_consumer_verified",
    ):
        assert inputs[gate]["type"] == "boolean"
        assert inputs[gate]["default"] is False
    assert inputs["confirm_production_promotion"]["default"] == ""

    jobs = workflow["jobs"]
    assert "verify" in jobs and "promote" in jobs
    assert jobs["verify"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "packages": "read",
    }
    assert jobs["promote"]["needs"] == ["verify"]
    assert jobs["promote"]["environment"] == "production"
    assert jobs["promote"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }


def test_release_workflow_never_rebuilds_or_injects_inputs_in_shell():
    assert "docker/build-push-action" not in RELEASE_WORKFLOW_TEXT
    assert "docker/setup-buildx-action" not in RELEASE_WORKFLOW_TEXT
    assert "docker build" not in RELEASE_WORKFLOW_TEXT
    assert "persist-credentials: false" in RELEASE_WORKFLOW_TEXT

    workflow = yaml.safe_load(RELEASE_WORKFLOW_TEXT)
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            run_script = step.get("run", "")
            assert "github.event.inputs" not in run_script


def test_validate_promotion_inputs_enforces_uat_and_confirmation_gates():
    req = release_promotion.validate_promotion_inputs(
        repository="ghjklw/home-kitchen",
        release_tag="v0.2.0",
        source_sha=SAMPLE_SHA,
        source_digest="sha256:" + "b" * 64,
        uat_health_verified="true",
        uat_migrations_verified="true",
        uat_backup_recovery_verified="true",
        uat_consumer_verified="true",
        uat_evidence_ref="https://github.com/ghjklw/home-kitchen/issues/114#issuecomment-1",
        confirm_production_promotion="PROMOTE TO PRODUCTION",
    )
    assert req.release_tag == "v0.2.0"
    assert req.source_sha == SAMPLE_SHA

    bad_cases = [
        {"release_tag": "main"},
        {"source_sha": "abc123"},
        {"source_digest": "latest"},
        {"source_digest": "sha256:" + "b" * 63},
        {"uat_health_verified": "false"},
        {"uat_migrations_verified": "false"},
        {"uat_backup_recovery_verified": "false"},
        {"uat_consumer_verified": "false"},
        {"uat_evidence_ref": "  "},
        {"confirm_production_promotion": "yes"},
    ]
    base: dict[str, Any] = {
        "repository": "ghjklw/home-kitchen",
        "release_tag": "v0.2.0",
        "source_sha": SAMPLE_SHA,
        "source_digest": "sha256:" + "b" * 64,
        "uat_health_verified": "true",
        "uat_migrations_verified": "true",
        "uat_backup_recovery_verified": "true",
        "uat_consumer_verified": "true",
        "uat_evidence_ref": "UAT run #42",
        "confirm_production_promotion": "PROMOTE TO PRODUCTION",
    }
    for override in bad_cases:
        with pytest.raises(ValueError):
            release_promotion.validate_promotion_inputs(**{**base, **override})


def test_compute_release_tags_only_moves_latest_for_stable_semver():
    assert release_promotion.compute_release_tags("v0.2.0", SAMPLE_SHA) == [
        "v0.2.0",
        "release-v0.2.0",
        f"release-sha-{SAMPLE_SHA}",
        "latest",
    ]
    assert release_promotion.compute_release_tags("v0.2.0-rc.1", SAMPLE_SHA) == [
        "v0.2.0-rc.1",
        "release-v0.2.0-rc.1",
        f"release-sha-{SAMPLE_SHA}",
    ]


def test_extract_oci_identity_verifies_index_digest_and_labels():
    index_digest, index_bytes, manifests, blobs = _make_oci_index_fixtures()
    identity = release_promotion.extract_oci_identity(
        index_bytes,
        "application/vnd.oci.image.index.v1+json",
        expected_digest=index_digest,
        expected_repository="ghjklw/home-kitchen",
        expected_sha=SAMPLE_SHA,
        fetch_manifest=manifests.__getitem__,
        fetch_blob=blobs.__getitem__,
    )
    assert identity.digest == index_digest
    assert identity.revision == SAMPLE_SHA
    assert identity.run_id == "98765"
    assert identity.run_attempt == "1"

    with pytest.raises(ValueError, match="digest mismatch"):
        release_promotion.extract_oci_identity(
            index_bytes,
            "application/vnd.oci.image.index.v1+json",
            expected_digest="sha256:" + "0" * 64,
            expected_repository="ghjklw/home-kitchen",
            expected_sha=SAMPLE_SHA,
            fetch_manifest=manifests.__getitem__,
            fetch_blob=blobs.__getitem__,
        )

    with pytest.raises(ValueError, match="org.opencontainers.image.revision"):
        release_promotion.extract_oci_identity(
            index_bytes,
            "application/vnd.oci.image.index.v1+json",
            expected_digest=index_digest,
            expected_repository="ghjklw/home-kitchen",
            expected_sha="c" * 40,
            fetch_manifest=manifests.__getitem__,
            fetch_blob=blobs.__getitem__,
        )


def test_validate_publish_metadata_contract():
    digest = "sha256:" + "d" * 64
    metadata = {
        "source_sha": SAMPLE_SHA,
        "event": "main",
        "image": "ghcr.io/ghjklw/home-kitchen",
        "digest": digest,
        "version": None,
        "tag": "build-98765-1",
    }
    release_promotion.validate_publish_metadata(
        metadata,
        repository="ghjklw/home-kitchen",
        source_sha=SAMPLE_SHA,
        source_digest=digest,
        run_id="98765",
        run_attempt="1",
    )

    with pytest.raises(ValueError, match="digest"):
        release_promotion.validate_publish_metadata(
            {**metadata, "digest": "sha256:" + "e" * 64},
            repository="ghjklw/home-kitchen",
            source_sha=SAMPLE_SHA,
            source_digest=digest,
            run_id="98765",
            run_attempt="1",
        )


def test_handle_promote_tags_exact_digest_and_omits_credentials(monkeypatch, tmp_path):
    index_digest, index_bytes, manifests, blobs = _make_oci_index_fixtures()
    pushed_tags: list[tuple[str, bytes, str, str]] = []
    secret_token = "ghp_super_secret_token_value"

    class FakeGhcrClient:
        def __init__(self, repository: str, actor: str, token: str, scope: str = "pull") -> None:
            self.repository = repository
            self.actor = actor
            self.token = token
            self.scope = scope

        def get_manifest(self, reference: str) -> tuple[bytes, str]:
            return manifests[reference]

        def get_blob(self, digest: str) -> bytes:
            return blobs[digest]

        def put_manifest(
            self, tag: str, manifest_bytes: bytes, content_type: str, expected_digest: str
        ) -> str:
            pushed_tags.append((tag, manifest_bytes, content_type, expected_digest))
            return expected_digest

    monkeypatch.setattr(release_promotion, "GhcrClient", FakeGhcrClient)
    monkeypatch.chdir(tmp_path)
    summary_file = tmp_path / "summary.md"

    for name, value in {
        "GITHUB_REPOSITORY": "ghjklw/home-kitchen",
        "GITHUB_ACTOR": "ghjklw",
        "GITHUB_RUN_ID": "55555",
        "GITHUB_STEP_SUMMARY": str(summary_file),
        "GHCR_TOKEN": secret_token,
        "PUBLISH_RUN_ID": "98765",
        "PUBLISH_RUN_ATTEMPT": "1",
        "INPUT_RELEASE_TAG": "v0.2.0",
        "INPUT_SOURCE_SHA": SAMPLE_SHA,
        "INPUT_SOURCE_DIGEST": index_digest,
        "INPUT_UAT_HEALTH_VERIFIED": "true",
        "INPUT_UAT_MIGRATIONS_VERIFIED": "true",
        "INPUT_UAT_BACKUP_RECOVERY_VERIFIED": "true",
        "INPUT_UAT_CONSUMER_VERIFIED": "true",
        "INPUT_UAT_EVIDENCE_REF": "UAT verified in issue #114",
        "INPUT_CONFIRM_PRODUCTION_PROMOTION": "PROMOTE TO PRODUCTION",
    }.items():
        monkeypatch.setenv(name, value)

    assert release_promotion.handle_promote() == 0

    assert [tag for tag, *_ in pushed_tags] == [
        "v0.2.0",
        "release-v0.2.0",
        f"release-sha-{SAMPLE_SHA}",
        "latest",
    ]
    for _, payload, content_type, digest in pushed_tags:
        assert payload == index_bytes
        assert content_type == "application/vnd.oci.image.index.v1+json"
        assert digest == index_digest

    record_text = (tmp_path / "promotion-metadata.json").read_text(encoding="utf-8")
    assert secret_token not in record_text
    assert secret_token not in summary_file.read_text(encoding="utf-8")
    record = json.loads(record_text)
    assert record["digest"] == index_digest
    assert record["source_sha"] == SAMPLE_SHA
    assert record["environment"] == "production"
    assert record["uat_verification"]["backup_recovery_verified"] is True


def test_ghcr_client_error_does_not_leak_token(monkeypatch):
    secret_token = "ghs_do_not_leak_this_token"
    client = release_promotion.GhcrClient("ghjklw/home-kitchen", "ghjklw", secret_token)

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="https://ghcr.io/token",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as exc_info:
        client.get_manifest("sha256:" + "a" * 64)
    assert secret_token not in str(exc_info.value)
