# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Add owner exception evidence only; migration grants no access."""
def upgrade(conn):
    from app.clhear.l1.operator_exceptions import TABLES
    for table in TABLES:
        table.create(conn, checkfirst=True)
