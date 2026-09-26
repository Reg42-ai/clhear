# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0005 — community schema: contributor users, submissions, validation votes."""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.community_models import COMMUNITY_SCHEMA, COMMUNITY_TABLES


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {COMMUNITY_SCHEMA}"))
    for table in COMMUNITY_TABLES:
        table.create(conn, checkfirst=True)
