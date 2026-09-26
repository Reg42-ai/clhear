# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Persist worker-owned resumable publisher discovery frontiers."""
from app.clhear.l1.discovery import metadata


def upgrade(conn):
    metadata.create_all(conn)
