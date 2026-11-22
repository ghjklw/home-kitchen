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
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

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
        "promotion_action",
        "uat_url",
        "dry_run",
    }
    assert expected_inputs.issubset(inputs.keys())
    for gate in (
        "uat_health_verified",
        "uat_migrations_verified",
        "uat_backup_recovery_verified",
        "uat_consumer_verified",
        "dry_run",
    ):
        assert inputs[gate]["type"] == "boolean"
        assert inputs[gate]["default"] is False
    assert inputs["confirm_production_promotion"]["default"] == ""
    assert inputs["promotion_action"]["options"] == ["promote", "rollback"]
    assert inputs["promotion_action"]["default"] == "promote"

    jobs = workflow["jobs"]
    assert "verify" in jobs and "promote" in jobs
    assert jobs["verify"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "packages": "read",
    }
    verify_step = next(s for s in jobs["verify"]["steps"] if s.get("id") == "verify")
    assert verify_step["env"]["UAT_API_TOKEN"] == "${{ secrets.UAT_API_TOKEN }}"
    assert jobs["promote"]["needs"] == ["verify"]
    assert jobs["promote"]["environment"] == "production"
    assert "inputs.dry_run != true" in jobs["promote"]["if"]
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


def test_validate_promotion_inputs_supports_dry_run_rollback_and_uat_url():
    req = release_promotion.validate_promotion_inputs(
        repository="ghjklw/home-kitchen",
        release_tag="v0.1.5",
        source_sha=SAMPLE_SHA,
        source_digest="sha256:" + "b" * 64,
        uat_health_verified="true",
        uat_migrations_verified="true",
        uat_backup_recovery_verified="true",
        uat_consumer_verified="true",
        uat_evidence_ref="Dry run in issue #114",
        confirm_production_promotion="",
        promotion_action="rollback",
        uat_url="https://kitchen-uat.maki.maloline.fr/",
        dry_run="true",
    )
    assert req.promotion_action == "rollback"
    assert req.uat_url == "https://kitchen-uat.maki.maloline.fr"
    assert req.dry_run is True

    dry_base: dict[str, Any] = {
        "repository": "ghjklw/home-kitchen",
        "release_tag": "v0.1.5",
        "source_sha": SAMPLE_SHA,
        "source_digest": "sha256:" + "b" * 64,
        "uat_health_verified": "true",
        "uat_migrations_verified": "true",
        "uat_backup_recovery_verified": "true",
        "uat_consumer_verified": "true",
        "uat_evidence_ref": "Dry run",
        "confirm_production_promotion": "",
        "dry_run": "true",
    }
    for bad_override in (
        {"promotion_action": "force"},
        {"uat_url": "http://kitchen-uat.maki.maloline.fr"},
        {"uat_url": "https://user:pass@kitchen-uat.maki.maloline.fr"},
        {"uat_url": "https://kitchen-uat.maki.maloline.fr/api"},
        {"confirm_production_promotion": "maybe"},
    ):
        with pytest.raises(ValueError):
            release_promotion.validate_promotion_inputs(**{**dry_base, **bad_override})


def test_verify_immutable_tag_continuity_blocks_substitution_and_supports_rollback():
    index_digest, index_bytes, manifests, _ = _make_oci_index_fixtures()
    other_digest = "sha256:" + "e" * 64
    other_bytes = b'{"other": true}'

    class TagStubGhcr:
        def __init__(self, tag_map: dict[str, bytes]) -> None:
            self.tag_map = tag_map

        def get_manifest(self, reference: str) -> tuple[bytes, str]:
            return self.tag_map[reference], "application/vnd.oci.image.index.v1+json"

    # Forward promote with :latest pointing to a prior digest succeeds and records previous :latest.
    continuity = release_promotion.verify_immutable_tag_continuity(
        TagStubGhcr({"latest": other_bytes}),
        release_tag="v0.2.0",
        source_sha=SAMPLE_SHA,
        source_digest=index_digest,
        promotion_action="promote",
    )
    assert continuity["previous_latest_digest"] == _digest_of(other_bytes)
    assert continuity["all_immutable_tags_match"] is False

    # Existing immutable version tag pointing to another digest must be rejected.
    with pytest.raises(ValueError, match="refusing to substitute"):
        release_promotion.verify_immutable_tag_continuity(
            TagStubGhcr({"v0.2.0": other_bytes}),
            release_tag="v0.2.0",
            source_sha=SAMPLE_SHA,
            source_digest=index_digest,
            promotion_action="promote",
        )

    # Rollback requires existing promoted release-sha tag and allows re-pointing :latest.
    with pytest.raises(ValueError, match="release-sha-"):
        release_promotion.verify_immutable_tag_continuity(
            TagStubGhcr({"latest": other_bytes}),
            release_tag="v0.1.5",
            source_sha=SAMPLE_SHA,
            source_digest=index_digest,
            promotion_action="rollback",
        )

    rb_continuity = release_promotion.verify_immutable_tag_continuity(
        TagStubGhcr(
            {
                "v0.1.5": index_bytes,
                "release-v0.1.5": index_bytes,
                f"release-sha-{SAMPLE_SHA}": index_bytes,
                "latest": other_bytes,
            }
        ),
        release_tag="v0.1.5",
        source_sha=SAMPLE_SHA,
        source_digest=index_digest,
        promotion_action="rollback",
    )
    assert rb_continuity["all_immutable_tags_match"] is True
    assert rb_continuity["previous_latest_digest"] != other_digest
    assert rb_continuity["previous_latest_digest"] == _digest_of(other_bytes)


