# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0025 — append-only scope snapshots, worker audits and reviewed scope evidence.

No artifacts, permissions, successful audits or scope reviews are seeded.
"""
from app.clhear.l1.inventory import metadata


def upgrade(conn):
    metadata.create_all(conn, checkfirst=True)
