# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Shared schema columns every layer table inherits (HLD v2 §3).

Import-safe (no model imports) so model modules can attach the columns at
definition time; :mod:`app.clhear.platform.record` re-exports for callers.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

Json = sa.JSON().with_variant(JSONB(), "postgresql")


def shared_columns() -> list[sa.Column]:
    """Fresh Column objects for the shared schema (a Column binds to one table)."""
    return [
        sa.Column("version", sa.Integer, nullable=False, default=1, server_default="1"),
        sa.Column("valid_from", sa.Date, nullable=True),
        sa.Column("valid_to", sa.Date, nullable=True),
        sa.Column("derived_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("derived_by", sa.Text, nullable=False, default="", server_default=""),
        sa.Column("model_manifest", Json, nullable=True),
        sa.Column("inputs_hash", sa.Text, nullable=False, default="", server_default=""),
        sa.Column("confidence", sa.Numeric(4, 3, asdecimal=False), nullable=True),
        sa.Column("why_trail_id", sa.Text, nullable=True),
        sa.Column("review", Json, nullable=True),
        sa.Column("jurisdictions", Json, nullable=True),
    ]


SHARED_COLUMN_NAMES: tuple[str, ...] = tuple(c.name for c in shared_columns())

_ADD_COLUMN_SQL: dict[str, dict[str, str]] = {
    "postgresql": {
        "version": "integer NOT NULL DEFAULT 1",
        "valid_from": "date",
        "valid_to": "date",
        "derived_at": "timestamptz DEFAULT now()",
        "derived_by": "text NOT NULL DEFAULT ''",
        "model_manifest": "jsonb",
        "inputs_hash": "text NOT NULL DEFAULT ''",
        "confidence": "numeric(4,3)",
        "why_trail_id": "text",
        "review": "jsonb",
        "jurisdictions": "jsonb",
    },
    "sqlite": {
        "version": "INTEGER NOT NULL DEFAULT 1",
        "valid_from": "DATE",
        "valid_to": "DATE",
        "derived_at": "TIMESTAMP",
        "derived_by": "TEXT NOT NULL DEFAULT ''",
        "model_manifest": "JSON",
        "inputs_hash": "TEXT NOT NULL DEFAULT ''",
        "confidence": "NUMERIC(4,3)",
        "why_trail_id": "TEXT",
        "review": "JSON",
        "jurisdictions": "JSON",
    },
}


def attach_shared_columns(*tables: sa.Table) -> None:
    """Append missing shared columns to Table objects (idempotent)."""
    for table in tables:
        for col in shared_columns():
            if col.name not in table.c:
                table.append_column(col)


def qualified_name(conn: Connection, table: sa.Table) -> str:
    if conn.engine.dialect.name == "postgresql" and table.schema:
        return f'"{table.schema}"."{table.name}"'
    return f'"{table.name}"'


def ensure_shared_columns(conn: Connection, table: sa.Table) -> list[str]:
    """ALTER an existing table to add any missing shared columns. Returns names added."""
    dialect = "postgresql" if conn.engine.dialect.name == "postgresql" else "sqlite"
    insp = sa.inspect(conn)
    schema = table.schema if dialect == "postgresql" else None
    if not insp.has_table(table.name, schema=schema):
        return []
    existing = {c["name"] for c in insp.get_columns(table.name, schema=schema)}
    added: list[str] = []
    for name in SHARED_COLUMN_NAMES:
        if name in existing:
            continue
        conn.execute(
            sa.text(f"ALTER TABLE {qualified_name(conn, table)} ADD COLUMN {name} {_ADD_COLUMN_SQL[dialect][name]}")
        )
        added.append(name)
    return added
