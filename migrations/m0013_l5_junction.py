# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0013 — HLD v2 §4.5 L5 activities junction.

* ``activities`` + side (business | compliance), action_type, canonical_id
* new edges ``implies`` (product / service -> business activity), ``operates``
  (compliance activity -> block, with obligation refs) and ``mitigates``
  (compliance activity -> business activity, lit by shared obligations)
* curated activities re-seeded with their side and action type; generated
  activities from earlier releases are classified by their name; first
  junction build from the curated table and the live L2–L4 state (no outbox
  event: the nightly fleet re-derives and publishes).
"""
import re

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import activities, implies, mitigates, operates
from app.clhear.platform.shared_schema import ensure_shared_columns, qualified_name

_NEW_COLUMNS = [
    ("side", "TEXT NOT NULL DEFAULT 'compliance'"),
    ("action_type", "TEXT NOT NULL DEFAULT ''"),
    ("canonical_id", "TEXT"),
]

# name cue -> (side, action type) for activities generated before v2
_NAME_CUES = [
    (r"screen|verif|due diligence", ("compliance", "screen")),
    (r"monitor|surveil", ("compliance", "monitor")),
    (r"investigat|high[- ]risk|escalat", ("compliance", "investigate")),
    (r"notif|incident|breach", ("compliance", "notify")),
    (r"report|disclos|file", ("compliance", "report")),
    (r"train|awareness", ("compliance", "train")),
    (r"approv|attest|govern|appoint|sign[- ]off", ("compliance", "attest")),
    (r"assess|review", ("compliance", "assess")),
    (r"record|retain|retention", ("compliance", "record")),
    (r"test|resilien|continuity|recover", ("compliance", "test")),
    (r"onboard|open(?:ing)? (?:an )?account|kyc", ("business", "onboarding")),
    (r"order|execut|trad", ("business", "order_handling")),
    (r"market|promot|advertis", ("business", "marketing")),
    (r"deposit|withdraw|payment|transfer", ("business", "payments")),
    (r"custod|safekeep", ("business", "custody")),
    (r"advi[cs]|portfolio", ("business", "advice")),
    (r"lend|margin", ("business", "lending")),
    (r"personal data|process(?:ing)? data", ("business", "data_processing")),
    (r"outsourc|third[- ]party|vendor|cloud", ("business", "outsourcing")),
]


def classify_name(name: str) -> tuple[str, str]:
    lowered = (name or "").lower()
    for pattern, verdict in _NAME_CUES:
        if re.search(pattern, lowered):
            return verdict
    return "compliance", "control"


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
        conn.execute(text(f"ALTER TABLE {qualified_name(conn, table)} ADD COLUMN {name} {ddl}"))
        added.append(name)
    return added


def upgrade(conn: Connection) -> None:
    activities.create(conn, checkfirst=True)
    ensure_shared_columns(conn, activities)
    _add_missing(conn, activities, _NEW_COLUMNS)
    for table in (implies, operates, mitigates):
        table.create(conn, checkfirst=True)
        ensure_shared_columns(conn, table)

    from app.clhear import curated
    from app.clhear.l1.scopes import active

    # A scoped corpus derives its activities from its own obligations.
    curated_rows = {} if active() else {c["id"]: c for c in curated.load("l5_activities")}
    for item in curated_rows.values():
        exists = conn.execute(sa.select(activities.c.id).where(activities.c.id == item["id"])).first()
        values = dict(
            name=item["name"], description=item.get("description", ""), business_owner=item.get("business_owner", ""),
            triggers=item.get("triggers", []), status="curated", side=item["side"], action_type=item["action_type"],
        )
        if exists:
            conn.execute(activities.update().where(activities.c.id == item["id"]).values(**values))
        else:
            conn.execute(activities.insert().values(id=item["id"], **values))
    for row in conn.execute(sa.select(activities.c.id, activities.c.name, activities.c.action_type)).all():
        if row.id in curated_rows or row.action_type:
            continue
        side, action = classify_name(row.name)
        conn.execute(activities.update().where(activities.c.id == row.id).values(side=side, action_type=action))

    from app.clhear.l5.map import build_junction_in

    build_junction_in(conn, publish=False)
