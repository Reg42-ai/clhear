# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0042 — every derived record carries its evidence; seeded content leaves.

* ``evidence`` (JSON quotes of clause text) on obligations, blocks, requires,
  characteristics, license_types, applies_to, activities and operates;
* ``evidence_gaps``: what a build could not derive and which source would help;
* the block kind ``Unspecified`` (the text names a measure, not its kind);
* rows seeded from the retired reviewed catalog (blocks, activities, the
  licence / product / client / channel ontology, validity rules, sample
  profiles, starter concepts) are removed, and applicability edges written in
  the retired attribute vocabulary are closed, so the next build derives them
  again from the texts in scope;
* measures, requires edges, characteristics, activities and operates edges
  derived before quotes existed are closed (never deleted), so the next build
  derives them again with quotes. The offline sample's rows are left alone.
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import (
    BLOCK_KINDS,
    activities,
    applies_to,
    blocks,
    channels,
    characteristics,
    client_types,
    concept_members,
    concepts,
    evidence_gaps,
    implies,
    license_types,
    licences,
    mitigates,
    obligations,
    operates,
    permits,
    products_services,
    requires,
    sample_profiles,
    validity_rules,
)
from app.clhear.platform.shared_schema import qualified_name

_WITH_EVIDENCE = (obligations, blocks, requires, characteristics, license_types, applies_to, activities, operates)


def _schema(conn: Connection, table: sa.Table):
    return table.schema if conn.dialect.name == "postgresql" else None


def _has(conn: Connection, table: sa.Table) -> bool:
    return sa.inspect(conn).has_table(table.name, schema=_schema(conn, table))


def _add_evidence(conn: Connection, table: sa.Table) -> None:
    if not _has(conn, table):
        return
    existing = {c["name"] for c in sa.inspect(conn).get_columns(table.name, schema=_schema(conn, table))}
    if "evidence" in existing:
        return
    kind = "JSONB" if conn.dialect.name == "postgresql" else "JSON"
    conn.execute(text(f"ALTER TABLE {qualified_name(conn, table)} ADD COLUMN evidence {kind}"))


def _allow_unspecified_kind(conn: Connection) -> None:
    allowed = "','".join(BLOCK_KINDS)
    if conn.dialect.name == "postgresql":
        name = qualified_name(conn, blocks)
        conn.execute(text(f"ALTER TABLE {name} DROP CONSTRAINT IF EXISTS blocks_kind_check"))
        conn.execute(text(f"ALTER TABLE {name} ADD CONSTRAINT blocks_kind_check CHECK (kind in ('{allowed}'))"))
        return
    sql = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='blocks'")).scalar() or ""
    if "blocks_kind_check" not in sql or "Unspecified" in sql:
        return
    # SQLite cannot alter a CHECK constraint: rebuild the table with the current definition.
    old = [c["name"] for c in sa.inspect(conn).get_columns("blocks")]
    conn.execute(text("ALTER TABLE blocks RENAME TO blocks_before_0042"))
    blocks.create(conn)
    shared = [c for c in old if c in blocks.c]
    cols = ", ".join(shared)
    conn.execute(text(f"INSERT INTO blocks ({cols}) SELECT {cols} FROM blocks_before_0042"))
    conn.execute(text("DROP TABLE blocks_before_0042"))


def _remove_seeded(conn: Connection) -> None:
    today = datetime.now(timezone.utc).date()
    if _has(conn, blocks):
        seeded = [r[0] for r in conn.execute(sa.select(blocks.c.id).where(blocks.c.status == "curated"))]
        if seeded:
            if _has(conn, requires):
                conn.execute(requires.delete().where(requires.c.block_id.in_(seeded)))
            if _has(conn, characteristics):
                conn.execute(characteristics.delete().where(characteristics.c.block_id.in_(seeded)))
            if _has(conn, operates):
                conn.execute(operates.delete().where(operates.c.block_id.in_(seeded)))
            conn.execute(blocks.delete().where(blocks.c.id.in_(seeded)))
    if _has(conn, activities):
        seeded = [r[0] for r in conn.execute(sa.select(activities.c.id).where(activities.c.status == "curated"))]
        if seeded:
            for table, cols in ((implies, (implies.c.activity_id,)), (operates, (operates.c.activity_id,)),
                                (mitigates, (mitigates.c.compliance_activity_id, mitigates.c.business_activity_id))):
                if _has(conn, table):
                    conn.execute(table.delete().where(sa.or_(*(col.in_(seeded) for col in cols))))
            conn.execute(activities.delete().where(activities.c.id.in_(seeded)))
    for table in (permits, validity_rules, licences, products_services, client_types, channels, sample_profiles):
        if _has(conn, table):
            conn.execute(table.delete())
    if _has(conn, concepts):
        seeded = [r[0] for r in conn.execute(sa.select(concepts.c.id).where(concepts.c.status == "curated"))]
        if seeded:
            if _has(conn, concept_members):
                conn.execute(concept_members.delete().where(concept_members.c.concept_id.in_(seeded)))
            conn.execute(concepts.delete().where(concepts.c.id.in_(seeded)))
    if _has(conn, applies_to):
        conn.execute(applies_to.update().where(applies_to.c.valid_to.is_(None)).values(valid_to=today))


def _close_unquoted(conn: Connection) -> None:
    """Rows derived before 0.2 carry no quotes: close them so the next build derives them again."""
    today = datetime.now(timezone.utc).date()
    for table, keep in ((requires, requires.c.method == "offline-sample"),
                        (characteristics, characteristics.c.method == "offline-sample"),
                        (operates, operates.c.activity_id.like("ACT-DECLARED%")),
                        (blocks, blocks.c.id.like("BLK-DECLARED%")),
                        (activities, activities.c.id.like("ACT-DECLARED%"))):
        if _has(conn, table):
            conn.execute(table.update().where(table.c.valid_to.is_(None), table.c.evidence.is_(None),
                                              sa.not_(keep)).values(valid_to=today))


def upgrade(conn: Connection) -> None:
    for table in _WITH_EVIDENCE:
        _add_evidence(conn, table)
    evidence_gaps.create(conn, checkfirst=True)
    if _has(conn, blocks):
        _allow_unspecified_kind(conn)
    _remove_seeded(conn)
    _close_unquoted(conn)
