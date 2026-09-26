# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Rows of one named scope that a snapshot may carry out of a larger database.

A scoped build writes L2–L8 only for the active scope. A release copies
that slice: obligations, blocks, the licence they ground, stored profiles
and their current blueprints, activities, scores, and the scope's reference
keys. A row that names any source outside the scope stays behind. When the
database has none of this slice, the caller keeps an L1-only snapshot.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.l1 import scopes

def project(conn: Connection, scope_name: str | None = None) -> dict | None:
    """The scope's derived rows, or None when that scope has not been built here."""
    scope_name = scope_name or scopes.active_name()
    if not scope_name:
        return None
    chosen = frozenset(scopes.source_keys(scope_name))
    if not chosen:
        return None
    from app.clhear import derived_models as d
    from app.clhear import layer_builds
    from app.clhear.l7 import models as l7

    obligations = _load(conn, d.obligations, d.obligations.c.source_key.in_(sorted(chosen)),
                        d.obligations.c.status.in_(("derived", "validated")))
    ob_ids = {row["id"] for row in obligations}
    license_types = [row for row in _load(conn, d.license_types) if _anchored(row, chosen)]
    type_ids = {row["id"] for row in license_types}
    licences = [row for row in _load(conn, d.licences) if row["id"] in type_ids or _anchored(row, chosen)]
    licence_ids = {row["id"] for row in licences}
    events = _load(conn, l7.enforcement_events, l7.enforcement_events.c.source_key.in_(sorted(chosen)))
    profiles = _load(conn, d.profiles)
    if not obligations and not license_types and not events and not profiles:
        return None

    asserts = _load(conn, d.asserts, d.asserts.c.obligation_id.in_(ob_ids or {""}))
    applies = _load(conn, d.applies_to, d.applies_to.c.obligation_id.in_(ob_ids or {""}))
    reviews = _load(conn, d.obligation_reviews, d.obligation_reviews.c.obligation_id.in_(ob_ids or {""}))
    changes = _load(conn, d.l2_change_events, d.l2_change_events.c.obligation_id.in_(ob_ids or {""}))
    equivalences = [row for row in _load(conn, d.equivalences)
                    if row["obligation_a"] in ob_ids and row["obligation_b"] in ob_ids]
    supersessions = [row for row in _load(conn, d.supersessions)
                     if row["old_obligation_id"] in ob_ids and row["new_obligation_id"] in ob_ids]
    members = [row for row in _load(conn, d.concept_members) if row["obligation_id"] in ob_ids]
    by_concept: dict[str, list] = {}
    for row in members:
        by_concept.setdefault(row["concept_id"], []).append(row)
    concept_ids = set()
    kept_members = []
    if by_concept:
        all_members = _load(conn, d.concept_members, d.concept_members.c.concept_id.in_(sorted(by_concept)))
        outside = {row["concept_id"] for row in all_members if row["obligation_id"] not in ob_ids}
        concept_ids = set(by_concept) - outside
        kept_members = [row for row in members if row["concept_id"] in concept_ids]
    concepts = _load(conn, d.concepts, d.concepts.c.id.in_(concept_ids or {""}))

    block_ids = _block_ids(conn, d, chosen)
    blocks = _load(conn, d.blocks, d.blocks.c.id.in_(block_ids or {""}))
    requires = _load(conn, d.requires, d.requires.c.block_id.in_(block_ids or {""}))
    characteristics = _load(conn, d.characteristics, d.characteristics.c.block_id.in_(block_ids or {""}))

    profile_ids = {row["id"] for row in profiles}
    blueprints = [row for row in _load(conn, d.blueprints)
                  if row.get("profile_id") in profile_ids and row.get("status") == "current"]
    blueprint_ids = {row["stable_id"] for row in blueprints if row.get("stable_id")}
    items = _load(conn, d.blueprint_items, d.blueprint_items.c.blueprint_id.in_(blueprint_ids or {""}))
    item_ids = {row["id"] for row in items}
    proofs = _load(conn, d.minimality_proofs, d.minimality_proofs.c.blueprint_id.in_(blueprint_ids or {""}))

    activities = []
    for row in _load(conn, d.activities):
        triggers = [t for t in _as_list(row.get("triggers")) if _trigger_in(t, chosen, ob_ids)]
        if not triggers:
            continue
        copied = dict(row)
        copied["triggers"] = triggers
        activities.append(copied)
    activity_ids = {row["id"] for row in activities}
    implies = _load(conn, d.implies, d.implies.c.activity_id.in_(activity_ids or {""}))
    operates = []
    for row in _load(conn, d.operates, d.operates.c.activity_id.in_(activity_ids or {""})):
        if row["block_id"] not in block_ids:
            continue
        copied = dict(row)
        copied["obligation_refs"] = [ref for ref in _as_list(row.get("obligation_refs")) if ref in ob_ids]
        operates.append(copied)
    mitigates = []
    for row in _load(conn, d.mitigates):
        if row["compliance_activity_id"] not in activity_ids or row["business_activity_id"] not in activity_ids:
            continue
        copied = dict(row)
        copied["obligation_refs"] = [ref for ref in _as_list(row.get("obligation_refs")) if ref in ob_ids]
        mitigates.append(copied)

    scores = []
    for row in _load(conn, l7.risk_scores, l7.risk_scores.c.status == "current"):
        if row["subject_kind"] == "obligation" and row["subject_ref"] in ob_ids:
            scores.append(row)
        elif row["subject_kind"] == "item" and row["subject_ref"] in item_ids:
            scores.append(row)
    event_ids = {row["id"] for row in events}
    links = [row for row in _load(conn, l7.enforcement_links) if row["event_id"] in event_ids and row["obligation_id"] in ob_ids]
    calibrations = _load(conn, l7.risk_calibrations, l7.risk_calibrations.c.published.is_(True))
    builds = _load(conn, layer_builds.layer_builds, layer_builds.layer_builds.c.scope == scope_name)

    tables = [
        (d.obligations, obligations), (d.asserts, asserts), (d.equivalences, equivalences),
        (d.supersessions, supersessions), (d.l2_change_events, changes), (d.obligation_reviews, reviews),
        (d.concepts, concepts), (d.concept_members, kept_members),
        (d.blocks, blocks), (d.requires, requires), (d.characteristics, characteristics),
        (d.l3_kinds, _load(conn, d.l3_kinds)),
        (d.attribute_schema, _load(conn, d.attribute_schema)),
        (d.license_types, license_types), (d.licences, licences),
        (d.products_services, _load(conn, d.products_services)),
        (d.client_types, _load(conn, d.client_types)), (d.channels, _load(conn, d.channels)),
        (d.validity_rules, _load(conn, d.validity_rules)),
        (d.permits, _load(conn, d.permits, d.permits.c.licence_id.in_(licence_ids or {""}))),
        (d.profiles, profiles), (d.sample_profiles, _load(conn, d.sample_profiles)),
        (d.applies_to, applies),
        (d.activities, activities), (d.implies, implies), (d.operates, operates), (d.mitigates, mitigates),
        (d.blueprints, blueprints), (d.blueprint_items, items), (d.minimality_proofs, proofs),
        (l7.enforcement_events, events), (l7.enforcement_links, links),
        (l7.risk_scores, scores), (l7.risk_calibrations, calibrations),
        (layer_builds.layer_builds, builds),
    ]
    present = set()
    if obligations:
        present.add("L2")
    if blocks:
        present.add("L3")
    if profiles or licences or license_types:
        present.add("L4")
    if activities:
        present.add("L5")
    if blueprints:
        present.add("L6")
    if scores or events:
        present.add("L7")
    present.add("L8")
    return {
        "tables": [(table, rows) for table, rows in tables if rows],
        "omitted_layers": [f"L{n}" for n in range(2, 9) if f"L{n}" not in present],
        "derived_scope": {"name": scope_name, "reference": list(scopes.role("reference", scope_name))},
    }


