# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0014 — HLD v2 §4.6 L6 blueprints with items and minimality proof.

* ``blueprints`` + stable_id (BLU-), profile_id, fingerprint, composition, status
* new ``blueprint_items`` (ITM-: block instance with characteristics resolved
  for the profile, obligations satisfied, activities operated, load-bearing)
  and ``minimality_proofs`` (which items are load-bearing for which
  obligations; removal impact)
* legacy request-log rows get a stable id and a fingerprint so their history
  is addressable; they are marked ``superseded`` (their composition was never
  stored) — nothing is deleted
* one current blueprint is composed for every stored valid L4 profile so the
  layer is browsable on a fresh instance (no outbox event: the nightly fleet
  re-derives and publishes)
"""
import json

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import blueprint_items, blueprints, minimality_proofs, profiles
from app.clhear.platform.ids import next_id
from app.clhear.platform.shared_schema import ensure_shared_columns, qualified_name

_NEW_COLUMNS = [
    ("stable_id", "TEXT"),
    ("profile_id", "TEXT"),
    ("fingerprint", "TEXT NOT NULL DEFAULT ''"),
    ("composition", "JSON"),
    ("status", "TEXT NOT NULL DEFAULT 'current'"),
]


def _add_missing(conn: Connection, table: sa.Table, columns: list[tuple[str, str]]) -> list[str]:
    insp = sa.inspect(conn)
    schema = table.schema if conn.engine.dialect.name == "postgresql" else None
    if not insp.has_table(table.name, schema=schema):
        return []
    existing = {c["name"] for c in insp.get_columns(table.name, schema=schema)}
    added = []
    for name, ddl in columns:
        if name in existing:
            continue
        if conn.engine.dialect.name == "postgresql" and ddl == "JSON":
            ddl = "jsonb"
        conn.execute(text(f"ALTER TABLE {qualified_name(conn, table)} ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


def upgrade(conn: Connection) -> None:
    blueprints.create(conn, checkfirst=True)
    ensure_shared_columns(conn, blueprints)
    _add_missing(conn, blueprints, _NEW_COLUMNS)
    for table in (blueprint_items, minimality_proofs):
        table.create(conn, checkfirst=True)
        ensure_shared_columns(conn, table)

    from app.clhear.l6.models import fingerprint

    # legacy request-log rows: addressable, superseded (no stored composition)
    for row in conn.execute(sa.select(blueprints).where(blueprints.c.stable_id.is_(None)).order_by(blueprints.c.id)).mappings():
        profile = row["profile"] if isinstance(row["profile"], dict) else json.loads(row["profile"] or "{}")
        conn.execute(blueprints.update().where(blueprints.c.id == row["id"]).values(
            stable_id=next_id(conn, "BLU"), fingerprint=fingerprint(profile.get("attributes", {}), profile.get("activities")),
            status="superseded",
        ))

    # a current blueprint per stored valid profile
    insp = sa.inspect(conn)
    schema = profiles.schema if conn.engine.dialect.name == "postgresql" else None
    if not insp.has_table(profiles.name, schema=schema):
        return
    from app.clhear.l6.composer import compose_in

    for prow in conn.execute(sa.select(profiles).where(profiles.c.status == "valid").order_by(profiles.c.id)).mappings():
        attrs = prow["attributes"] if isinstance(prow["attributes"], dict) else json.loads(prow["attributes"] or "{}")
        compose_in(conn, {"attributes": attrs, "activities": None, "profile_id": prow["id"]}, requested_by="m0014", release="")
