# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0040 — host-declared sources, profiles, runs, releases, and webhooks."""
from sqlalchemy.engine import Connection

from app.clhear.hoststore import TABLES


def upgrade(conn: Connection) -> None:
    for table in TABLES:
        table.create(conn, checkfirst=True)
