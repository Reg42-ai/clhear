# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Named CLHEAR releases for app consumers (HLD v2 §3 release pipeline, I6, I10).

A release is an immutable snapshot plus a manifest that declares which layers are
present. Versions are semantic dates (``2026.09.28``); daily deltas are recorded
between consecutive releases. A layer appears in ``layers`` only when its evals
gate passed (I10); the frozen model manifest (I6) and the Sigstore signature
fields travel with the manifest. ``scripts/verify_release.py`` checks all three.

Storage:
  s3://…/releases/latest.json
  s3://…/releases/{id}/manifest.json
  s3://…/releases/{id}/l1/snapshot.db

When CLHEAR_RELEASES_S3_PREFIX is empty, the same layout is written under
settings.clhear_artifacts_dir / "releases".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.clhear.layers import PUBLISHED_LAYERS
from app.clhear.settings import get_settings

MANIFEST_NAME = "manifest.json"
LATEST_NAME = "latest.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


RELEASE_ID_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2}|clhear-v[\w.\-]+)$")


def release_id_for(when: datetime | None = None) -> str:
    """Semantic-date version (HLD v2 §3): ``YYYY.MM.DD``."""
    return (when or _now()).strftime("%Y.%m.%d")


def is_release_id(value: str) -> bool:
    return bool(RELEASE_ID_RE.match(value or ""))


def _local_root() -> Path:
    settings = get_settings()
    root = Path(settings.clhear_artifacts_dir) / "releases"
    return root


def _s3_parts() -> tuple[str, str] | None:
    prefix = (get_settings().clhear_releases_s3_prefix or "").strip()
    if not prefix.startswith("s3://"):
        return None
    rest = prefix[len("s3://") :].rstrip("/")
    bucket, _, key = rest.partition("/")
    return bucket, key


def _put_json_local(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _get_json_local(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _s3():
    import boto3

    return boto3.client("s3", region_name=get_settings().aws_region)


def _put_json_s3(bucket: str, key: str, payload: dict) -> None:
    _s3().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def _get_json_s3(bucket: str, key: str) -> dict | None:
    try:
        body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    return json.loads(body)


def _latest_s3_etag(bucket: str, key: str) -> str | None:
    try:
        response = _s3().get_object(Bucket=bucket, Key=key)
        response["Body"].close()
        return response["ETag"]
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


def _immutable_s3_file(path: Path, bucket: str, key: str) -> None:
    """Conditional create prevents concurrent preparations from overwriting originals."""
    try:
        with path.open("rb") as content:
            _s3().put_object(Bucket=bucket, Key=key, Body=content, IfNoneMatch="*")
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
            raise
        stream = _s3().get_object(Bucket=bucket, Key=key)["Body"]
        try:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != _sha256_file(path):
                raise ValueError("Immutable release object already contains different bytes")
        finally:
            stream.close()


def _copy_snapshot_s3(src_uri: str, dest_bucket: str, dest_key: str) -> None:
    src_bucket, src_key = src_uri[len("s3://") :].split("/", 1)
    _s3().copy_object(
        Bucket=dest_bucket,
        Key=dest_key,
        CopySource={"Bucket": src_bucket, "Key": src_key},
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_reserved_prefixes(release_id: str, reserved: list[str] | None = None) -> None:
    """Keep l{n}/ prefixes present for layers not published in this release."""
    marker = {
        "layer_status": "not_published",
        "detail": "Not published in this release (below gate or not yet live).",
    }
    numbers = [int(l[1:]) for l in (reserved or [f"L{n}" for n in range(2, 9)])]
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        for n in numbers:
            key = f"{prefix}/{release_id}/l{n}/.reserved".lstrip("/")
            try:
                _s3().put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=json.dumps(marker).encode("utf-8"),
                    ContentType="application/json",
                )
            except Exception:
                break
        return
    root = _local_root()
    for n in numbers:
        path = root / release_id / f"l{n}" / ".reserved"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker, indent=2))


