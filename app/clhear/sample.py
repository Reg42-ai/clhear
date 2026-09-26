# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Offline sample corpus for the quickstart.

The fake provider is constructed and called. The layers recorded here are a
placeholder obligation, block, and activity for the sources the host declared.
No regulation is fetched.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear import layer_builds
from app.clhear.derived_models import activities, blocks, characteristics, obligations, requires
from app.clhear.l1 import scopes, source_registry
from app.clhear.l4.validate import create_profile
from app.clhear.l6 import composer
from app.clhear.platform import record

_STATEMENT = "An organisation must keep a record of each decision and the reason for it."


def _why(layer: str, summary: str, subject: str) -> record.WhyTrail:
    return record.WhyTrail(
        layer=layer,
        reasoning_summary=summary,
        evidence_refs=[subject],
        inputs=(subject,),
        model_manifest={"model": "fake", "method": "offline-sample"},
        skill_version="sample",
        confidence=1.0,
        agent_id="clhear.sample",
        subject_ref=subject,
    )


def seed_rows(engine: Engine, source_keys: list[str]) -> dict:
    """Insert one obligation, block, and activity per declared source key."""
    from app.clhear import hoststore

    source_registry.install(hoststore.registry_entries(engine, source_keys))
    seeded = source_registry.seed(engine)
    written = []
    with engine.begin() as conn:
        for key in source_keys:
            clause = "clause-1"
            obligation_id = f"OBL:{key}#{clause}"
            block_id = "BLK-DECLARED-RECORD"
            activity_id = "ACT-DECLARED-KEEP"
            digest = hashlib.sha256(_STATEMENT.encode()).hexdigest()
            if conn.execute(obligations.select().where(obligations.c.id == obligation_id)).first() is None:
                record.write(conn, obligations, {
                    "id": obligation_id,
                    "source_key": key,
                    "clause_ref": clause,
                    "title": "Keep a record of the decision",
                    "statement": _STATEMENT,
                    "modality": "must",
                    "status": "derived",
                    "text_hash": digest,
                    "confidence": 0.9,
                    "method": "offline-sample",
                }, why=_why("L2", "placeholder obligation for an offline sample", obligation_id))
            if conn.execute(blocks.select().where(blocks.c.id == block_id)).first() is None:
                record.write(conn, blocks, {
                    "id": block_id,
                    "name": "Decision record",
                    "description": "A record of the decision and the reason for it.",
                    "capability": "record",
                    "purpose": "Keep the reason for a decision with the decision.",
                    "kind": "Process",
                    "status": "derived",
                    "satisfies": [{"source_key": key, "refs": [clause]}],
                    "evidence_artifacts": ["decision record"],
                }, why=_why("L3", "placeholder block for an offline sample", block_id))
            if conn.execute(characteristics.select().where(characteristics.c.block_id == block_id)).first() is None:
                record.write(conn, characteristics, {
                    "block_id": block_id,
                    "key": "retention",
                    "value": "kept with the decision",
                    "status": "backed",
                    "backing_obligation_id": obligation_id,
                    "backing_span": _STATEMENT,
                    "method": "offline-sample",
                }, why=_why("L3", "placeholder characteristic for an offline sample", block_id))
            edge_id = f"REQ:{key}"
            if conn.execute(requires.select().where(requires.c.id == edge_id)).first() is None:
                record.write(conn, requires, {
                    "id": edge_id,
                    "obligation_id": obligation_id,
                    "block_id": block_id,
                    "rationale": _STATEMENT,
                    "method": "offline-sample",
                    "obligation_text_hash": digest,
                }, why=_why("L3", "placeholder requires edge for an offline sample", edge_id))
            if conn.execute(activities.select().where(activities.c.id == activity_id)).first() is None:
                record.write(conn, activities, {
                    "id": activity_id,
                    "name": "Keep the decision record",
                    "description": "Record the decision and the reason for it.",
                    "triggers": [{"anchor": {"source_key": key, "refs": [clause]}, "when": {}}],
                    "status": "derived",
                    "side": "compliance",
                    "action_type": "record",
                }, why=_why("L5", "placeholder activity for an offline sample", activity_id))
            written.append(obligation_id)
    return {"sources": seeded, "obligations": written}


def record_layers(engine: Engine, scope_name: str, steps: dict) -> dict:
    """Record L1 through L8 after the sample rows are already stored."""
    report = {}
    for layer in layer_builds.ORDER:
        started = datetime.now(timezone.utc)
        with engine.connect() as conn:
            inputs = layer_builds.check_inputs(conn, layer, scope_name)
            if layer == "L8":
                from app.clhear.l8.reference import derived_reference_rows

                counts = {"reference_rows": len(derived_reference_rows(conn))}
            else:
                counts = {table.name: len(layer_builds._rows(conn, table)) for table in layer_builds._tables(layer)}
        built = layer_builds.record(
            engine, layer, scope=scope_name, inputs=inputs, counts=counts,
            steps=steps.get(layer, {"sample": True}), started_at=started,
        )
        report[layer] = {"revision": built["revision"], "counts": built["counts"]}
    return report


def derive(engine: Engine, llm, profiles: list[dict]) -> dict:
    """Write sample layers and compose one blueprint per host profile."""
    scope = scopes.active()
    if scope is None:
        raise RuntimeError(f"Set {scopes.SCOPE_ENV} before the offline sample")
    keys = list(scope.get("sources") or [])
    if not keys:
        raise RuntimeError("the scope names no sources")
    llm.run("dummy.triage", prompt="offline sample", max_tokens=32)
    seeded = seed_rows(engine, keys)
    stored = []
    blueprints = {}
    for profile in profiles:
        row = create_profile(
            engine, profile.get("attributes") or {}, name=profile.get("name") or "",
            source="api", allow_invalid=True,
        )
        composed = composer.compose_for_profile(engine, row["id"], requested_by="clhear.sample")
        host_id = profile.get("host_id") or row["id"]
        blueprints[host_id] = composed
        stored.append({"host_id": host_id, "engine_id": row["id"], "blueprint_id": composed.get("blueprint_id")})
    layers = record_layers(engine, scope["name"], {"L2": seeded, "L6": {"blueprints": [s["blueprint_id"] for s in stored]}})
    return {"profiles": stored, "blueprints": blueprints, "layers": layers, "failed_sources": []}
