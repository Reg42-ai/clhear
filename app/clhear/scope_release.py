# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Publish a scoped corpus as one release that carries every built layer.

The release artifact is a SQLite snapshot: the L1 tables through the same
permission-checked compiler as every L1 release, plus the derived L2–L8 tables
and the layer build ledger. A layer is listed in ``layers`` only when it was
built from the current revisions of its inputs and its gate passed (I10).
Consumers read the release through ``/v1`` (``app_api``), which serves this
artifact rather than the reviewers' L1 viewer.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear import layer_builds, releases
from app.clhear.l1 import scopes

CURRENT_KEY = "current/snapshot.db"


def derived_tables() -> list:
    from app.clhear import derived_models as d
    from app.clhear.l7 import models as l7
    from app.clhear.platform import ids, record

    return [d.obligations, d.asserts, d.equivalences, d.supersessions, d.concepts, d.concept_members,
            d.blocks, d.requires, d.characteristics, d.l3_kinds,
            d.attribute_schema, d.license_types, d.licences, d.products_services, d.client_types, d.channels,
            d.profiles, d.permits, d.applies_to, d.validity_rules,
            d.activities, d.implies, d.operates, d.mitigates,
            d.blueprints, d.blueprint_items, d.minimality_proofs,
            l7.enforcement_events, l7.enforcement_links, l7.risk_scores, l7.risk_calibrations,
            l7.stated_penalties, l7.penalty_links,
            layer_builds.layer_builds, record.why_trails, ids.id_sequences]


def _copy_derived(engine: Engine, destination: Path) -> dict:
    from app.clhear.db import make_engine

    target = make_engine(f"sqlite:///{destination}")
    counts = {}
    projected = None
    if scopes.active():
        from app.clhear.scope_projection import project

        with engine.connect() as conn:
            projected = project(conn, scopes.active_name())
        by_name = {table.name: rows for table, rows in (projected or {"tables": []})["tables"]}
    try:
        with engine.connect() as source, target.begin() as out:
            for table in derived_tables():
                table.create(out, checkfirst=True)
                if scopes.active():
                    rows = by_name.get(table.name, [])
                else:
                    rows = [dict(r) for r in source.execute(sa.select(table)).mappings()]
                for start in range(0, len(rows), 500):
                    out.execute(table.insert(), rows[start:start + 500])
                counts[table.name] = len(rows)
    finally:
        target.dispose()
    return counts


def gates(engine: Engine, scope_name: str) -> dict:
    """Per-layer gate: built from current inputs, non-empty, and the scope proof checks pass."""
    from app.clhear.scope_proof import run as proof

    results = proof(engine, scope_name)
    out = {}
    with engine.connect() as conn:
        for layer in layer_builds.ORDER:
            build = layer_builds.latest(conn, layer, scope_name)
            current = layer_builds.revision(conn, layer)
            fresh = build is not None and build["revision"] == current
            checks = results["layers"].get(layer, {"passed": True, "checks": []})
            out[layer] = {"built": build is not None, "fresh": fresh, "proof": checks,
                          "passed": fresh and checks["passed"]}
    return {"layers": out, "proof": results}


def _s3_put(path: Path, bucket: str, key: str, digest: str) -> None:
    import boto3

    from app.clhear.settings import get_settings

    with path.open("rb") as body:
        boto3.client("s3", region_name=get_settings().aws_region).put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/vnd.sqlite3", ServerSideEncryption="AES256",
            Metadata={"sha256": digest, "kind": "scope-release"})


def publish(engine: Engine, scope_name: str, *, release_id: str | None = None) -> dict:
    from app.clhear.l1 import release_snapshot

    if scopes.active_name() != scope_name:
        raise RuntimeError("Publish a scope release from a database built for that scope")
    rid = release_id or f"clhear-v{datetime.now(timezone.utc).strftime('%Y.%m.%d.%H%M')}-{scope_name}"
    gate = gates(engine, scope_name)
    with tempfile.TemporaryDirectory(prefix="clhear-scope-release-") as directory:
        path = Path(directory) / "snapshot.db"
        projection = release_snapshot.compile_snapshot(engine, path, source_keys=scopes.source_keys(scope_name))
        derived = _copy_derived(engine, path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        parts = releases._s3_parts()
        uri = path.resolve().as_uri()
        if parts:
            bucket, prefix = parts
            uri = f"s3://{bucket}/{prefix}/{rid}/l1/snapshot.db"
            _s3_put(path, bucket, f"{prefix}/{rid}/l1/snapshot.db".lstrip("/"), digest)
            _s3_put(path, bucket, f"{prefix}/{CURRENT_KEY}".lstrip("/"), digest)
        snap_counts = projection["counts"]
        counts = {"families": snap_counts.get("source_families", 0), "sources": snap_counts.get("sources", 0),
                  "clauses": snap_counts.get("clauses", 0), "change_events": snap_counts.get("change_events", 0)}
        manifest = releases.build_manifest(release_id=rid, snapshot_uri=uri, content_hash=digest, counts=counts,
                                           engine=engine, previous=releases.get_latest(engine=None), contributions=[])
    layers = ["L0", *[layer for layer, g in gate["layers"].items() if g["passed"]]]
    l1_ok = gate["layers"]["L1"]["passed"]
    manifest.update(
        layers=layers, reserved_layers=[f"L{n}" for n in range(1, 9) if f"L{n}" not in layers],
        status="candidate", audience="accounts", scope={"name": scope_name, "label": scopes.get(scope_name)["label"],
                                                         "sources": list(scopes.source_keys(scope_name))},
        acceptance={"passed": l1_ok, "basis": "scope", "detail": gate["layers"]["L1"]["proof"]},
        projection={**projection, "derived_counts": derived}, gates={k: v["passed"] for k, v in gate["layers"].items()},
        proof=gate["proof"],
    )
    manifest["manifest_hash"] = releases._manifest_hash(manifest)
    parts = releases._s3_parts()
    if parts:
        bucket, prefix = parts
        releases._put_json_s3(bucket, f"{prefix}/{rid}/manifest.json".lstrip("/"), manifest)
        releases._put_json_s3(bucket, f"{prefix}/{releases.LATEST_NAME}".lstrip("/"),
                              {"id": rid, "manifest_uri": f"s3://{bucket}/{prefix}/{rid}/manifest.json"})
    else:
        root = releases._local_root() / rid
        root.mkdir(parents=True, exist_ok=True)
        releases._put_json_local(root / releases.MANIFEST_NAME, manifest)
        releases._put_json_local(releases._local_root() / releases.LATEST_NAME, {"id": rid})
    return {"id": rid, "layers": layers, "snapshot_uri": uri, "sha256": digest,
            "gates": manifest["gates"], "derived_counts": derived}


def summary(manifest: dict) -> str:
    return json.dumps({k: manifest.get(k) for k in ("id", "layers", "gates", "status")}, sort_keys=True)
