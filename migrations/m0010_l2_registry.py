# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0010 — HLD v2 §4.2 L2 obligation registry.

* ``obligations`` + stable_id (OBL-), determination, subject / action /
  condition / object, regulator, obligation_type, effective_from / _to,
  canonical_id, review_confidence
* new ``asserts``, ``equivalences``, ``supersessions``, ``l2_change_events``,
  ``obligation_reviews`` (all with the shared bi-temporal columns)

Existing obligations are backfilled with a stable id and an ``asserts`` edge
to the clause they were derived from (explicit strength, whole-clause span).
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import (
    asserts,
    equivalences,
    l2_change_events,
    obligation_reviews,
    obligations,
    supersessions,
)
from app.clhear.platform.shared_schema import ensure_shared_columns, qualified_name

_NEW_COLUMNS = [
    ("stable_id", "TEXT"),
    ("determination", "TEXT NOT NULL DEFAULT ''"),
    ("subject", "TEXT NOT NULL DEFAULT ''"),
    ("action", "TEXT NOT NULL DEFAULT ''"),
    ("condition", "TEXT NOT NULL DEFAULT ''"),
    ("object", "TEXT NOT NULL DEFAULT ''"),
    ("regulator", "TEXT NOT NULL DEFAULT ''"),
    ("obligation_type", "TEXT NOT NULL DEFAULT ''"),
    ("effective_from", "DATE"),
    ("effective_to", "DATE"),
    ("canonical_id", "TEXT"),
    ("review_confidence", "NUMERIC(4,3)"),
]


def _add_missing(conn: Connection, table: sa.Table, columns: list[tuple[str, str]]) -> list[str]:
    insp = sa.inspect(conn)
    schema = table.schema if conn.engine.dialect.name == "postgresql" else None
    if not insp.has_table(table.name, schema=schema):
        return []
    existing = {c["name"] for c in insp.get_columns(table.name, schema=schema)}
    added = []
    for name, ddl in columns:
        if name in existing:
            continue
        conn.execute(text(f"ALTER TABLE {qualified_name(conn, table)} ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


def upgrade(conn: Connection) -> None:
    added = _add_missing(conn, obligations, _NEW_COLUMNS)
    if "stable_id" in added:
        conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS obligations_stable_id_uq "
                f"ON {qualified_name(conn, obligations)} (stable_id)"
            )
        )
    for table in (asserts, equivalences, supersessions, l2_change_events, obligation_reviews):
        table.create(conn, checkfirst=True)
        ensure_shared_columns(conn, table)

    # Backfill: every existing obligation gets a stable id and its basis edge.
    from app.clhear.l2.registry import backfill_stable_ids_and_asserts

    backfill_stable_ids_and_asserts(conn)
