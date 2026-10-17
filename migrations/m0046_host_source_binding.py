# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0046 — whether a registered text binds: ``host_sources.binding``.

Unset (NULL) keeps the rule by kind: a law, regulation or standard binds;
enforcement actions and registers do not. ``false`` marks a text that does not
bind, such as voluntary guidance: L2 reads no obligations from it and L8 reads
it for practices. Existing rows stay unset, so nothing a host built changes.
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.hoststore import host_sources
from app.clhear.platform.shared_schema import qualified_name


def upgrade(conn: Connection) -> None:
    schema = host_sources.schema if conn.dialect.name == "postgresql" else None
    inspector = sa.inspect(conn)
    if not inspector.has_table(host_sources.name, schema=schema):
        return
    existing = {c["name"] for c in inspector.get_columns(host_sources.name, schema=schema)}
    if "binding" not in existing:
        conn.execute(text(f"ALTER TABLE {qualified_name(conn, host_sources)} ADD COLUMN binding BOOLEAN"))