def corpus_counts(engine) -> dict[str, int]:
    import sqlalchemy as sa

    from app.clhear.l1.models import change_events, clauses, source_families, sources, source_versions, family_members
    from app.clhear.l1.origin import corpus_sources_predicate

    with engine.connect() as conn:
        eligible = sa.select(sources.c.id).where(corpus_sources_predicate())
        families = sa.or_(source_families.c.id.in_(sa.select(sources.c.family_id).where(sources.c.id.in_(eligible))),
                          source_families.c.id.in_(sa.select(family_members.c.family_id).where(family_members.c.source_id.in_(eligible))))
        def _count(table, condition) -> int:
            try:
                return int(conn.execute(sa.select(sa.func.count()).select_from(table).where(condition)).scalar() or 0)
            except Exception:
                return 0

        return {
            "families": _count(source_families, families),
            "sources": _count(sources, sources.c.id.in_(eligible)),
            "clauses": int(conn.execute(sa.select(sa.func.count()).select_from(clauses)
                .join(source_versions, clauses.c.source_version_id == source_versions.c.id)
                .where(source_versions.c.status == "in_force", source_versions.c.source_id.in_(eligible))).scalar() or 0),
            "change_events": _count(change_events, change_events.c.source_id.in_(eligible)),
        }


def _gate_layers(engine) -> tuple[list[str], list[str], dict]:
    """Layers that publish in this release (gate passed) vs reserved (I10)."""
    from app.clhear.layers import LAYER_CATALOG
    from app.clhear.platform import gates

    published: list[str] = ["L0"]
    reserved: list[str] = []
    statuses: dict = {}
    if engine is None:
        return ["L0"], [l for l in LAYER_CATALOG if l != "L0"], {}
    for layer, meta in LAYER_CATALOG.items():
        if layer == "L0":
            continue
        st = gates.gate_status(engine, layer)
        statuses[layer] = {"passed": st["passed"], "failed": st["failed"], "missing": st["missing"]}
        # A layer publishes when it is live in the catalog AND its gate passed.
        if meta["published"] and st["passed"]:
            published.append(layer)
        else:
            reserved.append(layer)
    return published, reserved, statuses


