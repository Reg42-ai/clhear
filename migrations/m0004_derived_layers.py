# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0004 — derived-layer tables: L2 obligations (machine-derived), L3/L5
curated catalog, L4 attribute schema + sample profiles, L6 blueprint log.

The m0003 `_reserved` markers stay; these are the first product tables the
reserved schemas were held for.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import DERIVED_TABLES

SCHEMAS = ("l2_obligations", "l3_building_blocks", "l4_profiles", "l5_activities", "l6_composer")


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        for schema in SCHEMAS:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    for table in DERIVED_TABLES:
        table.create(conn, checkfirst=True)
