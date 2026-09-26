# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0002 — l1_sources schema: families, sources, versions, doc_nodes (typed raw
document tree), clauses (provision projection), citations, discovery
candidates, change events, licenses, BYOL uploads.

HLD §6.2. The `clauses_public` / `nodes_public` discipline is enforced in
code via app.clhear.l1.public (the Aurora view + reader-role grants land
when deployment notes is wired — # ARCH).
"""
from sqlalchemy.engine import Connection

from app.clhear.l1.models import ALL_TABLES


def upgrade(conn: Connection) -> None:
    for table in ALL_TABLES:
        table.create(conn, checkfirst=True)
    if conn.dialect.name == "sqlite":
        # BM25 full-text index over the search units (standalone FTS5 table,
        # rowid == search_units.id, kept in sync by the pipeline indexer).
        # ARCH: on Aurora this becomes a tsvector column + GIN index.
        try:
            conn.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search_units_fts USING fts5(text)"
            )
        except Exception:
            pass  # FTS5 not compiled in: retrieval falls back to LIKE
