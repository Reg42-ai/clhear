# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0036 — observation documents (app / agent / process performance)."""
from sqlalchemy.engine import Connection

from app.clhear.observations import observations


def upgrade(conn: Connection) -> None:
    observations.create(conn, checkfirst=True)
