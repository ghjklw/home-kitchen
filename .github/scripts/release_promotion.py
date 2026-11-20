"""Verify and promote an immutable Home Kitchen OCI image digest from UAT to production."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_TAG_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$")
STABLE_SEMVER_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+$")
DIGITS_RE = re.compile(r"^[0-9]+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

CONFIRMATION_PHRASE = "PROMOTE TO PRODUCTION"
MANIFEST_ACCEPT_HEADER = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


@dataclasses.dataclass(frozen=True)
class PromotionRequest:
    repository: str
    release_tag: str
    source_sha: str
    source_digest: str
    uat_health_verified: bool
    uat_migrations_verified: bool
    uat_backup_recovery_verified: bool
    uat_consumer_verified: bool
    uat_evidence_ref: str
    confirm_production_promotion: str


@dataclasses.dataclass(frozen=True)
class OciImageIdentity:
    digest: str
    source: str
    revision: str
    run_id: str
    run_attempt: str
    manifest_bytes: bytes
    content_type: str


def _parse_required_true(value: str | bool | None, field_name: str) -> bool:
    if isinstance(value, bool):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            parsed = True
        elif normalized == "false":
            parsed = False
        else:
            raise ValueError(f"{field_name} must be 'true' or 'false'")
    else:
        raise ValueError(f"{field_name} must be 'true'")
    if not parsed:
        raise ValueError(f"{field_name} must be explicitly verified (true) before promotion")
    return True


def validate_promotion_inputs(
    *,
    repository: str,
    release_tag: str,
    source_sha: str,
    source_digest: str,
    uat_health_verified: str | bool,
    uat_migrations_verified: str | bool,
    uat_backup_recovery_verified: str | bool,
    uat_consumer_verified: str | bool,
    uat_evidence_ref: str,
    confirm_production_promotion: str,
) -> PromotionRequest:
    """Validate release inputs, UAT verification gates, and explicit production confirmation."""
    repo = repository.strip()
    if not REPO_RE.fullmatch(repo):
        raise ValueError(f"Invalid repository identifier: {repository!r}")

    tag = release_tag.strip()
    if not RELEASE_TAG_RE.fullmatch(tag):
        raise ValueError(f"Invalid release tag {release_tag!r}; expected semver tag such as v0.2.0")

    sha = source_sha.strip()
    if not SHA40_RE.fullmatch(sha):
        raise ValueError(
            f"Invalid source_sha {source_sha!r}; expected 40-character lowercase hex SHA"
        )

    digest = source_digest.strip()
    if not SHA256_DIGEST_RE.fullmatch(digest):
        raise ValueError(
            f"Invalid source_digest {source_digest!r}; expected immutable sha256:<64-hex> digest"
        )

    health_ok = _parse_required_true(uat_health_verified, "uat_health_verified")
    migrations_ok = _parse_required_true(uat_migrations_verified, "uat_migrations_verified")
    backup_ok = _parse_required_true(uat_backup_recovery_verified, "uat_backup_recovery_verified")
    consumer_ok = _parse_required_true(uat_consumer_verified, "uat_consumer_verified")

    evidence = " ".join(uat_evidence_ref.split())
    if len(evidence) < 3:
        raise ValueError("uat_evidence_ref must reference UAT verification evidence")

    confirmation = confirm_production_promotion.strip()
    if confirmation != CONFIRMATION_PHRASE:
        raise ValueError(
            f"confirm_production_promotion must equal {CONFIRMATION_PHRASE!r} for cutover"
        )

    return PromotionRequest(
        repository=repo,
        release_tag=tag,
        source_sha=sha,
        source_digest=digest,
        uat_health_verified=health_ok,
        uat_migrations_verified=migrations_ok,
        uat_backup_recovery_verified=backup_ok,
        uat_consumer_verified=consumer_ok,
        uat_evidence_ref=evidence,
        confirm_production_promotion=confirmation,
    )


def compute_release_tags(release_tag: str, source_sha: str) -> list[str]:
    """Compute immutable-digest target tags; move :latest only for stable semver tags."""
    if not RELEASE_TAG_RE.fullmatch(release_tag):
        raise ValueError(f"Invalid release tag: {release_tag!r}")
    if not SHA40_RE.fullmatch(source_sha):
        raise ValueError(f"Invalid source_sha: {source_sha!r}")

    tags = [
        release_tag,
        f"release-{release_tag}",
        f"release-sha-{source_sha}",
    ]
    if STABLE_SEMVER_RE.fullmatch(release_tag):
        tags.append("latest")
    return list(dict.fromkeys(tags))


def verify_sha256_digest(payload: bytes, expected_digest: str, label: str) -> None:
    if not SHA256_DIGEST_RE.fullmatch(expected_digest):
        raise ValueError(f"{label} digest is not a valid sha256 digest: {expected_digest!r}")
    actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual != expected_digest:
        raise ValueError(f"{label} digest mismatch: expected {expected_digest}, computed {actual}")


def validate_publish_metadata(
    metadata: dict[str, Any],
    *,
    repository: str,
    source_sha: str,
    source_digest: str,
    run_id: str,
    run_attempt: str,
) -> None:
    """Validate the image-metadata.json contract uploaded by .github/workflows/publish.yml."""
    expected_image = f"ghcr.io/{repository.lower()}"
    expected_tag = f"build-{run_id}-{run_attempt}"
    if metadata.get("source_sha") != source_sha:
        raise ValueError(
            f"image-metadata.json source_sha {metadata.get('source_sha')!r} != {source_sha!r}"
        )
    if metadata.get("event") != "main":
        raise ValueError(f"image-metadata.json event {metadata.get('event')!r} != 'main'")
    if str(metadata.get("image", "")).lower() != expected_image:
        raise ValueError(
            f"image-metadata.json image {metadata.get('image')!r} != {expected_image!r}"
        )
    if metadata.get("digest") != source_digest:
        raise ValueError(
            f"image-metadata.json digest {metadata.get('digest')!r} != {source_digest!r}"
        )
    if metadata.get("tag") != expected_tag:
        raise ValueError(f"image-metadata.json tag {metadata.get('tag')!r} != {expected_tag!r}")


def _is_runnable_platform_entry(entry: dict[str, Any]) -> bool:
    annotations = entry.get("annotations") or {}
    if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
        return False
    platform = entry.get("platform") or {}
    os_name = platform.get("os")
    return bool(os_name and os_name != "unknown")


def extract_oci_identity(
    manifest_bytes: bytes,
    content_type: str,
    *,
    expected_digest: str,
    expected_repository: str,
    expected_sha: str,
    fetch_manifest: Callable[[str], tuple[bytes, str]],
    fetch_blob: Callable[[str], bytes],
) -> OciImageIdentity:
    """Verify manifest bytes match expected_digest and all runnable configs carry source SHA/run."""
    verify_sha256_digest(manifest_bytes, expected_digest, "Root manifest")
    manifest_doc = json.loads(manifest_bytes.decode("utf-8"))

    platform_manifests: list[dict[str, Any]] = []
    if isinstance(manifest_doc.get("manifests"), list):
        runnable = [
            entry for entry in manifest_doc["manifests"] if _is_runnable_platform_entry(entry)
        ]
        if not runnable:
            raise ValueError("OCI image index contains no runnable platform manifests")
        for entry in runnable:
            child_digest = entry.get("digest", "")
            child_bytes, _ = fetch_manifest(child_digest)
            verify_sha256_digest(child_bytes, child_digest, f"Platform manifest {child_digest}")
            platform_manifests.append(json.loads(child_bytes.decode("utf-8")))
    else:
        platform_manifests.append(manifest_doc)

    expected_source = f"https://github.com/{expected_repository}"
    run_id: str | None = None
    run_attempt: str | None = None

    for manifest in platform_manifests:
        config_digest = (manifest.get("config") or {}).get("digest", "")
        config_bytes = fetch_blob(config_digest)
        verify_sha256_digest(config_bytes, config_digest, f"Image config {config_digest}")
        config_doc = json.loads(config_bytes.decode("utf-8"))
        labels = (config_doc.get("config") or {}).get("Labels") or {}

        source_label = labels.get("org.opencontainers.image.source")
        if source_label != expected_source:
            raise ValueError(
                f"OCI label org.opencontainers.image.source {source_label!r} != {expected_source!r}"
            )
        revision_label = labels.get("org.opencontainers.image.revision")
        if revision_label != expected_sha:
            raise ValueError(
                f"OCI label org.opencontainers.image.revision {revision_label!r} != "
                f"{expected_sha!r}"
            )
        label_run_id = str(labels.get("io.home-kitchen.run-id", ""))
        label_run_attempt = str(labels.get("io.home-kitchen.run-attempt", ""))
        if not DIGITS_RE.fullmatch(label_run_id) or not DIGITS_RE.fullmatch(label_run_attempt):
            raise ValueError(
                "Image config is missing numeric io.home-kitchen.run-id / run-attempt labels"
            )
        if run_id is None:
            run_id = label_run_id
            run_attempt = label_run_attempt
        elif (run_id, run_attempt) != (label_run_id, label_run_attempt):
            raise ValueError("Multi-arch platform manifests disagree on Home Kitchen run labels")

    assert run_id is not None and run_attempt is not None
    return OciImageIdentity(
        digest=expected_digest,
        source=expected_source,
        revision=expected_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        manifest_bytes=manifest_bytes,
        content_type=content_type,
    )


class GhcrClient:
    """Minimal OCI Distribution v2 client for GHCR digest verification and immutable tagging."""

    def __init__(self, repository: str, actor: str, token: str, scope: str = "pull") -> None:
        self.repository = repository.lower()
        self._actor = actor
        self._token = token
        self._scope = scope
        self._bearer_token: str | None = None

    def _authenticate(self) -> str:
        if self._bearer_token is not None:
            return self._bearer_token
        query = urllib.parse.urlencode(
            {
                "service": "ghcr.io",
                "scope": f"repository:{self.repository}:{self._scope}",
            }
        )
        basic = base64.b64encode(f"{self._actor}:{self._token}".encode()).decode("ascii")
        request = urllib.request.Request(
            f"https://ghcr.io/token?{query}",
            headers={"Authorization": f"Basic {basic}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GHCR token exchange failed with HTTP {exc.code}") from None
        bearer = payload.get("token")
        if not isinstance(bearer, str) or not bearer:
            raise RuntimeError("GHCR token exchange returned an empty token")
        self._bearer_token = bearer
        return bearer

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        bearer = self._authenticate()
        req_headers = {"Authorization": f"Bearer {bearer}"}
        if headers:
            req_headers.update(headers)
        request = urllib.request.Request(
            f"https://ghcr.io{path}",
            headers=req_headers,
            data=data,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                return response.status, resp_headers, response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GHCR {method} {path} failed with HTTP {exc.code}: {exc.reason}"
            ) from None

    def get_manifest(self, reference: str) -> tuple[bytes, str]:
        _, headers, body = self._request(
            "GET",
            f"/v2/{self.repository}/manifests/{urllib.parse.quote(reference, safe=':')}",
            headers={"Accept": MANIFEST_ACCEPT_HEADER},
        )
        content_type = headers.get("content-type", "application/vnd.oci.image.manifest.v1+json")
        header_digest = headers.get("docker-content-digest")
        if header_digest and reference.startswith("sha256:") and header_digest != reference:
            raise ValueError(
                f"GHCR Docker-Content-Digest {header_digest!r} != requested {reference!r}"
            )
        return body, content_type

    def get_blob(self, digest: str) -> bytes:
        _, _, body = self._request(
            "GET",
            f"/v2/{self.repository}/blobs/{urllib.parse.quote(digest, safe=':')}",
        )
        return body

    def put_manifest(
        self, tag: str, manifest_bytes: bytes, content_type: str, expected_digest: str
    ) -> str:
        _, headers, _ = self._request(
            "PUT",
            f"/v2/{self.repository}/manifests/{urllib.parse.quote(tag, safe='')}",
            headers={"Content-Type": content_type},
            data=manifest_bytes,
        )
        returned_digest = headers.get("docker-content-digest")
        if returned_digest and returned_digest != expected_digest:
            raise ValueError(
                f"Tag {tag!r} returned digest {returned_digest!r} != {expected_digest!r}"
            )
        verified_bytes, _ = self.get_manifest(tag)
        verify_sha256_digest(verified_bytes, expected_digest, f"Promoted tag {tag!r}")
        return expected_digest


def _run_command(cmd: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Command {' '.join(cmd)} failed ({result.returncode}): {stderr}")
    return result.stdout.strip()


def verify_git_and_ci(repository: str, source_sha: str, release_tag: str) -> None:
    _run_command(["git", "fetch", "origin", "main", "--tags", "--quiet"])
    _run_command(["git", "merge-base", "--is-ancestor", source_sha, "origin/main"])

    tag_check = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{release_tag}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if tag_check.returncode == 0:
        tag_sha = tag_check.stdout.strip()
        if tag_sha != source_sha:
            raise ValueError(
                f"Existing tag {release_tag!r} points to {tag_sha!r}, not source_sha {source_sha!r}"
            )

    ci_runs_json = _run_command(
        [
            "gh",
            "api",
            f"repos/{repository}/actions/workflows/ci.yml/runs"
            f"?head_sha={source_sha}&status=completed",
        ]
    )
    ci_runs = json.loads(ci_runs_json).get("workflow_runs", [])
    successful_main_ci = [
        run
        for run in ci_runs
        if run.get("conclusion") == "success" and run.get("head_branch") == "main"
    ]
    if not successful_main_ci:
        raise ValueError(f"No successful Kitchen CI run found on main for {source_sha}")


def verify_publisher_run(
    repository: str,
    *,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    source_digest: str,
) -> dict[str, Any]:
    run_json = _run_command(["gh", "api", f"repos/{repository}/actions/runs/{run_id}"])
    run = json.loads(run_json)
    if run.get("path") != ".github/workflows/publish.yml":
        raise ValueError(
            f"Run {run_id} belongs to {run.get('path')!r}, expected '.github/workflows/publish.yml'"
        )
    if run.get("conclusion") != "success":
        raise ValueError(f"Publish run {run_id} conclusion is {run.get('conclusion')!r}")
    if run.get("head_branch") != "main":
        raise ValueError(f"Publish run {run_id} head_branch is {run.get('head_branch')!r}")
    if (run.get("repository") or {}).get("full_name") != repository:
        raise ValueError(f"Publish run {run_id} repository does not match {repository!r}")

    with tempfile.TemporaryDirectory() as tmpdir:
        _run_command(
            [
                "gh",
                "run",
                "download",
                run_id,
                "--repo",
                repository,
                "--name",
                "image-metadata",
                "--dir",
                tmpdir,
            ]
        )
        metadata_path = pathlib.Path(tmpdir) / "image-metadata.json"
        if not metadata_path.is_file():
            raise ValueError(f"Publish run {run_id} did not provide image-metadata.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    validate_publish_metadata(
        metadata,
        repository=repository,
        source_sha=source_sha,
        source_digest=source_digest,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    return metadata


def _request_from_env() -> PromotionRequest:
    return validate_promotion_inputs(
        repository=os.environ["GITHUB_REPOSITORY"],
        release_tag=os.environ["INPUT_RELEASE_TAG"],
        source_sha=os.environ["INPUT_SOURCE_SHA"],
        source_digest=os.environ["INPUT_SOURCE_DIGEST"],
        uat_health_verified=os.environ["INPUT_UAT_HEALTH_VERIFIED"],
        uat_migrations_verified=os.environ["INPUT_UAT_MIGRATIONS_VERIFIED"],
        uat_backup_recovery_verified=os.environ["INPUT_UAT_BACKUP_RECOVERY_VERIFIED"],
        uat_consumer_verified=os.environ["INPUT_UAT_CONSUMER_VERIFIED"],
        uat_evidence_ref=os.environ["INPUT_UAT_EVIDENCE_REF"],
        confirm_production_promotion=os.environ["INPUT_CONFIRM_PRODUCTION_PROMOTION"],
    )


def _write_github_output(outputs: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            if "\n" in value:
                raise ValueError(f"Refusing multiline GitHub output for {key}")
            handle.write(f"{key}={value}\n")


def _append_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(markdown)
        if not markdown.endswith("\n"):
            handle.write("\n")


def handle_verify() -> int:
    req = _request_from_env()
    verify_git_and_ci(req.repository, req.source_sha, req.release_tag)

    ghcr = GhcrClient(
        repository=req.repository,
        actor=os.environ["GITHUB_ACTOR"],
        token=os.environ["GHCR_TOKEN"],
        scope="pull",
    )
    manifest_bytes, content_type = ghcr.get_manifest(req.source_digest)
    identity = extract_oci_identity(
        manifest_bytes,
        content_type,
        expected_digest=req.source_digest,
        expected_repository=req.repository,
        expected_sha=req.source_sha,
        fetch_manifest=ghcr.get_manifest,
        fetch_blob=ghcr.get_blob,
    )
    metadata = verify_publisher_run(
        req.repository,
        run_id=identity.run_id,
        run_attempt=identity.run_attempt,
        source_sha=req.source_sha,
        source_digest=req.source_digest,
    )
    planned_tags = compute_release_tags(req.release_tag, req.source_sha)

    _write_github_output(
        {
            "source_sha": req.source_sha,
            "source_digest": req.source_digest,
            "release_tag": req.release_tag,
            "source_build_tag": str(metadata["tag"]),
            "publish_run_id": identity.run_id,
            "publish_run_attempt": identity.run_attempt,
            "uat_evidence_ref": req.uat_evidence_ref,
        }
    )
    _append_summary(
        "\n".join(
            [
                "### UAT Candidate Verification Passed",
                f"- **Release tag**: `{req.release_tag}`",
                f"- **Source SHA**: `{req.source_sha}`",
                f"- **Immutable digest**: `{req.source_digest}`",
                f"- **Publish run**: `{identity.run_id}` (attempt `{identity.run_attempt}`, "
                f"tag `{metadata['tag']}`)",
                f"- **UAT evidence**: {req.uat_evidence_ref}",
                f"- **Planned production tags**: {', '.join(f'`{t}`' for t in planned_tags)}",
                "- **Next gate**: Explicit human approval required in `production` environment.",
            ]
        )
    )
    return 0


def handle_promote() -> int:
    req = _request_from_env()
    expected_run_id = os.environ["PUBLISH_RUN_ID"].strip()
    expected_run_attempt = os.environ["PUBLISH_RUN_ATTEMPT"].strip()

    ghcr = GhcrClient(
        repository=req.repository,
        actor=os.environ["GITHUB_ACTOR"],
        token=os.environ["GHCR_TOKEN"],
        scope="pull,push",
    )
    manifest_bytes, content_type = ghcr.get_manifest(req.source_digest)
    identity = extract_oci_identity(
        manifest_bytes,
        content_type,
        expected_digest=req.source_digest,
        expected_repository=req.repository,
        expected_sha=req.source_sha,
        fetch_manifest=ghcr.get_manifest,
        fetch_blob=ghcr.get_blob,
    )
    if identity.run_id != expected_run_id or identity.run_attempt != expected_run_attempt:
        raise ValueError(
            "Image run labels changed between verify and promote; aborting without tagging"
        )

    promoted_tags = compute_release_tags(req.release_tag, req.source_sha)
    for tag in promoted_tags:
        ghcr.put_manifest(tag, identity.manifest_bytes, identity.content_type, req.source_digest)

    promotion_record = {
        "image": f"ghcr.io/{req.repository.lower()}",
        "release_tag": req.release_tag,
        "source_sha": req.source_sha,
        "digest": req.source_digest,
        "source_build_tag": f"build-{identity.run_id}-{identity.run_attempt}",
        "publish_run_id": identity.run_id,
        "publish_run_attempt": identity.run_attempt,
        "promoted_tags": promoted_tags,
        "environment": "production",
        "requested_by": os.environ["GITHUB_ACTOR"],
        "promotion_run_id": os.environ["GITHUB_RUN_ID"],
        "promoted_at": dt.datetime.now(dt.UTC).isoformat(),
        "uat_verification": {
            "health_verified": req.uat_health_verified,
            "migrations_verified": req.uat_migrations_verified,
            "backup_recovery_verified": req.uat_backup_recovery_verified,
            "consumer_verified": req.uat_consumer_verified,
            "evidence_ref": req.uat_evidence_ref,
        },
        "rollout_and_recovery": {
            "immutable_digest_enforced": True,
            "environment_isolation": "separate UAT and production databases/credentials",
            "migration_compatibility": "forward-compatible Alembic upgrade verified in UAT",
            "rollback_policy": "re-promote prior verified source_digest; restore backup if needed",
        },
    }
    pathlib.Path("promotion-metadata.json").write_text(
        json.dumps(promotion_record, indent=2) + "\n", encoding="utf-8"
    )
    _append_summary(
        "\n".join(
            [
                "### Production Image Promotion Completed",
                f"- **Image**: `ghcr.io/{req.repository.lower()}@{req.source_digest}`",
                f"- **Source SHA**: `{req.source_sha}`",
                f"- **Promoted tags**: {', '.join(f'`{t}`' for t in promoted_tags)}",
                f"- **UAT evidence**: {req.uat_evidence_ref}",
                "- **Rollback**: Re-promote the previous verified `source_digest` via this workflow "
                "(and restore DB backup if schema changes are not backward-compatible).",
            ]
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Promote immutable Home Kitchen release images.")
    parser.add_argument("command", choices=["verify", "promote"])
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            return handle_verify()
        return handle_promote()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
