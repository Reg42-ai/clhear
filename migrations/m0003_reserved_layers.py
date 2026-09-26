# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0003 — reserve empty schemas for L2–L8 so later layer migrations do not collide.

No product tables. Bodies stay empty until those HLDs ship.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

RESERVED = (
    ("l2_obligations", "L2"),
    ("l3_building_blocks", "L3"),
    ("l4_profiles", "L4"),
    ("l5_activities", "L5"),
    ("l6_composer", "L6"),
    ("l7_risk", "L7"),
    ("l8_benchmarks", "L8"),
)


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name != "postgresql":
        return
    for schema, layer in RESERVED:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        conn.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {schema}._reserved ("
                "layer TEXT PRIMARY KEY, "
                "status TEXT NOT NULL DEFAULT 'not_published')"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO {schema}._reserved (layer, status) VALUES (:layer, 'not_published') "
                "ON CONFLICT (layer) DO NOTHING"
            ),
            {"layer": layer},
        )
