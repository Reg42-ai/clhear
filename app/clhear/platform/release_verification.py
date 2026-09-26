# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Strict artifact and signature verification used by the L0 release worker.

A declared S3 URI is not proof that snapshot bytes exist or match. Signer
identity in a manifest is not a trust anchor. Verification never edits files.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from app.clhear.platform.manifest import check_manifest, frozen_model_ids

TRUSTED_RELEASE_IDENTITY = "https://github.com/Reg42-ai/CLHEAR-MVP/.github/workflows/release.yml@refs/heads/main"
TRUSTED_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


def _digest(stream) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _sha256(path: Path) -> tuple[str, int]:
    with path.open("rb") as stream:
        return _digest(stream)


def _valid_hash(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _snapshot_hash(root: Path, uri: str) -> tuple[str, int]:
    local = root / "l1" / "snapshot.db"
    if local.is_file():
        return _sha256(local)
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/") or parsed.query or parsed.fragment:
        raise ValueError("snapshot file missing and no valid S3 object URI")
    import boto3
    from app.clhear.settings import get_settings

    response = boto3.client("s3", region_name=get_settings().aws_region).get_object(
        Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
    stream = response["Body"]
    try:
        return _digest(stream)
    finally:
        stream.close()


def verify(target: Path, *, require_signature: bool = False,
           cosign_identity: str | None = None) -> tuple[bool, list[str], dict]:
    """Inspect artifacts; promotion must pass require_signature=True.

    cosign_identity is retained for compatibility, but cannot override trust.
    """
    from app.clhear.releases import verify_manifest_hash

    target = Path(target)
    manifest_path = target / "manifest.json" if target.is_dir() else target
    root, problems = manifest_path.parent, []
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        return False, [f"manifest cannot be read: {exc}"], {}
    if not isinstance(manifest, dict):
        return False, ["manifest must be a JSON object"], {}
    if manifest.get("spec_version") != 2:
        problems.append(f"spec_version {manifest.get('spec_version')} != 2")
    if not verify_manifest_hash(manifest):
        problems.append("manifest_hash does not match manifest body")

    snapshot = (manifest.get("artifacts") or {}).get("snapshot") or {}
    declared = snapshot.get("sha256")
    if not _valid_hash(declared):
        problems.append("snapshot sha256 must be a nonempty 64-character hexadecimal digest")
    else:
        if (manifest.get("l1") or {}).get("content_hash") != declared:
            problems.append("snapshot sha256 differs from the L1 content_hash binding")
        try:
            actual, size = _snapshot_hash(root, str(snapshot.get("uri") or ""))
            if not size:
                problems.append("snapshot artifact is empty")
            if actual != declared:
                problems.append(f"snapshot sha256 mismatch: manifest={declared[:12]} actual={actual[:12]}")
        except Exception as exc:
            problems.append(f"snapshot bytes could not be verified: {exc}")

    model_manifest = manifest.get("model_manifest") or {}
    if not model_manifest:
        problems.append("model_manifest missing")
    else:
        problems.extend(f"model_manifest: {problem}" for problem in check_manifest(model_manifest))
    gates = manifest.get("gates") or {}
    for layer in manifest.get("layers") or []:
        if layer == "L0":
            continue
        gate = gates.get(layer)
        if gate is None:
            problems.append(f"{layer} published without a gate record")
        elif gate.get("unverified"):
            problems.append(f"{layer} published with an unverified gate (no suites have run)")
        elif gate.get("passed") is not True:
            problems.append(f"{layer} published below gate: failed={gate.get('failed')} missing={gate.get('missing')}")

    sbom_uri, sbom_hash = manifest.get("sbom_uri"), manifest.get("sbom_sha256")
    if require_signature or sbom_uri or sbom_hash:
        if sbom_uri != "sbom.spdx.json" or not _valid_hash(sbom_hash):
            problems.append("SBOM declaration requires sbom.spdx.json and its sha256")
        else:
            try:
                actual, size = _sha256(root / sbom_uri)
                if not size or actual != sbom_hash:
                    problems.append("SBOM bytes do not match the manifest hash")
            except OSError as exc:
                problems.append(f"SBOM bytes could not be verified: {exc}")

    signature = manifest.get("signature") or {}
    bundle = root / "manifest.sigstore.json"
    if cosign_identity is not None and cosign_identity != TRUSTED_RELEASE_IDENTITY:
        problems.append("signer identity override differs from the trusted release workflow")
    if signature.get("identity") and signature["identity"] != TRUSTED_RELEASE_IDENTITY:
        problems.append("manifest signer identity differs from the trusted release workflow")
    if require_signature or bundle.exists() or signature.get("signed"):
        if signature.get("identity") != TRUSTED_RELEASE_IDENTITY:
            problems.append("trusted signature identity declaration missing")
        if signature.get("scheme") != "sigstore-cosign-keyless" or signature.get("bundle_uri") != bundle.name:
            problems.append("signature declaration must name the detached Sigstore bundle")
        if not bundle.is_file():
            problems.append("signature bundle manifest.sigstore.json missing")
        elif shutil.which("cosign") is None:
            problems.append("cosign not installed; cannot verify signature")
        else:
            command = ["cosign", "verify-blob", str(manifest_path), "--bundle", str(bundle),
                       "--certificate-identity", TRUSTED_RELEASE_IDENTITY,
                       "--certificate-oidc-issuer", TRUSTED_OIDC_ISSUER]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    problems.append(f"cosign verify-blob failed: {result.stderr.strip()[:300]}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                problems.append(f"cosign verify-blob could not complete: {exc}")
    return not problems, problems, manifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("--require-signature", action="store_true")
    args = parser.parse_args(argv)
    ok, problems, manifest = verify(args.target, require_signature=args.require_signature)
    print(f"release {manifest.get('id')} layers={manifest.get('layers')}")
    for task_class, model_id in sorted(frozen_model_ids(manifest.get("model_manifest") or {}).items()):
        print(f"  {task_class:16s} {model_id}")
    for problem in problems:
        print("PROBLEM", problem)
    print("VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
