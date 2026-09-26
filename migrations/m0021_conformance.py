# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0021 — Conformance program (standard §7, Annex E; item 15).

``conformance.self_assessments`` (CFA- Annex E submissions with their automated
checks and the verifier's decision), ``conformance.marks`` (CFM- register entries —
CL1/CL2 from verified self-assessments, CL3/CL4 from accredited assessors' ISAE 3000
reports; withdrawn marks stay visible) and ``conformance.assessors`` (the accredited
assessor register). The program stores evidence *references*, never artefacts.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.conformance import CONFORMANCE_SCHEMA, CONFORMANCE_TABLES


def upgrade(conn: Connection) -> None:
    if conn.engine.dialect.name == "postgresql":
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {CONFORMANCE_SCHEMA}"))
    for table in CONFORMANCE_TABLES:
        table.create(conn, checkfirst=True)
