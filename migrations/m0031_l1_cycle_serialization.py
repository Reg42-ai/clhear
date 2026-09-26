# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0031 — durable FIFO admission and one whole-cycle L1 slot."""
from app.clhear.l1.cycles import queue, slot


def upgrade(conn):
    queue.create(conn, checkfirst=True)
    slot.create(conn, checkfirst=True)
