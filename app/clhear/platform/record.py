# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The single write path for layer data (HLD v2 §3 shared schema, I1–I4, I10).

Every layer table inherits :data:`SHARED_COLUMNS`. Rows are written only through
:func:`write`, which refuses a row without a why-trail (I3 — "no trail, no write"),
stamps version / derivation metadata, and enforces that the producing layer only
cites inputs from lower layers (I1). Nothing is ever deleted: :func:`invalidate`
closes ``valid_to`` and bumps ``version`` (I2).

Ledger tables (events, llm_calls, runs, eval_runs, proposals, …) are append-only
audit records, not nodes or edges, and are exempt from the shared schema. Projection
tables (search index, document tree derived from the WORM original) may be rebuilt
via :func:`rebuild_projection` because they are reproducible from the record store
and object store (I7) — the version node itself is never deleted.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.models import L0_SCHEMA, metadata
from app.clhear.platform.shared_schema import (  # noqa: F401  (re-exported)
    SHARED_COLUMN_NAMES,
    Json,
    attach_shared_columns,
    ensure_shared_columns,
    shared_columns,
)

LAYER_ORDER = ("L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8")

# Per-layer confidence below which a determination goes to the approval console (I4).
LOW_CONFIDENCE_THRESHOLDS: dict[str, float] = {
    "L1": 0.90,
    "L2": 0.85,
    "L3": 0.80,
    "L4": 0.85,
    "L5": 0.80,
    "L6": 0.85,
    "L7": 0.75,
    "L8": 0.70,
}


class WhyTrailRequired(ValueError):
    """Raised when a layer row is written without a why-trail (I3)."""


class LayerOrderViolation(ValueError):
    """Raised when a layer cites an input from the same or a higher layer (I1)."""


class DeletionForbidden(RuntimeError):
    """Raised when code attempts to delete a node or edge (I2)."""


why_trails = sa.Table(
    "why_trails",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False, default=""),
    sa.Column("reasoning_summary", sa.Text, nullable=False, default=""),
    sa.Column("evidence_refs", Json, nullable=False, default=list),
    sa.Column("inputs_hash", sa.Text, nullable=False, default=""),
    sa.Column("model_manifest", Json, nullable=False, default=dict),
    sa.Column("skill_version", sa.Text, nullable=False, default=""),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
    sa.Column("evals_run_id", sa.Text, nullable=True),
    sa.Column("agent_id", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L0_SCHEMA,
)


def layer_tables() -> list[sa.Table]:
    """Every table that carries the shared schema (nodes + edges of L1–L8)."""
    from app.clhear import derived_models
    from app.clhear.l1 import models as l1_models
    from app.clhear.models import cohorts, risk_narratives

    from app.clhear.l7 import models as l7_models
    from app.clhear.l8 import models as l8_models

    tables: list[sa.Table] = list(l1_models.ALL_TABLES) + list(derived_models.DERIVED_TABLES)
    tables += [risk_narratives, cohorts]
    tables += [l7_models.enforcement_events, l7_models.enforcement_links, l7_models.risk_scores,
               l7_models.stated_penalties, l7_models.penalty_links]
    tables += [l8_models.fills, l8_models.benchmark_aggregates]
    for loader in _EXTRA_LAYER_TABLE_LOADERS:
        tables += list(loader())
    return tables


_EXTRA_LAYER_TABLE_LOADERS: list[Any] = []


def register_layer_tables(loader) -> None:
    """Later layer packages register their tables so the migration and the
    'every table has the shared schema' test see them."""
    _EXTRA_LAYER_TABLE_LOADERS.append(loader)


# Tables that are deterministic projections of the WORM original / record store.
PROJECTION_TABLES: frozenset[str] = frozenset(
    {"search_units", "doc_nodes", "clauses", "citations", "clause_references", "clause_annotations"}
)


