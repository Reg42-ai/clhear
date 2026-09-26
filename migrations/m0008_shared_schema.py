# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0008 — HLD v2 §3 shared schema on every layer table + why_trails + id_sequences.

Adds ``version, valid_from, valid_to, derived_at, derived_by, model_manifest,
inputs_hash, confidence, why_trail_id, review, jurisdictions`` to every node/edge
table (L1 corpus, L2–L8 derived tables). Existing rows are backfilled with
``version=1, derived_by='migration:m0008'`` so the invariant "every node is
versioned and dated" (I2) holds retroactively. Ledger tables are exempt.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.platform.ids import id_sequences
from app.clhear.platform.record import ensure_shared_columns, layer_tables, why_trails


def upgrade(conn: Connection) -> None:
    why_trails.create(conn, checkfirst=True)
    id_sequences.create(conn, checkfirst=True)
    for table in layer_tables():
        added = ensure_shared_columns(conn, table)
        if "derived_by" in added:
            name = f'"{table.schema}"."{table.name}"' if conn.engine.dialect.name == "postgresql" else f'"{table.name}"'
            conn.execute(text(f"UPDATE {name} SET derived_by = 'migration:m0008' WHERE derived_by = ''"))
