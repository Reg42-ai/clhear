# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0039 — the web tier's role may record and read observations.

Observations are tenant writes arriving through ``/v1/observations``. The web
tier serves a read-only snapshot, so it keeps them in Aurora as ``clhear_web``
(granted by 0037), which may insert and read them but not change or remove one.
"""
from sqlalchemy.engine import Connection

from app.clhear.identity import ROLE


def upgrade(conn: Connection) -> None:
    if conn.dialect.name != "postgresql":
        return
    conn.exec_driver_sql(
        f"DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') "
        f"THEN GRANT SELECT, INSERT ON l0_platform.observations TO {ROLE}; END IF; END $$")
