# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The clauses_public / nodes_public discipline (HLD §6.2, working rule 4).

Every code path that can emit clause text, raw_text, or a source_fragment
MUST go through this module (or an explicit BYOL check, P3). On Aurora this
is the `clauses_public` view and the reader role has no grant on the raw
tables; the SQLite fallback enforces the same allow-list in the query
itself. # ARCH: swap to the view when Aurora is wired.
"""
import sqlalchemy as sa

from app.clhear.l1.models import clauses, doc_nodes, source_versions, sources


def _allowed_source_ids(conn=None):
    """Recheck the current operation ledger, including revocation and expiry."""
    from app.clhear.l1 import permissions
    if conn is None:
        from app.clhear.db import get_engine
        with get_engine().connect() as connection:
            return _allowed_source_ids(connection)
    from app.clhear.l1.origin import corpus_sources_predicate, production_worker
    rows = conn.execute(sa.select(sources).where(corpus_sources_predicate() if production_worker() else sa.true())).mappings().all()
    protected = [row for row in rows if permissions.required_for(row)]
    allowed = [row["id"] for row in rows if not permissions.required_for(row)]
    schema = None if conn.dialect.name == "sqlite" else permissions.L1_SCHEMA
    if protected and sa.inspect(conn).has_table(permissions.source_permissions.name, schema=schema):
        allowed.extend(row["id"] for row in protected
                       if permissions.decision(conn, row["key"], "display_public")["allowed"])
    return allowed


def _public_version_ids(conn=None):
    return sa.select(source_versions.c.id).where(source_versions.c.source_id.in_(_allowed_source_ids(conn)))


def clauses_public_select(conn=None) -> sa.Select:
    """SELECT over clauses restricted to public_ok rows — the ONLY way to read
    clause text for an external caller."""
    return sa.select(
        clauses.c.id,
        clauses.c.source_version_id,
        clauses.c.doc_node_id,
        clauses.c.ref,
        clauses.c.path,
        clauses.c.ordering,
        clauses.c.text,
        clauses.c.text_hash,
    ).where(clauses.c.public_ok.is_(True), clauses.c.source_version_id.in_(_public_version_ids(conn)))


def clause_refs_select() -> sa.Select:
    """Refs/hashes only; paths may contain verbatim publisher headings."""
    return sa.select(
        clauses.c.id,
        clauses.c.source_version_id,
        clauses.c.doc_node_id,
        clauses.c.ref,
        sa.literal(None).label("path"),
        clauses.c.ordering,
        clauses.c.text_hash,
    )


def nodes_internal_select(conn):
    """Old worker snapshots stay readable; missing locator evidence is null.

    Only workers migrate persisted data. Readers cannot invent old locations.
    """
    schema = doc_nodes.schema if conn.dialect.name == "postgresql" else None
    columns = {c["name"] for c in sa.inspect(conn).get_columns(doc_nodes.name, schema=schema)}
    return sa.select(*(column if column.name in columns else sa.literal(None).label(column.name)
                       for column in doc_nodes.columns))


def nodes_public_select(conn=None) -> sa.Select:
    """Document reconstruction rows with raw_text (public_ok only)."""
    # Published projections may predate locator storage; reads never migrate them.
    locator = sa.literal(None).label("source_locator")
    if conn is not None:
        schema = doc_nodes.schema if conn.dialect.name == "postgresql" else None
        columns = {c["name"] for c in sa.inspect(conn).get_columns(doc_nodes.name, schema=schema)}
        if "source_locator" in columns:
            locator = doc_nodes.c.source_locator
    return sa.select(
        doc_nodes.c.id,
        doc_nodes.c.parent_id,
        doc_nodes.c.seq,
        doc_nodes.c.depth,
        doc_nodes.c.node_type,
        doc_nodes.c.ref,
        doc_nodes.c.label,
        doc_nodes.c.heading,
        doc_nodes.c.raw_text,
        doc_nodes.c.text_hash,
        doc_nodes.c.public_ok,
        doc_nodes.c.source_version_id,
        locator,
    ).where(doc_nodes.c.public_ok.is_(True), doc_nodes.c.source_version_id.in_(_public_version_ids(conn)))


def nodes_refs_select() -> sa.Select:
    """Structure/refs/hashes only — no raw_text, no source_fragment."""
    return sa.select(
        doc_nodes.c.id,
        doc_nodes.c.parent_id,
        doc_nodes.c.seq,
        doc_nodes.c.depth,
        doc_nodes.c.node_type,
        doc_nodes.c.ref,
        sa.literal(None).label("label"),
        sa.literal(None).label("heading"),
        doc_nodes.c.text_hash,
        doc_nodes.c.public_ok,
        doc_nodes.c.source_version_id,
    )