def rebuild_projection(conn: Connection, table: sa.Table, where) -> int:
    """Drop projection rows so they can be re-derived from the immutable original.

    Only tables in :data:`PROJECTION_TABLES` may be rebuilt; anything else must go
    through :func:`invalidate` (I2)."""
    if table.name not in PROJECTION_TABLES:
        raise DeletionForbidden(f"{table.name} is a node/edge table; use invalidate()")
    return conn.execute(table.delete().where(where)).rowcount or 0


# Full-text indexes are projections of search_units; they are rebuilt, never edited.
# FTS5 is a SQLite virtual table. On every other dialect the index does not exist
# and must never be probed: a failed statement on PostgreSQL aborts the open
# transaction, and every later statement in it fails with InFailedSqlTransaction —
# which is how an import that "only" indexed text lost its document rows.
FTS_INDEXES: frozenset[str] = frozenset({"search_units_fts"})
FTS_DIALECT = "sqlite"


def fts_supported(conn: Connection) -> bool:
    """True only on the dialect that has FTS5 at all. Answered from the dialect
    name, not from a probe, so it is safe inside any transaction."""
    return conn.dialect.name == FTS_DIALECT


def fts_available(conn: Connection, index: str = "search_units_fts") -> bool:
    """True when the FTS projection ``index`` can be read and written on this
    connection: SQLite, and the virtual table exists. Uses the catalogue, never
    a trial query, so a missing index cannot poison the transaction."""
    if index not in FTS_INDEXES:
        raise DeletionForbidden(f"{index} is not a registered FTS projection")
    if not fts_supported(conn):
        return False
    return sa.inspect(conn).has_table(index)


def drop_fts_rows(conn: Connection, index: str, rowids: Iterable[int]) -> int:
    """Remove rows from an FTS5 projection ahead of re-indexing. SQLite only:
    on any other dialect there is no index to clean and nothing is executed."""
    if index not in FTS_INDEXES:
        raise DeletionForbidden(f"{index} is not a registered FTS projection")
    if not fts_supported(conn):
        return 0
    ids = [int(i) for i in rowids]
    if not ids:
        return 0
    return conn.exec_driver_sql(f"DELETE FROM {index} WHERE rowid IN ({','.join(str(i) for i in ids)})").rowcount or 0


def layer_index(layer: str) -> int:
    layer = layer.upper()
    if layer not in LAYER_ORDER:
        raise ValueError(f"unknown layer {layer!r}")
    return LAYER_ORDER.index(layer)


def assert_layer_inputs(layer: str, input_layers: Iterable[str]) -> None:
    """I1: a layer may only read layers strictly below it (L0 is shared rails)."""
    li = layer_index(layer)
    for inp in input_layers:
        ii = layer_index(inp)
        if ii >= li and inp.upper() != "L0":
            raise LayerOrderViolation(f"{layer} may not read {inp} (layers derive strictly downward)")


def inputs_hash(*parts: Any) -> str:
    """Deterministic sha256 over JSON-normalized inputs."""
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


