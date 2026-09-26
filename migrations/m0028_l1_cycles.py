# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0028 — durable full L1 cycle requests, manifests and child accounting."""
from app.clhear.l1.cycles import cycles, children


def upgrade(conn):
    cycles.create(conn, checkfirst=True)
    children.create(conn, checkfirst=True)
