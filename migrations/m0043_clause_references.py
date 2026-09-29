# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0043 — cross-references between texts, and the publisher's reference for a source.

* ``l1_sources.clause_references``: every mention a clause makes of another text
  or provision, quoted with offsets (``app.clhear.l1.references``);
* ``host_sources.reference``: the publisher's own reference for a registered
  text, so a clause that cites it by that reference resolves to it;
* the references of every in-force version already stored are recorded now: a
  later import of unchanged text does not store the version again.
"""
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.hoststore import host_sources
from app.clhear.l1.models import clause_references, source_versions, sources
from app.clhear.platform.shared_schema import qualified_name


def _schema(conn: Connection, table: sa.Table):
    return table.schema if conn.dialect.name == "postgresql" else None


def _add_reference(conn: Connection) -> None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(host_sources.name, schema=_schema(conn, host_sources)):
        return
    existing = {c["name"] for c in inspector.get_columns(host_sources.name, schema=_schema(conn, host_sources))}
    if "reference" not in existing:
        conn.execute(text(f"ALTER TABLE {qualified_name(conn, host_sources)} ADD COLUMN reference TEXT NOT NULL DEFAULT ''"))


def _backfill(conn: Connection) -> None:
    from app.clhear.l1 import permissions, references

    rows = conn.execute(sa.select(source_versions.c.id, sources.c.key, sources.c.adapter, sources.c.license,
                                  sources.c.canonical_url)
                        .join(sources, sources.c.id == source_versions.c.source_id)
                        .where(source_versions.c.status == "in_force")).mappings().all()
    for row in rows:
        meta = {"source_key": row["key"], "adapter": row["adapter"], "license": row["license"],
                "canonical_url": row["canonical_url"]}
        if permissions.required_for(meta) and not permissions.decision(conn, row["key"], "derive")["allowed"]:
            continue
        references.record_version(conn, row["id"])


def upgrade(conn: Connection) -> None:
    clause_references.create(conn, checkfirst=True)
    _add_reference(conn)
    _backfill(conn)