def _load(conn: Connection, table, *criteria) -> list[dict]:
    query = sa.select(table)
    preds = list(criteria)
    if "valid_to" in table.c:
        preds.append(table.c.valid_to.is_(None))
    if preds:
        query = query.where(*preds)
    return [dict(row) for row in conn.execute(query).mappings()]


def _as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return list(value) if isinstance(value, list) else []


def _anchored(row: dict, chosen: frozenset[str]) -> bool:
    anchors = _as_list(row.get("clause_anchors"))
    sources = {anchor.get("source_key") for anchor in anchors if isinstance(anchor, dict) and anchor.get("source_key")}
    return bool(sources) and sources <= chosen


def _trigger_in(trigger: dict, chosen: frozenset[str], ob_ids: set[str]) -> bool:
    if not isinstance(trigger, dict):
        return False
    anchor = trigger.get("anchor") or {}
    if isinstance(anchor, dict) and anchor.get("source_key") in chosen:
        return True
    obligation = trigger.get("obligation")
    if isinstance(obligation, dict):
        return obligation.get("id") in ob_ids or obligation.get("source_key") in chosen
    return isinstance(obligation, str) and obligation in ob_ids


def _block_ids(conn: Connection, models, chosen: frozenset[str]) -> set[str]:
    """Blocks whose live requires edges and satisfies selectors stay inside the scope."""
    linked: dict[str, set[str]] = {}
    rows = conn.execute(
        sa.select(models.requires.c.block_id, models.obligations.c.source_key)
        .join(models.obligations, models.obligations.c.id == models.requires.c.obligation_id)
        .where(models.requires.c.valid_to.is_(None))
    )
    for block_id, source_key in rows:
        linked.setdefault(block_id, set()).add(source_key)
    allowed = {block_id for block_id, sources in linked.items() if sources and sources <= chosen}
    for row in conn.execute(sa.select(models.blocks.c.id, models.blocks.c.satisfies).where(models.blocks.c.valid_to.is_(None))):
        if row.id in linked:
            continue
        selectors = _as_list(row.satisfies)
        sources = {item.get("source_key") for item in selectors if isinstance(item, dict) and item.get("source_key")}
        if sources and sources <= chosen:
            allowed.add(row.id)
    return allowed
