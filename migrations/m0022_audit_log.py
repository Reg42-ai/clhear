# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0022 — Audit log (HLD v2 §7.1; item 17).

``l0_platform.audit_log``: append-only ledger of every write through ``record.write`` /
``record.invalidate``, every read of licensed clause text, and every mutating HTTP
request, with the actor bound by the request middleware. Never updated or deleted.
"""
from sqlalchemy.engine import Connection

from app.clhear.platform.audit import AUDIT_TABLES


def upgrade(conn: Connection) -> None:
    for table in AUDIT_TABLES:
        table.create(conn, checkfirst=True)
