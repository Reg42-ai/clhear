# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0011 — HLD v2 §4.3 L3 building blocks.

* ``blocks`` + kind (8-kind enum), purpose, canonical_id
* new ``requires`` (obligation -> block with rationale span),
  ``characteristics`` (fixed schema per kind, backed / not_specified),
  ``l3_kinds`` (schema registry served at /l3/kinds)

Existing blocks (curated + AI-generated) get a kind: curated blocks from the
reviewed JSON, generated ones inferred from their name / capability. Blocks
that already anchor obligations through ``satisfies`` selectors get explicit
``requires`` edges so the completeness gate sees them.
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import blocks, characteristics, l3_kinds, requires
from app.clhear.platform.shared_schema import ensure_shared_columns, qualified_name

_NEW_COLUMNS = [
    ("kind", "TEXT NOT NULL DEFAULT 'Process'"),
    ("purpose", "TEXT NOT NULL DEFAULT ''"),
    ("canonical_id", "TEXT"),
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
    _add_missing(conn, blocks, _NEW_COLUMNS)
    for table in (requires, characteristics):
        table.create(conn, checkfirst=True)
        ensure_shared_columns(conn, table)
    l3_kinds.create(conn, checkfirst=True)

    from app.clhear.l3.kinds import kinds_catalog

    for entry in kinds_catalog():
        exists = conn.execute(sa.select(l3_kinds.c.kind).where(l3_kinds.c.kind == entry["kind"])).first()
        if not exists:
            conn.execute(l3_kinds.insert().values(kind=entry["kind"], description=entry["description"], fields=entry["fields"]))

    from app.clhear.l3.decompose import backfill_kinds_and_requires

    backfill_kinds_and_requires(conn)
