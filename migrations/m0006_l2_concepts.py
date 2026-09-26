# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0006 — L2 concepts: consolidated cross-jurisdiction CLHEAR obligations
(concepts + concept_members). Resolution stays computed, never stored."""
from sqlalchemy.engine import Connection

from app.clhear.derived_models import concept_members, concepts


def upgrade(conn: Connection) -> None:
    for table in (concepts, concept_members):
        table.create(conn, checkfirst=True)
