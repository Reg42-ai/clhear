# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0020 — HLD v2 §4.8 L8 benchmarks and fills.

``l8_benchmarks.fills`` (FIL-), ``fill_reviews``, ``benchmark_inputs`` (HMAC-keyed
member observations, never served), ``benchmark_aggregates`` (BMA-, k ≥ 5 with
Laplace noise) and ``members`` (who may read L8). The layer stays closed by mode
(I9): only fill existence and maturity are public metadata.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.l8.models import L8_SCHEMA, L8_TABLES


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {L8_SCHEMA}"))
    for table in L8_TABLES:
        table.create(conn, checkfirst=True)