def test_verify_publisher_run_checks_attempt_and_rollback_artifact_expiry(monkeypatch):
    digest = "sha256:" + "d" * 64
    calls: list[list[str]] = []

    def fake_run_command(cmd: list[str], *, env=None) -> str:
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            assert cmd[2].endswith("/actions/runs/98765/attempts/2")
            return json.dumps(
                {
                    "path": ".github/workflows/publish.yml",
                    "conclusion": "success",
                    "head_branch": "main",
                    "head_sha": SAMPLE_SHA,
                    "run_attempt": 2,
                    "repository": {"full_name": "ghjklw/home-kitchen"},
                    "head_repository": {"full_name": "ghjklw/home-kitchen"},
                }
            )
        if cmd[:3] == ["gh", "run", "download"]:
            raise RuntimeError("artifact expired")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(release_promotion, "_run_command", fake_run_command)

    # Forward promote fails when image-metadata artifact is missing/expired.
    with pytest.raises(RuntimeError, match="artifact expired"):
        release_promotion.verify_publisher_run(
            "ghjklw/home-kitchen",
            run_id="98765",
            run_attempt="2",
            source_sha=SAMPLE_SHA,
            source_digest=digest,
            allow_expired_artifact_for_rollback=False,
        )

    # Rollback with verified existing immutable GHCR tags succeeds cleanly.
    meta = release_promotion.verify_publisher_run(
        "ghjklw/home-kitchen",
        run_id="98765",
        run_attempt="2",
        source_sha=SAMPLE_SHA,
        source_digest=digest,
        allow_expired_artifact_for_rollback=True,
    )
    assert meta["artifact_expired"] is True
    assert meta["tag"] == "build-98765-2"


def test_verify_git_and_ci_rejects_fork_or_pr_ci_runs(monkeypatch):
    def fake_run_command(cmd: list[str], *, env=None) -> str:
        if cmd[:2] == ["gh", "api"]:
            return json.dumps(
                {
                    "workflow_runs": [
                        {
                            "conclusion": "success",
                            "head_branch": "main",
                            "head_sha": SAMPLE_SHA,
                            "head_repository": {"full_name": "attacker/home-kitchen"},
                            "event": "pull_request",
                        }
                    ]
                }
            )
        return ""

    class OkResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(release_promotion, "_run_command", fake_run_command)
    monkeypatch.setattr(release_promotion.subprocess, "run", lambda *a, **kw: OkResult())

    with pytest.raises(ValueError, match="No successful Kitchen CI run"):
        release_promotion.verify_git_and_ci("ghjklw/home-kitchen", SAMPLE_SHA, "v0.2.0")


def test_resolve_alembic_head_matches_alembic_script_directory():
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    expected = ScriptDirectory.from_config(cfg).get_heads()
    assert len(expected) == 1
    assert release_promotion.resolve_alembic_head() == expected[0]


