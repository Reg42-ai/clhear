# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0017 — HLD v2 I4 / §8 item 11: the approval console's human-edit ledger.

``l0_platform.human_edits`` records every maintainer decision that changes or
vouches for a determination (field edits from modification requests, validations
from the low-confidence queue, expert verdicts on review escalations) together
with the basis hash it was taken on, so the next derivation cycle can reproduce
the edit or escalate it.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.models import L0_SCHEMA, human_edits


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {L0_SCHEMA}"))
    human_edits.create(conn, checkfirst=True)