def build_manifest(
    *,
    release_id: str,
    snapshot_uri: str,
    content_hash: str,
    counts: dict[str, int],
    generated_at: datetime | None = None,
    engine=None,
    model_manifest: dict | None = None,
    previous: dict | None = None,
    contributions: list[dict] | None = None,
) -> dict[str, Any]:
    from app.clhear.platform.manifest import build_model_manifest

    when = generated_at or _now()
    published, reserved, gate_statuses = _gate_layers(engine)
    mm = model_manifest or _FROZEN_MODEL_MANIFEST or build_model_manifest(release_id=release_id)
    shipped = list(contributions or [])
    manifest: dict[str, Any] = {
        "id": release_id,
        "version": release_id,
        "generated_at": when.isoformat(),
        "spec_version": 2,
        "layers": published,
        "reserved_layers": reserved,
        "gates": gate_statuses,
        "model_manifest": mm,
        "l1": {
            "snapshot_uri": snapshot_uri,
            "content_hash": content_hash,
            "counts": counts,
        },
        "artifacts": {
            "snapshot": {"uri": snapshot_uri, "sha256": content_hash},
        },
        "signature": {
            # Filled by the release workflow (cosign sign-blob, keyless OIDC).
            "scheme": "sigstore-cosign-keyless",
            "bundle_uri": "",
            "signed": False,
            "identity": "",
        },
        "sbom_uri": "",
        "delta": _delta(previous, counts, published),
        "licence": {"data": "source-specific", "text_and_schemas": "source-specific", "code": "Apache-2.0"},
        # HLD v2 §6: every accepted community contribution ships with attribution and
        # the impact it had (blueprints / obligations changed).
        "contributions": {
            "count": len(shipped),
            "contributors": sorted({c["contributor"] for c in shipped}),
            "attribution": shipped,
        },
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return manifest


def _manifest_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def verify_manifest_hash(manifest: dict) -> bool:
    return manifest.get("manifest_hash") == _manifest_hash(manifest)


def _delta(previous: dict | None, counts: dict[str, int], layers: list[str]) -> dict:
    """Daily delta vs the previous release (counts and layer set)."""
    if not previous:
        return {"against": None, "counts": {}, "layers_added": [], "layers_removed": []}
    prev_counts = (previous.get("l1") or {}).get("counts") or {}
    prev_layers = set(previous.get("layers") or [])
    return {
        "against": previous.get("id"),
        "counts": {k: int(counts.get(k, 0)) - int(prev_counts.get(k, 0)) for k in set(counts) | set(prev_counts)},
        "layers_added": sorted(set(layers) - prev_layers),
        "layers_removed": sorted(prev_layers - set(layers)),
    }


def publish_release(
    engine,
    *,
    snapshot_path: str | None = None,
    snapshot_uri: str | None = None,
    release_id: str | None = None,
    sbom_path: str | None = None,
) -> dict:
    """Prepare an immutable private candidate; only verified promotion moves latest.

    Legacy snapshot arguments remain accepted but raw operational DB snapshots
    are never copied. L0 compiles the L1 table allowlist from its actual engine.
    """
    from app.clhear.l1 import inventory, release_snapshot
    from app.clhear.platform.release_verification import TRUSTED_RELEASE_IDENTITY

    rid = release_id or release_id_for()
    if not is_release_id(rid):
        raise ValueError("Invalid release id")
    root = _local_root() / rid
    if (root / MANIFEST_NAME).exists():
        return _get_json_local(root / MANIFEST_NAME)
    # Reserve this local candidate atomically. A crashed preparation requires a
    # new candidate ID; it must not overwrite an earlier snapshot or manifest.
    root.mkdir(parents=True, exist_ok=False)
    acceptance = inventory.acceptance_status(engine)
    previous = get_latest(engine=None)
    counts = corpus_counts(engine)
    content_hash, dest_uri = "", ""
    projection = None
    if acceptance.get("passed"):
        dest = root / "l1" / "snapshot.db"
        projection = release_snapshot.compile_snapshot(engine, dest)
        content_hash, dest_uri = _sha256_file(dest), dest.resolve().as_uri()
        if _s3_parts():
            bucket, prefix = _s3_parts()
            dest_uri = f"s3://{bucket}/{prefix}/{rid}/l1/snapshot.db"
        after = inventory.acceptance_status(engine)
        if not after.get("passed") or any(after.get(k) != acceptance.get(k) for k in ("inventory_hash", "bindings_hash")):
            raise RuntimeError("Inventory changed during snapshot preparation")
    manifest = build_manifest(
        release_id=rid, snapshot_uri=dest_uri, content_hash=content_hash, counts=counts, engine=engine, previous=previous,
        contributions=[],
    )
    manifest.update(status="candidate" if acceptance.get("passed") else "blocked", audience="restricted-reviewers",
                    acceptance=acceptance, projection=projection)
    # Downstream layers stay preview until independently accepted after L1.
    manifest["layers"] = ["L0", "L1"] if acceptance.get("passed") else ["L0"]
    manifest["reserved_layers"] = [f"L{n}" for n in range(1, 9) if f"L{n}" not in manifest["layers"]]
    manifest["signature"].update(bundle_uri="manifest.sigstore.json", identity=TRUSTED_RELEASE_IDENTITY,
                                 status="verification_required")
    if sbom_path:
        source_sbom = Path(sbom_path)
        shutil.copyfile(source_sbom, root / "sbom.spdx.json")
        manifest.update(sbom_uri="sbom.spdx.json", sbom_sha256=_sha256_file(root / "sbom.spdx.json"))
    manifest["manifest_hash"] = _manifest_hash(manifest)
    _put_json_local(root / MANIFEST_NAME, manifest)
    return manifest


@contextmanager
def _local_promotion_lock():
    import fcntl
    root = _local_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".promotion.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def promote_release(engine, release_id: str) -> dict:
    # A filesystem lock serializes local callers; S3 additionally compares the
    # previously observed pointer ETag across independent worker hosts.
    with _local_promotion_lock():
        return _promote_release(engine, release_id)


def _promote_release(engine, release_id: str) -> dict:
    """L0 promotes only final signed bytes that still match the audited corpus."""
    from app.clhear.l1 import inventory, release_snapshot
    from app.clhear.platform.release_verification import verify

    if not is_release_id(release_id):
        raise ValueError("Invalid release id")
    root = _local_root() / release_id
    s3 = _s3_parts()
    pointer_key = f"{s3[1]}/{LATEST_NAME}".lstrip("/") if s3 else None
    observed_etag = _latest_s3_etag(s3[0], pointer_key) if s3 else None
    ok, problems, manifest = verify(root, require_signature=True)
    if not ok:
        raise ValueError("Release verification failed: " + "; ".join(problems))
    if manifest.get("id") != release_id:
        raise ValueError("Signed manifest id differs from the requested release directory")
    if manifest.get("audience") != "restricted-reviewers":
        raise ValueError("This release worker promotes restricted review artifacts only")
    if s3:
        expected_uri = f"s3://{s3[0]}/{s3[1]}/{release_id}/l1/snapshot.db"
        if ((manifest.get("l1") or {}).get("snapshot_uri") != expected_uri
                or ((manifest.get("artifacts") or {}).get("snapshot") or {}).get("uri") != expected_uri):
            raise ValueError("Signed snapshot URI differs from the configured immutable destination")
    acceptance = inventory.acceptance_status(engine)
    frozen = manifest.get("acceptance") or {}
    if (manifest.get("status") != "candidate" or "L1" not in manifest.get("layers", [])
            or not acceptance.get("passed") or not frozen.get("passed")):
        raise ValueError("L1 inventory is not accepted; latest retained")
    if any(acceptance.get(k) != frozen.get(k) for k in ("inventory_hash", "bindings_hash")):
        raise ValueError("Audited corpus changed; latest retained")
    projection = manifest.get("projection") or {}
    expected = projection.get("bindings")
    if not expected or not release_snapshot.verify_snapshot_bindings(root / "l1" / "snapshot.db", expected):
        raise ValueError("Release snapshot does not match declared version bindings")
    with engine.connect() as conn:
        if release_snapshot.current_bindings(conn) != expected:
            raise ValueError("Current database differs from signed release snapshot")
    previous = get_latest()
    if previous and previous.get("generated_at", "") > manifest.get("generated_at", ""):
        raise ValueError("A newer accepted release exists; latest retained")
    receipt = {"id": release_id, "manifest_hash": manifest["manifest_hash"], "verified_signature": True,
               "promoted_at": _now().isoformat(), "audit_id": acceptance.get("audit_id"), "status": "accepted"}
    if s3:
        bucket, prefix = s3
        existing = _get_json_s3(bucket, f"{prefix}/{release_id}/{MANIFEST_NAME}".lstrip("/"))
        if existing and existing.get("manifest_hash") != manifest["manifest_hash"]:
            raise ValueError("Release id already names different immutable content")
        for relative in ("l1/snapshot.db", "manifest.sigstore.json", MANIFEST_NAME, "sbom.spdx.json"):
            path = root / relative
            if path.exists():
                _immutable_s3_file(path, bucket, f"{prefix}/{release_id}/{relative}".lstrip("/"))
        final_acceptance = inventory.acceptance_status(engine)
        if not final_acceptance.get("passed") or any(final_acceptance.get(k) != frozen.get(k) for k in ("inventory_hash", "bindings_hash")):
            raise ValueError("Inventory changed during upload; latest retained")
        condition = {"IfMatch": observed_etag} if observed_etag else {"IfNoneMatch": "*"}
        _s3().put_object(Bucket=bucket, Key=pointer_key,
                         Body=json.dumps(receipt, sort_keys=True).encode(), ContentType="application/json", **condition)
        # The successful conditional pointer write is the commit point. A
        # secondary receipt failure cannot undo that accepted publication or
        # cause the worker to falsely report that latest was retained.
        try:
            _put_json_s3(bucket, f"{prefix}/{release_id}/promotion.json".lstrip("/"), receipt)
        except Exception:
            logging.getLogger(__name__).exception("Release %s committed; remote receipt copy needs repair", release_id)
    else:
        _put_json_local(_local_root() / LATEST_NAME, receipt)
    try:
        _put_json_local(root / "promotion.json", receipt)
    except Exception:
        logging.getLogger(__name__).exception("Release %s committed; local receipt copy needs repair", release_id)
    return receipt


def get_promotion(release_id: str) -> dict | None:
    """Read acceptance without trusting a stale declared manifest hash.

    Latest contains the durable commit receipt. It also proves the current
    release's acceptance when an ancillary per-release receipt copy failed.
    """
    manifest = get_release(release_id)
    if not manifest or not verify_manifest_hash(manifest):
        return None
    release_id = manifest["id"]
    s3 = _s3_parts()
    receipt = (_get_json_s3(s3[0], f"{s3[1]}/{release_id}/promotion.json".lstrip("/")) if s3
               else _get_json_local(_local_root() / release_id / "promotion.json"))
    def matches(value):
        return (isinstance(value, dict) and value.get("id") == release_id
                and value.get("verified_signature") is True and value.get("status") == "accepted"
                and value.get("manifest_hash") == manifest["manifest_hash"])
    if matches(receipt):
        return receipt
    latest = (_get_json_s3(s3[0], f"{s3[1]}/{LATEST_NAME}".lstrip("/")) if s3
              else _get_json_local(_local_root() / LATEST_NAME))
    return latest if matches(latest) else None


def _ship_contributions(engine, release_id: str) -> list[dict]:
    """Accepted community contributions ship with this release (HLD v2 §6): status →
    released, impact computed, contributor notified. Tolerates an engine without the
    community schema (snapshot-only publishes)."""
    if engine is None:
        return []
    try:
        from app.clhear.platform import contributions

        return contributions.release_contributions(engine, release_id)
    except Exception as exc:  # pragma: no cover - publish must not fail on the ledger
        import logging

        logging.getLogger("clhear.releases").warning("contributions not shipped with %s: %s", release_id, exc)
        return []


def _live_manifest(engine) -> dict:
    return build_manifest(
        release_id="clhear-vLIVE",
        # A live preview has no immutable artifact. Database connection URLs
        # may contain credentials and must never become API metadata.
        snapshot_uri="",
        content_hash="",
        counts=corpus_counts(engine),
        engine=engine,
    )


def get_latest(engine=None) -> dict | None:
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        pointer = _get_json_s3(bucket, f"{prefix}/{LATEST_NAME}".lstrip("/"))
        if not pointer:
            return _live_manifest(engine) if engine is not None else None
        mid = pointer.get("id")
        return get_release(mid, engine=engine) if mid else None
    pointer = _get_json_local(_local_root() / LATEST_NAME)
    if pointer and pointer.get("id"):
        return get_release(pointer["id"], engine=engine)
    if engine is not None:
        return _live_manifest(engine)
    return None


def get_release(release_id: str, engine=None) -> dict | None:
    if release_id in {"latest", "clhear-vLATEST"}:
        return get_latest(engine)
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        return _get_json_s3(bucket, f"{prefix}/{release_id}/{MANIFEST_NAME}".lstrip("/"))
    return _get_json_local(_local_root() / release_id / MANIFEST_NAME) or (
        _live_manifest(engine) if engine is not None and release_id == "clhear-vLIVE" else None
    )


def list_releases(engine=None) -> list[dict]:
    found: list[dict] = []
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        resp = _s3().list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/".lstrip("/"), Delimiter="/")
        for cp in resp.get("CommonPrefixes") or []:
            rid = cp.get("Prefix", "").rstrip("/").split("/")[-1]
            if is_release_id(rid):
                man = get_release(rid, engine=engine)
                if man:
                    found.append(man)
    else:
        root = _local_root()
        for child in sorted(root.iterdir()) if root.exists() else []:
            if child.is_dir() and is_release_id(child.name):
                man = get_release(child.name, engine=engine)
                if man:
                    found.append(man)
    if not found and engine is not None:
        found.append(_live_manifest(engine))
    found.sort(key=lambda m: m.get("generated_at") or "", reverse=True)
    return found


def pin_release(release_id: str, engine=None) -> dict:
    man = get_release(release_id, engine=engine)
    if not man:
        raise ValueError(f"release {release_id} not found")
    pointer = {"id": release_id, "pinned": True}
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        _put_json_s3(bucket, f"{prefix}/pinned.json".lstrip("/"), pointer)
    else:
        _put_json_local(_local_root() / "pinned.json", pointer)
    man = dict(man)
    man["pinned"] = True
    return man


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m app.clhear.releases publish [--release ID] [--model-manifest path]"""
    import argparse
    import sys

    from app.clhear.db import get_engine, run_migrations

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["publish", "latest", "list"])
    parser.add_argument("--release", default=None)
    parser.add_argument("--model-manifest", default=None)
    parser.add_argument("--snapshot", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    engine = get_engine()
    run_migrations(engine)
    if args.command == "publish":
        mm = json.loads(Path(args.model_manifest).read_text()) if args.model_manifest else None
        if mm is not None:
            # publish_release builds the manifest itself; inject the frozen model manifest.
            global _FROZEN_MODEL_MANIFEST
            _FROZEN_MODEL_MANIFEST = mm
        manifest = publish_release(engine, snapshot_path=args.snapshot, release_id=args.release)
        print(json.dumps(manifest, indent=2, default=str))
        return 0
    if args.command == "latest":
        print(json.dumps(get_latest(engine), indent=2, default=str))
        return 0
    print(json.dumps(list_releases(engine), indent=2, default=str))
    return 0


_FROZEN_MODEL_MANIFEST: dict | None = None


if __name__ == "__main__":
    raise SystemExit(main())
