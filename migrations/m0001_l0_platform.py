# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0001 — l0_platform schema: events (outbox), proposals, llm_calls, eval_runs, runs.

HLD §6.1. The l1_sources schema arrives in P1 (m0002).
"""
from sqlalchemy.engine import Connection

from app.clhear.models import eval_runs, events, llm_calls, proposals, runs


def upgrade(conn: Connection) -> None:
    for table in (events, proposals, llm_calls, eval_runs, runs):
        table.create(conn, checkfirst=True)