@dataclass
class WhyTrail:
    layer: str
    reasoning_summary: str
    evidence_refs: list[dict] = field(default_factory=list)
    inputs: tuple = ()
    model_manifest: dict = field(default_factory=dict)
    skill_version: str = ""
    confidence: float | None = None
    evals_run_id: str | None = None
    agent_id: str = ""
    subject_ref: str = ""
    input_layers: tuple[str, ...] = ()

    def write(self, conn: Connection) -> str:
        from app.clhear.platform.ids import next_id

        if self.input_layers:
            assert_layer_inputs(self.layer, self.input_layers)
        trail_id = next_id(conn, "WHY")
        conn.execute(
            why_trails.insert().values(
                id=trail_id,
                layer=self.layer.upper(),
                subject_ref=self.subject_ref,
                reasoning_summary=self.reasoning_summary,
                evidence_refs=self.evidence_refs,
                inputs_hash=inputs_hash(*self.inputs) if self.inputs else "",
                model_manifest=self.model_manifest,
                skill_version=self.skill_version,
                confidence=self.confidence,
                evals_run_id=self.evals_run_id,
                agent_id=self.agent_id,
            )
        )
        return trail_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def write(
    conn: Connection,
    table: sa.Table,
    row: dict,
    *,
    why: WhyTrail | str | None,
    valid_from: date | None = None,
    jurisdictions: list[str] | None = None,
) -> dict:
    """Insert one node/edge row with the shared schema populated.

    ``why`` is a :class:`WhyTrail` (written in the same transaction) or an existing
    why-trail id. ``None`` raises :class:`WhyTrailRequired`."""
    if why is None:
        raise WhyTrailRequired(f"write to {table.name} without a why-trail")
    if isinstance(why, WhyTrail):
        trail_id = why.write(conn)
        derived_by = why.agent_id or ""
        manifest = why.model_manifest
        ih = inputs_hash(*why.inputs) if why.inputs else row.get("inputs_hash", "")
        conf = why.confidence
    else:
        trail_id = str(why)
        derived_by = row.get("derived_by", "")
        manifest = row.get("model_manifest")
        ih = row.get("inputs_hash", "")
        conf = row.get("confidence")
    values = dict(row)
    values.setdefault("version", 1)
    values["why_trail_id"] = trail_id
    values.setdefault("derived_at", _now())
    values["derived_by"] = values.get("derived_by") or derived_by
    if manifest is not None:
        values["model_manifest"] = manifest
    values["inputs_hash"] = values.get("inputs_hash") or ih
    if conf is not None and values.get("confidence") is None:
        values["confidence"] = conf
    if valid_from is not None:
        values["valid_from"] = valid_from
    if jurisdictions is not None:
        values["jurisdictions"] = jurisdictions
    conn.execute(table.insert().values(**values))
    _audit_write(conn, table, values, trail_id, "write")
    return values


def _audit_write(conn: Connection, table: sa.Table, values: dict, trail_id: str, action: str) -> None:
    """Every write through this path lands in the audit log (HLD v2 §7.1) in the same
    transaction, so a row and its audit entry commit or roll back together."""
    from app.clhear.platform import audit

    audit.log_write(conn, table, values, why_trail_id=trail_id, action=action)


def invalidate(
    conn: Connection,
    table: sa.Table,
    where,
    *,
    valid_to: date | None = None,
    why: WhyTrail | str | None = None,
    reason: str = "",
) -> int:
    """Close ``valid_to`` on matching rows (never delete). Bumps ``version`` and
    records the invalidation in ``review``. Returns rows affected."""
    if why is None:
        raise WhyTrailRequired(f"invalidate on {table.name} without a why-trail")
    trail_id = why.write(conn) if isinstance(why, WhyTrail) else str(why)
    until = valid_to or _now().date()
    rows = conn.execute(sa.select(table).where(where)).mappings().all()
    count = 0
    for r in rows:
        review = r.get("review") or []
        if isinstance(review, str):
            review = json.loads(review)
        review = list(review) + [
            {"event": "invalidated", "at": _now().isoformat(), "why_trail_id": trail_id, "reason": reason}
        ]
        pk = [c for c in table.primary_key.columns]
        cond = sa.and_(*[c == r[c.name] for c in pk])
        conn.execute(
            table.update()
            .where(cond)
            .values(valid_to=until, version=(r.get("version") or 1) + 1, review=review, why_trail_id=trail_id)
        )
        _audit_write(conn, table, {**dict(r), "version": (r.get("version") or 1) + 1}, trail_id, "invalidate")
        count += 1
    return count


def in_force(table: sa.Table, as_of: date | None = None):
    """Filter clause: rows valid at ``as_of`` (default today)."""
    day = as_of or _now().date()
    return sa.and_(
        sa.or_(table.c.valid_to.is_(None), table.c.valid_to > day),
        sa.or_(table.c.valid_from.is_(None), table.c.valid_from <= day),
    )


def needs_human(layer: str, confidence: float | None) -> bool:
    if confidence is None:
        return True
    return float(confidence) < LOW_CONFIDENCE_THRESHOLDS.get(layer.upper(), 0.8)


def new_uuid() -> str:
    return str(uuid.uuid4())
