# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Bind cycle admission to parser settings as well as the worker image."""
import sqlalchemy as sa
from app.clhear.l1.cycles import cycles


def upgrade(conn):
    schema = cycles.schema if conn.dialect.name == "postgresql" else None
    if "parser_configuration_digest" not in {c["name"] for c in sa.inspect(conn).get_columns("l1_cycles", schema=schema)}:
        table = f"{schema}.l1_cycles" if schema else "l1_cycles"
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN parser_configuration_digest TEXT")
