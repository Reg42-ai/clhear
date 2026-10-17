# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Layer builds: each layer of a scoped corpus is built from the layers below it.

A build records the revision of every input layer it read and the revision of
what it produced. A layer may only build when each input's current revision is
the one its latest build produced, so nothing is derived from a half-updated
layer and the order L1 → L2 → … → L8 is enforced rather than assumed.
A revision is a digest of the layer's live rows, excluding bookkeeping
columns (timestamps, trail ids, version counters).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.models import Json, L0_SCHEMA, metadata

layer_builds = sa.Table(
    "layer_builds",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
    sa.Column("scope", sa.Text, nullable=False, default=""),
    sa.Column("layer", sa.Text, nullable=False, index=True),
    sa.Column("revision", sa.Text, nullable=False),
    sa.Column("inputs", Json, nullable=False, default=dict),  # {"L2": revision, ...}
    sa.Column("counts", Json, nullable=False, default=dict),
    sa.Column("steps", Json, nullable=False, default=dict),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    schema=L0_SCHEMA,
)

# The layers each layer reads. L7 reads L1 enforcement sources, L2 links and
# L3/L5 operational impact, never a blueprint.
INPUTS = {
    "L1": (),
    "L2": ("L1",),
    "L3": ("L2",),
    "L4": ("L1", "L2"),
    "L5": ("L2", "L3", "L4"),
    "L6": ("L2", "L3", "L4", "L5"),
    "L7": ("L1", "L2", "L3", "L5"),
    "L8": ("L1", "L3", "L7"),
}
ORDER = tuple(INPUTS)
# Views read built layers but no layer reads them: item priority lays L7
# obligation scores onto each current L6 blueprint's items.
VIEWS = {"L7 item priority": ("L6", "L7")}
_VOLATILE = {"created_at", "updated_at", "derived_at", "why_trail_id", "valid_from", "valid_to", "version", "ran_at",
             "retrieved_at", "last_used_at", "last_login_at", "heartbeat_at", "detected_at", "checked_at"}


class LayerOrderViolation(RuntimeError):
    """An input layer changed after its last build, or was never built."""


def _tables(layer: str) -> list:
    from app.clhear import derived_models as d
    from app.clhear.l1 import models as l1
    from app.clhear.l7 import models as l7

    return {
        "L1": [l1.sources, l1.source_versions, l1.clauses],
        "L2": [d.obligations],
        "L3": [d.blocks, d.requires, d.characteristics],
        # Profiles are what a host submits, not something L4 derives: a profile
        # saved during a build must not look like a changed input layer.
        "L4": [d.applies_to, d.licences, d.license_types],
        "L5": [d.activities, d.implies, d.operates, d.mitigates],
        "L6": [d.blueprints, d.blueprint_items],
        "L7": [l7.enforcement_events, l7.enforcement_links, l7.risk_scores, l7.stated_penalties, l7.penalty_links],
        "L8": [],
    }[layer]


def _rows(conn: Connection, table) -> list[dict]:
    query = sa.select(table)
    if "valid_to" in table.c:
        query = query.where(table.c.valid_to.is_(None))
    if table.name == "source_versions":
        query = query.where(table.c.status == "in_force")
    if table.name == "risk_scores":
        query = query.where(table.c.subject_kind == "obligation")
    cols = [c.name for c in table.c if c.name not in _VOLATILE]
    rows = [{c: row[c] for c in cols} for row in conn.execute(query).mappings()]
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def revision(conn: Connection, layer: str) -> str:
    digest = hashlib.sha256(layer.encode())
    if layer == "L8":
        from app.clhear.l8.reference import derived_reference_rows

        payload = derived_reference_rows(conn)
        digest.update(json.dumps(payload, sort_keys=True, default=str).encode())
    for table in _tables(layer):
        digest.update(table.name.encode())
        digest.update(json.dumps(_rows(conn, table), sort_keys=True, default=str).encode())
    return digest.hexdigest()


def latest(conn: Connection, layer: str, scope: str = "") -> dict | None:
    row = conn.execute(sa.select(layer_builds).where(layer_builds.c.layer == layer, layer_builds.c.scope == scope)
                       .order_by(layer_builds.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row else None


def check_inputs(conn: Connection, layer: str, scope: str = "") -> dict:
    """Current input revisions of a layer or view, provided each equals its latest build's output."""
    inputs = {}
    for name in INPUTS[layer] if layer in INPUTS else VIEWS[layer]:
        build = latest(conn, name, scope)
        current = revision(conn, name)
        if build is None:
            raise LayerOrderViolation(f"{layer} reads {name}, which has not been built")
        if build["revision"] != current:
            raise LayerOrderViolation(f"{layer} reads {name}, which changed after its last build")
        inputs[name] = current
    return inputs


def record(engine: Engine, layer: str, *, scope: str, inputs: dict, counts: dict, steps: dict,
           started_at: datetime) -> dict:
    with engine.begin() as conn:
        rev = revision(conn, layer)
        row = {"scope": scope, "layer": layer, "revision": rev, "inputs": inputs, "counts": counts,
               "steps": json.loads(json.dumps(steps, default=str)), "started_at": started_at,
               "finished_at": datetime.now(timezone.utc)}
        conn.execute(layer_builds.insert().values(**row))
    return row
