# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0015 — HLD v2 §5 public UI: API keys, learning progress, profile watches.

* ``community.api_keys`` — one-click keys for signed-in users (secret hashed,
  revocation by ``revoked_at``, never deleted)
* ``community.learning_progress`` — Learn path steps completed per learner
* ``community.profile_watches`` — Watch: follow an L4 profile so the digest
  can report what changed for profiles like yours
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.community_models import COMMUNITY_SCHEMA, api_keys, learning_progress, profile_watches


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {COMMUNITY_SCHEMA}"))
    for table in (api_keys, learning_progress, profile_watches):
        table.create(conn, checkfirst=True)