def test_verify_uat_endpoint_validates_health_readiness_and_version_without_leaking_token():
    secret_token = "entra_uat_bearer_secret"
    seen_auth: list[str | None] = []

    class FakeResp:
        def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
            self.status = status
            self._body = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    responses = {
        "https://kitchen-uat.maki.maloline.fr/healthz": {"status": "ok"},
        "https://kitchen-uat.maki.maloline.fr/readyz": {
            "status": "ready",
            "database": "ok",
            "migrations": "current",
            "alembic_head": "0031_alias_household_source_type",
        },
        "https://kitchen-uat.maki.maloline.fr/api/version": {
            "version": "0.1.1.post58.dev0+a547222",
            "channel": "uat",
            "revision": SAMPLE_SHA,
            "alembic_head": "0031_alias_household_source_type",
        },
    }

    def fake_urlopen(request, timeout=15):
        seen_auth.append(request.headers.get("Authorization"))
        return FakeResp(responses[request.full_url])

    report = release_promotion.verify_uat_endpoint(
        "https://kitchen-uat.maki.maloline.fr/",
        expected_sha=SAMPLE_SHA,
        expected_alembic_head="0031_alias_household_source_type",
        bearer_token=secret_token,
        urlopen=fake_urlopen,
    )
    assert report["healthz"] == "ok"
    assert report["readyz"] == "ready"
    assert report["version_checked"] is True
    assert report["revision"] == SAMPLE_SHA
    assert secret_token not in json.dumps(report)
    assert seen_auth == [None, None, f"Bearer {secret_token}"]

    # Mismatched or missing revision must fail without leaking bearer token.
    for bad_rev in ("b" * 40, None, ""):
        responses["https://kitchen-uat.maki.maloline.fr/api/version"]["revision"] = bad_rev
        with pytest.raises(ValueError, match="revision") as exc_info:
            release_promotion.verify_uat_endpoint(
                "https://kitchen-uat.maki.maloline.fr",
                expected_sha=SAMPLE_SHA,
                expected_alembic_head="0031_alias_household_source_type",
                bearer_token=secret_token,
                urlopen=fake_urlopen,
            )
        assert secret_token not in str(exc_info.value)

    # Restore valid revision and check UAT vs production channel isolation.
    responses["https://kitchen-uat.maki.maloline.fr/api/version"]["revision"] = SAMPLE_SHA
    responses["https://kitchen-uat.maki.maloline.fr/api/version"]["channel"] = "beta"
    beta_report = release_promotion.verify_uat_endpoint(
        "https://kitchen-uat.maki.maloline.fr",
        expected_sha=SAMPLE_SHA,
        expected_alembic_head="0031_alias_household_source_type",
        bearer_token=secret_token,
        urlopen=fake_urlopen,
    )
    assert beta_report["channel"] == "beta"

    for non_uat_channel in ("production", "release", "local", None):
        responses["https://kitchen-uat.maki.maloline.fr/api/version"]["channel"] = non_uat_channel
        with pytest.raises(ValueError, match="isolated UAT channel") as exc_info:
            release_promotion.verify_uat_endpoint(
                "https://kitchen-uat.maki.maloline.fr",
                expected_sha=SAMPLE_SHA,
                expected_alembic_head="0031_alias_household_source_type",
                bearer_token=secret_token,
                urlopen=fake_urlopen,
            )
        assert secret_token not in str(exc_info.value)


