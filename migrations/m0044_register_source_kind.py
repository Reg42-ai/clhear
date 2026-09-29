# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0044 — the source kind ``register``; enforcement and register sources are informative.

* ``l1_sources.sources.kind`` admits ``'register'``: an official register of
  licensed or authorised entities, or of licence categories. L4 reads licence
  types from its entries.
* Sources of kind ``enforcement`` or ``register`` are informative family
  members: L2 never reads them for obligations. Host-registered sources were
  stored as binding before; they are moved now.

SQLite cannot alter a CHECK constraint, so the table is rebuilt: a copy is
created from the stored definition with the widened check, filled, and renamed
over the original. Renaming the copy (not the original) leaves the foreign keys
of the child tables pointing at ``sources``.
"""
import re

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.l1.models import INFORMATIVE_KINDS, L1_SCHEMA, family_members, sources

_KINDS = "('law','regulation','standard','guidance','form','agreement','enforcement','register')"


def _sqlite_widen(conn: Connection) -> None:
    ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'")).scalar()
    if not ddl or "'register'" in ddl:
        return
    widened = re.sub(r"\bkind in \([^)]*\)", f"kind in {_KINDS}", ddl, count=1)
    widened = re.sub(r'^CREATE TABLE\s+"?sources"?', "CREATE TABLE sources__new", widened, count=1)
    columns = ", ".join(f'"{c["name"]}"' for c in sa.inspect(conn).get_columns("sources"))
    conn.exec_driver_sql(widened)
    conn.exec_driver_sql(f"INSERT INTO sources__new ({columns}) SELECT {columns} FROM sources")
    conn.exec_driver_sql("DROP TABLE sources")
    conn.exec_driver_sql("ALTER TABLE sources__new RENAME TO sources")


def upgrade(conn: Connection) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"ALTER TABLE {L1_SCHEMA}.sources DROP CONSTRAINT IF EXISTS sources_kind_check"))
        conn.execute(text(f"ALTER TABLE {L1_SCHEMA}.sources ADD CONSTRAINT sources_kind_check CHECK (kind in {_KINDS})"))
    else:
        _sqlite_widen(conn)
    informative = sa.select(sources.c.id).where(sources.c.kind.in_(INFORMATIVE_KINDS))
    conn.execute(family_members.update().where(family_members.c.source_id.in_(informative),
                                               family_members.c.tier == "binding").values(tier="informative"))
