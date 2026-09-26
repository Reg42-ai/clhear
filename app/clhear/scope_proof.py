# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Proof that a scoped corpus builds a real compliance program.

Checks, per layer, that every derived row traces to the layer below it, and
per compliance-program component (``scopes.json``) that the component is
covered from its own sources down to verified L1 text. Golden expectations,
when a reviewer has written them (``clhear-evals/scope/<scope>.json``), are
scored here and never read by any derivation.
"""
from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l1 import scopes

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "clhear-evals" / "scope"
LIVE = ("derived", "validated")


def _check(name: str, passed: bool, **detail) -> dict:
    return {"check": name, "passed": bool(passed), **detail}


def _in_force(conn) -> dict[str, dict]:
    from app.clhear.l1.models import clauses, source_versions, sources

    out: dict[str, dict] = {}
    for r in conn.execute(sa.select(sources.c.key, clauses.c.id, clauses.c.text_hash)
                          .join(source_versions, source_versions.c.source_id == sources.c.id)
                          .join(clauses, clauses.c.source_version_id == source_versions.c.id)
                          .where(source_versions.c.status == "in_force")):
        out.setdefault(r.key, {})[r.id] = r.text_hash
    return out


def run(engine: Engine, scope_name: str) -> dict:
    from app.clhear import derived_models as d
    from app.clhear.l7 import models as l7
    from app.clhear.l8.reference import derived_reference_rows

    scope = scopes.get(scope_name)
    with engine.connect() as conn:
        clauses_by_source = _in_force(conn)
        clause_hash = {cid: h for per in clauses_by_source.values() for cid, h in per.items()}
        obligations = {r.id: dict(r._mapping) for r in conn.execute(sa.select(d.obligations).where(d.obligations.c.status.in_(LIVE)))}
        asserts = [dict(r._mapping) for r in conn.execute(sa.select(d.asserts))]
        requires = [dict(r._mapping) for r in conn.execute(sa.select(d.requires).where(d.requires.c.valid_to.is_(None)))]
        blocks = {r.id for r in conn.execute(sa.select(d.blocks.c.id).where(d.blocks.c.valid_to.is_(None)))}
        applies = {r.obligation_id for r in conn.execute(sa.select(d.applies_to.c.obligation_id).where(d.applies_to.c.valid_to.is_(None)))}
        activities = [dict(r._mapping) for r in conn.execute(sa.select(d.activities).where(d.activities.c.valid_to.is_(None)))]
        items = [dict(r._mapping) for r in conn.execute(
            sa.select(d.blueprint_items).join(d.blueprints, d.blueprints.c.stable_id == d.blueprint_items.c.blueprint_id)
            .where(d.blueprints.c.status == "current", d.blueprint_items.c.valid_to.is_(None)))]
        events = [dict(r._mapping) for r in conn.execute(sa.select(l7.enforcement_events).where(l7.enforcement_events.c.valid_to.is_(None)))]
        links = [dict(r._mapping) for r in conn.execute(sa.select(l7.enforcement_links).where(l7.enforcement_links.c.valid_to.is_(None)))]
        references = derived_reference_rows(conn)

    def triggered(activity) -> set[str]:
        raw = activity.get("triggers") or []
        raw = json.loads(raw) if isinstance(raw, str) else raw
        return {t.get("obligation") for t in raw if isinstance(t, dict) and t.get("obligation")}

    grounded = {a["obligation_id"] for a in asserts if clause_hash.get(a["clause_id"]) is not None}
    required = {}
    for r in requires:
        required.setdefault(r["obligation_id"], set()).add(r["block_id"])
    obligation_ids = set(obligations)
    stable = {o.get("stable_id"): oid for oid, o in obligations.items() if o.get("stable_id")}
    mapped = {stable.get(ref, ref) for a in activities for ref in triggered(a)}

    layers = {
        "L1": [_check("every scoped source is in force", set(scope["sources"]) <= set(clauses_by_source),
                      missing=sorted(set(scope["sources"]) - set(clauses_by_source)))],
        "L2": [_check("obligations exist", bool(obligations), count=len(obligations)),
               _check("every obligation asserts an in-force clause", obligation_ids <= grounded,
                      ungrounded=sorted(obligation_ids - grounded)[:20])],
        "L3": [_check("every obligation requires a block", obligation_ids <= set(required),
                      unrequired=sorted(obligation_ids - set(required))[:20]),
               _check("every required block exists", all(b in blocks for bs in required.values() for b in bs))],
        "L4": [_check("every obligation has an applicability edge", obligation_ids <= applies,
                      unscoped=sorted(obligation_ids - applies)[:20])],
        "L5": [_check("every obligation is operated by an activity", obligation_ids <= mapped,
                      unmapped=sorted(obligation_ids - mapped)[:20])],
    }
    orphan_items = []
    for item in items:
        satisfied = item["obligations_satisfied"]
        satisfied = json.loads(satisfied) if isinstance(satisfied, str) else (satisfied or [])
        resolved = {stable.get(o, o) for o in satisfied}
        if item["block_id"] not in blocks or not resolved or not resolved <= grounded:
            orphan_items.append(item["id"])
    layers["L6"] = [_check("every blueprint item traces to verified L1 text", not orphan_items, items=len(items),
                           orphans=orphan_items[:20])]
    event_ids = {e["id"] for e in events}
    layers["L7"] = [_check("every event reads an in-force clause", all(clause_hash.get(e["clause_id"]) for e in events),
                           events=len(events)),
                    _check("every link joins a live event to a live obligation",
                           all(lk["event_id"] in event_ids and stable.get(lk["obligation_id"], lk["obligation_id"]) in obligation_ids
                               for lk in links), links=len(links))]
    layers["L8"] = [_check("every reference row quotes an in-force clause",
                           all(clause_hash.get(r["source"]["clause_id"]) for r in references), rows=len(references))]

    components = {}
    enforcement = set(scope.get("roles", {}).get("enforcement", ()))
    reference = set(scope.get("roles", {}).get("reference", ()))
    for component, keys in scope["components"].items():
        keys = set(keys)
        in_force = keys <= set(clauses_by_source)
        own = {oid for oid, o in obligations.items() if o["source_key"] in keys}
        if own:
            covered = all(oid in required and oid in mapped for oid in own)
            detail = {"obligations": len(own), "with_block_and_activity": sum(1 for o in own if o in required and o in mapped)}
        elif keys <= enforcement:
            covered = any(e["source_key"] in keys for e in events)
            detail = {"events": sum(1 for e in events if e["source_key"] in keys)}
        elif keys & reference:
            covered = any(r["source"]["source_key"] in keys for r in references)
            detail = {"reference_rows": sum(1 for r in references if r["source"]["source_key"] in keys)}
        else:
            covered, detail = in_force, {"interpretation_only": True}
        components[component] = {"passed": in_force and covered, "in_force": in_force, **detail}

    golden_path = GOLDEN_DIR / f"{scope_name}.json"
    golden = {"status": "awaiting_reviewer"}
    if golden_path.exists():
        expected = json.loads(golden_path.read_text())
        wanted = set(expected.get("obligations", []))
        if wanted:
            found = wanted & obligation_ids
            golden = {"status": "scored", "expected": len(wanted), "found": len(found),
                      "recall": round(len(found) / len(wanted), 4), "missing": sorted(wanted - found)}
    return {"scope": scope_name,
            "layers": {k: {"passed": all(c["passed"] for c in v), "checks": v} for k, v in layers.items()},
            "components": components, "golden": golden}