def test_handle_promote_refuses_dry_run_and_records_rollback_previous_latest(monkeypatch, tmp_path):
    index_digest, index_bytes, manifests, blobs = _make_oci_index_fixtures()
    newer_bytes = b'{"newer": true}'
    newer_digest = _digest_of(newer_bytes)
    tag_store = {
        **manifests,
        "v0.1.5": (index_bytes, "application/vnd.oci.image.index.v1+json"),
        "release-v0.1.5": (index_bytes, "application/vnd.oci.image.index.v1+json"),
        f"release-sha-{SAMPLE_SHA}": (index_bytes, "application/vnd.oci.image.index.v1+json"),
        "latest": (newer_bytes, "application/vnd.oci.image.index.v1+json"),
    }

    class RollbackFakeGhcrClient:
        def __init__(self, repository: str, actor: str, token: str, scope: str = "pull") -> None:
            pass

        def get_manifest(self, reference: str) -> tuple[bytes, str]:
            return tag_store[reference]

        def get_blob(self, digest: str) -> bytes:
            return blobs[digest]

        def put_manifest(
            self, tag: str, manifest_bytes: bytes, content_type: str, expected_digest: str
        ) -> str:
            tag_store[tag] = (manifest_bytes, content_type)
            return expected_digest

    monkeypatch.setattr(release_promotion, "GhcrClient", RollbackFakeGhcrClient)
    monkeypatch.chdir(tmp_path)
    summary_file = tmp_path / "summary.md"

    env = {
        "GITHUB_REPOSITORY": "ghjklw/home-kitchen",
        "GITHUB_ACTOR": "ghjklw",
        "GITHUB_RUN_ID": "77777",
        "GITHUB_STEP_SUMMARY": str(summary_file),
        "GHCR_TOKEN": "ghs_test_token",
        "PUBLISH_RUN_ID": "98765",
        "PUBLISH_RUN_ATTEMPT": "1",
        "INPUT_RELEASE_TAG": "v0.1.5",
        "INPUT_SOURCE_SHA": SAMPLE_SHA,
        "INPUT_SOURCE_DIGEST": index_digest,
        "INPUT_UAT_HEALTH_VERIFIED": "true",
        "INPUT_UAT_MIGRATIONS_VERIFIED": "true",
        "INPUT_UAT_BACKUP_RECOVERY_VERIFIED": "true",
        "INPUT_UAT_CONSUMER_VERIFIED": "true",
        "INPUT_UAT_EVIDENCE_REF": "Rollback drill in issue #114",
        "INPUT_CONFIRM_PRODUCTION_PROMOTION": "PROMOTE TO PRODUCTION",
        "INPUT_PROMOTION_ACTION": "rollback",
        "INPUT_DRY_RUN": "true",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    with pytest.raises(ValueError, match="dry_run"):
        release_promotion.handle_promote()

    monkeypatch.setenv("INPUT_DRY_RUN", "false")
    assert release_promotion.handle_promote() == 0
    assert tag_store["latest"][0] == index_bytes

    record = json.loads((tmp_path / "promotion-metadata.json").read_text(encoding="utf-8"))
    assert record["promotion_action"] == "rollback"
    assert record["previous_latest_digest"] == newer_digest
    assert record["digest"] == index_digest
    assert record["alembic_head"] is not None


def test_handle_verify_dry_run_writes_verification_metadata_and_checks_uat(monkeypatch, tmp_path):
    index_digest, _, manifests, blobs = _make_oci_index_fixtures()

    class VerifyFakeGhcrClient:
        def __init__(self, repository: str, actor: str, token: str, scope: str = "pull") -> None:
            self.scope = scope

        def get_manifest(self, reference: str) -> tuple[bytes, str]:
            if reference in manifests:
                return manifests[reference]
            raise KeyError(reference)

        def get_blob(self, digest: str) -> bytes:
            return blobs[digest]

    monkeypatch.setattr(release_promotion, "GhcrClient", VerifyFakeGhcrClient)
    monkeypatch.setattr(release_promotion, "verify_git_and_ci", lambda repo, sha, tag: None)
    monkeypatch.setattr(
        release_promotion,
        "verify_publisher_run",
        lambda *args, **kwargs: {
            "source_sha": SAMPLE_SHA,
            "event": "main",
            "image": "ghcr.io/ghjklw/home-kitchen",
            "digest": index_digest,
            "version": None,
            "tag": "build-98765-1",
        },
    )
    monkeypatch.setattr(
        release_promotion,
        "verify_uat_endpoint",
        lambda url, **kwargs: {
            "url": url,
            "healthz": "ok",
            "readyz": "ready",
            "alembic_head": kwargs.get("expected_alembic_head"),
            "version_checked": False,
        },
    )
    monkeypatch.chdir(tmp_path)
    output_file = tmp_path / "github_output.txt"
    summary_file = tmp_path / "summary.md"

    for name, value in {
        "GITHUB_REPOSITORY": "ghjklw/home-kitchen",
        "GITHUB_ACTOR": "ghjklw",
        "GITHUB_OUTPUT": str(output_file),
        "GITHUB_STEP_SUMMARY": str(summary_file),
        "GHCR_TOKEN": "ghs_test_token",
        "INPUT_RELEASE_TAG": "v0.2.0",
        "INPUT_SOURCE_SHA": SAMPLE_SHA,
        "INPUT_SOURCE_DIGEST": index_digest,
        "INPUT_UAT_HEALTH_VERIFIED": "true",
        "INPUT_UAT_MIGRATIONS_VERIFIED": "true",
        "INPUT_UAT_BACKUP_RECOVERY_VERIFIED": "true",
        "INPUT_UAT_CONSUMER_VERIFIED": "true",
        "INPUT_UAT_EVIDENCE_REF": "Dry-run exercise in issue #114",
        "INPUT_CONFIRM_PRODUCTION_PROMOTION": "",
        "INPUT_PROMOTION_ACTION": "promote",
        "INPUT_UAT_URL": "https://kitchen-uat.maki.maloline.fr",
        "INPUT_DRY_RUN": "true",
    }.items():
        monkeypatch.setenv(name, value)

    assert release_promotion.handle_verify() == 0
    verification = json.loads((tmp_path / "verification-metadata.json").read_text(encoding="utf-8"))
    assert verification["dry_run"] is True
    assert verification["digest"] == index_digest
    assert verification["image_ref"] == f"ghcr.io/ghjklw/home-kitchen@{index_digest}"
    assert verification["uat_probe"]["readyz"] == "ready"
    summary_text = summary_file.read_text(encoding="utf-8")
    assert "Dry run complete" in summary_text
    assert "**UAT probe**: `https://kitchen-uat.maki.maloline.fr`" in summary_text
    assert f"source_digest={index_digest}" in output_file.read_text(encoding="utf-8")
