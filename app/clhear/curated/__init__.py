# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Curated catalog: L3 building blocks, L5 activities, L4 attribute schema +
sample profiles, L8 benchmark definitions.

CURATED means human-authored policy content — not derived, not demo. It is
seeded from these reviewed JSON files into the derived-layer tables and from
then on changes only through the L0 proposals queue. Obligation references
are ANCHORS ({source_key, refs[]}) that resolve to machine-derived L2 rows at
read time, so curated mappings always point at the live registry.
"""
import json
from functools import lru_cache
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import activities, attribute_schema, blocks, sample_profiles
from app.clhear.platform import audit

CURATED_DIR = Path(__file__).parent
_SEED_ACTOR = audit.Actor(actor="system:curated-seed", kind="system")


def _audited(conn, table, row_id: str, created: bool) -> None:
    """The catalog bypasses record.write (it is authored, not derived) but still owes
    the audit log one entry per row it touches (HLD v2 §7.1)."""
    audit.log(conn, "write" if created else "update", resource=f"{table.schema}.{table.name}", resource_id=row_id,
              detail={"source": "curated catalog", "status": "curated"}, actor=audit.current_actor() if audit.current_actor().kind != "anonymous" else _SEED_ACTOR)


# Sample profiles and member benchmarks are not part of this distribution.
_OPTIONAL = frozenset({"l4_sample_profiles", "l8_benchmarks"})


@lru_cache
def load(name: str) -> list[dict]:
    path = CURATED_DIR / f"{name}.json"
    if not path.is_file():
        if name in _OPTIONAL:
            return []
        raise FileNotFoundError(path)
    return json.loads(path.read_text())


def load_object(name: str) -> dict:
    return json.loads((CURATED_DIR / f"{name}.json").read_text())


def seed(engine: Engine) -> dict:
    """Idempotent upsert of the curated catalog into the DB."""
    counts = {"blocks": 0, "activities": 0, "attributes": 0, "profiles": 0}
    with engine.begin() as conn:
        for item in load("l3_building_blocks"):
            exists = conn.execute(sa.select(blocks.c.id).where(blocks.c.id == item["id"])).first()
            values = dict(
                name=item["name"], description=item.get("description", ""),
                capability=item.get("capability", ""),
                evidence_artifacts=item.get("evidence_artifacts", []),
                satisfies=item.get("satisfies", []),
                implements_controls=item.get("implements_controls", []),
                status="curated",
                kind=item.get("kind", "Process"),
                purpose=item.get("purpose", ""),
            )
            if exists:
                conn.execute(blocks.update().where(blocks.c.id == item["id"]).values(**values))
            else:
                conn.execute(blocks.insert().values(id=item["id"], **values))
            _audited(conn, blocks, item["id"], created=not exists)
            counts["blocks"] += 1
        from app.clhear.derived_models import l3_kinds
        from app.clhear.l3.kinds import kinds_catalog

        for entry in kinds_catalog():
            exists = conn.execute(sa.select(l3_kinds.c.kind).where(l3_kinds.c.kind == entry["kind"])).first()
            values = dict(description=entry["description"], fields=entry["fields"])
            if exists:
                conn.execute(l3_kinds.update().where(l3_kinds.c.kind == entry["kind"]).values(**values))
            else:
                conn.execute(l3_kinds.insert().values(kind=entry["kind"], **values))
        for item in load("l5_activities"):
            exists = conn.execute(sa.select(activities.c.id).where(activities.c.id == item["id"])).first()
            values = dict(
                name=item["name"], description=item.get("description", ""),
                business_owner=item.get("business_owner", ""),
                triggers=item.get("triggers", []), status="curated",
                side=item.get("side", "compliance"), action_type=item.get("action_type", ""),
            )
            if exists:
                conn.execute(activities.update().where(activities.c.id == item["id"]).values(**values))
            else:
                conn.execute(activities.insert().values(id=item["id"], **values))
            _audited(conn, activities, item["id"], created=not exists)
            counts["activities"] += 1
        for item in load("l4_attribute_schema"):
            exists = conn.execute(
                sa.select(attribute_schema.c.key).where(attribute_schema.c.key == item["key"])
            ).first()
            values = dict(type=item["type"], description=item.get("description", ""), read_by=item.get("read_by", []))
            if exists:
                conn.execute(attribute_schema.update().where(attribute_schema.c.key == item["key"]).values(**values))
            else:
                conn.execute(attribute_schema.insert().values(key=item["key"], **values))
            _audited(conn, attribute_schema, item["key"], created=not exists)
            counts["attributes"] += 1
        for item in load("l4_sample_profiles"):
            exists = conn.execute(
                sa.select(sample_profiles.c.id).where(sample_profiles.c.id == item["id"])
            ).first()
            values = dict(
                name=item["name"], description=item.get("description", ""),
                attributes=item.get("attributes", {}), activities=item.get("activities", []),
                status="sample",
            )
            if exists:
                conn.execute(sample_profiles.update().where(sample_profiles.c.id == item["id"]).values(**values))
            else:
                conn.execute(sample_profiles.insert().values(id=item["id"], **values))
            _audited(conn, sample_profiles, item["id"], created=not exists)
            counts["profiles"] += 1
    # HLD v2 §4.5: the junction edges derive from the catalog just seeded (no outbox event at startup).
    from app.clhear.l5.map import build_junction

    counts["junction_edges"] = build_junction(engine, publish=False)["added"]
    return counts


def seed_data_model(engine: Engine) -> dict:
    """Only the product's data model: the L3 block kinds and the L4 profile
    attributes. No curated blocks, activities, profiles or concepts — a scoped
    corpus derives those from its own sources."""
    from app.clhear.derived_models import l3_kinds
    from app.clhear.l3.kinds import kinds_catalog

    counts = {"kinds": 0, "attributes": 0}
    with engine.begin() as conn:
        for entry in kinds_catalog():
            exists = conn.execute(sa.select(l3_kinds.c.kind).where(l3_kinds.c.kind == entry["kind"])).first()
            values = dict(description=entry["description"], fields=entry["fields"])
            if exists:
                conn.execute(l3_kinds.update().where(l3_kinds.c.kind == entry["kind"]).values(**values))
            else:
                conn.execute(l3_kinds.insert().values(kind=entry["kind"], **values))
            counts["kinds"] += 1
        for item in load("l4_attribute_schema"):
            exists = conn.execute(sa.select(attribute_schema.c.key).where(attribute_schema.c.key == item["key"])).first()
            values = dict(type=item["type"], description=item.get("description", ""), read_by=[])
            if exists:
                conn.execute(attribute_schema.update().where(attribute_schema.c.key == item["key"]).values(**values))
            else:
                conn.execute(attribute_schema.insert().values(key=item["key"], **values))
            _audited(conn, attribute_schema, item["key"], created=not exists)
            counts["attributes"] += 1
    return counts


def seed_concepts(engine: Engine) -> dict:
    """Seed the starter concept set AFTER extraction has run.

    Create-only: existing concepts are never overwritten (maintainer edits and
    gateway-drafted approvals win over the seed file). Members that don't
    resolve to a derived obligation are skipped by upsert_concept, so the seed
    naturally fills in as the corpus grows."""
    from app.clhear.derived_models import concepts as concepts_t
    from app.clhear.l2.concepts import upsert_concept

    written = skipped_existing = 0
    missing: list[str] = []
    with engine.connect() as conn:
        existing = {row.id for row in conn.execute(sa.select(concepts_t.c.id))}
    for item in load("l2_concepts"):
        if item["id"] in existing:
            skipped_existing += 1
            continue
        result = upsert_concept(
            engine,
            concept_id=item["id"],
            name=item["name"],
            canonical_statement=item["canonical_statement"],
            themes=item.get("themes", []),
            members=item["members"],
            status="curated",
            drafted_by="human",
            approved_by="seed:reviewed-catalog",
        )
        if result.get("written"):
            written += 1
        missing.extend(result.get("missing_members", []))
    return {"concepts_written": written, "concepts_existing": skipped_existing, "missing_members": missing}
