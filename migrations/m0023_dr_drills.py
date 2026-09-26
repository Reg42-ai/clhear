# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0023 — DR drill ledger (HLD v2 §7.1; item 17).

``l0_platform.dr_drills``: one row per scheduled restore drill (record + graph +
datalake replica), with RPO/RTO measured and the per-check outcome. Append-only;
the status page reads the latest row.
"""
from sqlalchemy.engine import Connection

from app.clhear.platform.dr import DR_TABLES


def upgrade(conn: Connection) -> None:
    for table in DR_TABLES:
        table.create(conn, checkfirst=True)
