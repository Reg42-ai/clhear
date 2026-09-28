# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0041 — drop the retired internal review ledger tables, if present."""
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.l1.models import L1_SCHEMA

# Children first: the source bindings reference the exception events.
_TABLES = ("l1_operator_exception_sources", "l1_operator_exceptions")


def upgrade(conn: Connection) -> None:
    schema = L1_SCHEMA if conn.dialect.name == "postgresql" else None
    inspector = sa.inspect(conn)
    for name in _TABLES:
        if inspector.has_table(name, schema=schema):
            sa.Table(name, sa.MetaData(schema=schema)).drop(conn)
