# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Persist artifact-bound document locations; no text or approval is seeded."""
import sqlalchemy as sa


def upgrade(conn):
    schema = "l1_sources" if conn.dialect.name == "postgresql" else None
    if "source_locator" not in {c["name"] for c in sa.inspect(conn).get_columns("doc_nodes", schema=schema)}:
        table = "l1_sources.doc_nodes" if schema else "doc_nodes"
        kind = "JSONB" if schema else "JSON"
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN source_locator {kind} NOT NULL DEFAULT '{{}}'")
