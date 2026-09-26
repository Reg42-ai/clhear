# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Preserve source history while recording explicit test-origin exclusions."""


def upgrade(conn):
    from app.clhear.l1.origin import metadata
    metadata.create_all(conn)
