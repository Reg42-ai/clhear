# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0019 — HLD v2 §6 contributor model (I12).

``community.cla_signatures`` (append-only CLA ledger), ``community.roles``
(Reader / Contributor / Reviewer / Maintainer / Steering grants with a validity
window), ``community.contributions`` (CON- proposals with their automated checks,
the fleet's re-derivation verdict, acceptance, release and impact),
``community.contribution_reviews`` (two reviewers accept) and
``community.contributor_notifications`` ("your correction changed 41 blueprints").

A contribution never writes to a layer table by itself: acceptance applies it
through the approval console's record path, under the two reviewers' names.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.community_models import COMMUNITY_SCHEMA, CONTRIBUTION_TABLES


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {COMMUNITY_SCHEMA}"))
    for table in CONTRIBUTION_TABLES:
        table.create(conn, checkfirst=True)
