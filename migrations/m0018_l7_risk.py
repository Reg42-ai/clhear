# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0018 — HLD v2 §4.7 L7 risk and priority scoring.

* ``l7_risk.enforcement_events`` (ENF-), ``enforcement_links``, ``risk_scores``
  (RSK-), ``risk_calibrations`` — bi-temporal product tables with the shared
  schema (the ``_reserved`` marker from 0003 stays; the layer is now published).
* ``l1_sources.sources.kind`` admits ``'enforcement'``: final notices, enforcement
  actions and disciplinary decisions are L1 sources (verbatim, rights-recorded)
  that L7 reads — they are *informative* family members, so L2 never derives
  obligations from them.
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.l1.models import L1_SCHEMA, sources
from app.clhear.l7.models import L7_SCHEMA, L7_TABLES

_KINDS = "('law','regulation','standard','guidance','form','agreement','enforcement')"


def _sqlite_has_kind(conn: Connection) -> bool:
    ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'")).scalar()
    return not ddl or "enforcement" in ddl


def _sqlite_rebuild_sources(conn: Connection) -> None:
    """SQLite cannot alter a CHECK constraint: rebuild ``sources`` from the model."""
    cols = [c["name"] for c in sa.inspect(conn).get_columns("sources")]
    model_cols = [c.name for c in sources.columns if c.name in cols]
    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    conn.exec_driver_sql("ALTER TABLE sources RENAME TO sources__old")
    sources.create(conn, checkfirst=False)
    col_list = ", ".join(f'"{c}"' for c in model_cols)
    conn.exec_driver_sql(f"INSERT INTO sources ({col_list}) SELECT {col_list} FROM sources__old")
    conn.exec_driver_sql("DROP TABLE sources__old")
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def upgrade(conn: Connection) -> None:
    pg = conn.engine.dialect.name == "postgresql"
    if pg:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {L7_SCHEMA}"))
        conn.execute(text(f'ALTER TABLE {L1_SCHEMA}.sources DROP CONSTRAINT IF EXISTS sources_kind_check'))
        conn.execute(text(f'ALTER TABLE {L1_SCHEMA}.sources ADD CONSTRAINT sources_kind_check CHECK (kind in {_KINDS})'))
    elif not _sqlite_has_kind(conn):
        _sqlite_rebuild_sources(conn)
    for table in L7_TABLES:
        table.create(conn, checkfirst=True)
