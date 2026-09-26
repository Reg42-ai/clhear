# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0035 — additive deferred-delivery ledger (held, misrouted, unknown or malformed
queue messages preserved with their evidence before acknowledgement) and the
outbox relay disposition (sent | audit_only | quarantined) per event."""
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.models import events
from app.clhear.platform.deferred import TABLES


def upgrade(conn: Connection) -> None:
    for table in TABLES:
        table.create(conn, checkfirst=True)
    schema = None if conn.dialect.name == "sqlite" else events.schema
    columns = {c["name"] for c in sa.inspect(conn).get_columns(events.name, schema=schema)}
    if "relay_disposition" not in columns:
        qualified = f"{schema}.{events.name}" if schema else events.name
        conn.exec_driver_sql(f"ALTER TABLE {qualified} ADD COLUMN relay_disposition TEXT")
