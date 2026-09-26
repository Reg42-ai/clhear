# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0009 — HLD v2 §4.1 L1 hardening.

* ``sources``       + rights_basis, publisher, instrument, family_root
* ``clauses``       + span_start, span_end, normative
* ``change_events`` + clause_ids, effective_date, effective_date_basis
* new ``rights_records`` (rights recorder ledger) and ``watchlists``

Existing rows are backfilled: rights_basis from the adapter's declared basis
(l1.rights.BASIS_BY_ADAPTER), family_root from the root membership row.
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.l1.models import change_events, clauses, family_members, rights_records, sources, watchlists
from app.clhear.platform.shared_schema import ensure_shared_columns, qualified_name

_NEW_COLUMNS = {
    sources: [
        ("rights_basis", "TEXT NOT NULL DEFAULT 'open_licence'"),
        ("publisher", "TEXT NOT NULL DEFAULT ''"),
        ("instrument", "TEXT NOT NULL DEFAULT ''"),
        ("family_root", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ],
    clauses: [
        ("span_start", "INTEGER"),
        ("span_end", "INTEGER"),
        ("normative", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ],
    change_events: [
        ("clause_ids", "JSON NOT NULL DEFAULT '[]'"),
        ("effective_date", "DATE"),
        ("effective_date_basis", "TEXT NOT NULL DEFAULT ''"),
    ],
}


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
        if conn.engine.dialect.name == "postgresql" and ddl.startswith("JSON"):
            ddl = ddl.replace("JSON", "JSONB", 1)
        conn.execute(text(f"ALTER TABLE {qualified_name(conn, table)} ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


def upgrade(conn: Connection) -> None:
    for table, columns in _NEW_COLUMNS.items():
        _add_missing(conn, table, columns)
    rights_records.create(conn, checkfirst=True)
    watchlists.create(conn, checkfirst=True)
    for table in (rights_records, watchlists):
        ensure_shared_columns(conn, table)

    from app.clhear.l1.rights import BASIS_BY_ADAPTER

    for adapter, basis in BASIS_BY_ADAPTER.items():
        conn.execute(
            sources.update()
            .where(sources.c.adapter == adapter)
            .where(sources.c.rights_basis == "open_licence")
            .values(rights_basis=basis)
        )
    conn.execute(sources.update().where(sources.c.license == "restricted").values(rights_basis="byol_only"))
    root_ids = sa.select(family_members.c.source_id).where(family_members.c.relation == "root")
    conn.execute(sources.update().where(sources.c.id.in_(root_ids)).values(family_root=True))
