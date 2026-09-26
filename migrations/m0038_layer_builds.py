# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0038 — one row per layer build of a scoped corpus: its inputs and output revision."""
from sqlalchemy.engine import Connection

from app.clhear.layer_builds import layer_builds


def upgrade(conn: Connection) -> None:
    layer_builds.create(conn, checkfirst=True)
