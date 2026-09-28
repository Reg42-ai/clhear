# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L5 consistency checker (HLD v2 §4.5): no orphan activities.

* a business activity must be implied by at least one live L4 product / service;
* a compliance activity must operate at least one L3 block (that is how it
  traces to an obligation) and trigger at least one anchored obligation;
* every edge endpoint must be live (activity, product, block) and every
  obligation ref on an edge must be a live obligation;
* every activity quotes the duty words it was read from, and every trigger
  ``when`` may only use the L4 applicability language.

Anchors whose clauses are not in the corpus yet are reported (an L1
completeness concern), not counted as orphans.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import implies, mitigates, operates
from app.clhear.l5.map import (
    _anchor_index,
    _live_blocks,
    _live_edges,
    _live_obligations,
    _live_products,
    _ref,
    live_activities,
    resolve_anchor,
)
from app.clhear.l5.models import SIDES

WHEN_KEYS = frozenset({"jurisdictions", "roles", "condition", "fact", "expect"})


def check_junction(engine: Engine) -> dict:
    with engine.connect() as conn:
        acts = live_activities(conn)
        obs = _live_obligations(conn)
        products = _live_products(conn)
        blocks = _live_blocks(conn)
        imp = _live_edges(conn, implies)
        opr = _live_edges(conn, operates)
        mit = _live_edges(conn, mitigates)
    by_id = {a["id"]: a for a in acts}
    index = _anchor_index(obs)
    live_refs = {_ref(o) for o in obs} | {o["id"] for o in obs}

    orphans: list[dict] = []
    dangling: list[dict] = []
    bad_vocab: list[dict] = []
    bad_when: list[dict] = []
    anchors_not_in_corpus: list[dict] = []
    unlit: list[dict] = []

    implied = {e["activity_id"] for e in imp if e["product_id"] in products}
    operating = {e["activity_id"] for e in opr if e["block_id"] in blocks}
    for a in acts:
        if a["side"] not in SIDES or (a.get("status") == "derived" and not (a.get("evidence") or {}).get("name")):
            bad_vocab.append({"activity": a["id"], "side": a["side"], "reason": "not quoted from a duty"})
        resolved = 0
        for t in a["triggers"]:
            when = t.get("when") or {}
            unknown = sorted(k for k in when if k not in WHEN_KEYS)
            if unknown:
                bad_when.append({"activity": a["id"], "unknown_attributes": unknown})
            hits = resolve_anchor(t.get("anchor") or {}, index)
            resolved += len(hits)
            if not hits:
                anchors_not_in_corpus.append({"activity": a["id"], "anchor": t.get("anchor")})
        if a["side"] == "business" and a["id"] not in implied:
            orphans.append({"activity": a["id"], "side": a["side"], "reason": "no product or service implies it"})
        if a["side"] == "compliance":
            if a["id"] not in operating:
                orphans.append({"activity": a["id"], "side": a["side"], "reason": "operates no block"})
            if not a["triggers"]:
                orphans.append({"activity": a["id"], "side": a["side"], "reason": "anchored to no obligation"})
    for e in imp:
        if e["product_id"] not in products:
            dangling.append({"edge": e["id"], "kind": "implies", "missing": e["product_id"]})
        if e["activity_id"] not in by_id or by_id[e["activity_id"]]["side"] != "business":
            dangling.append({"edge": e["id"], "kind": "implies", "missing": e["activity_id"]})
    for e in opr:
        if e["block_id"] not in blocks:
            dangling.append({"edge": e["id"], "kind": "operates", "missing": e["block_id"]})
        if e["activity_id"] not in by_id or by_id[e["activity_id"]]["side"] != "compliance":
            dangling.append({"edge": e["id"], "kind": "operates", "missing": e["activity_id"]})
        for r in e["obligation_refs"]:
            if r not in live_refs:
                dangling.append({"edge": e["id"], "kind": "operates", "missing": r})
    for e in mit:
        c, b = by_id.get(e["compliance_activity_id"]), by_id.get(e["business_activity_id"])
        if c is None or c["side"] != "compliance":
            dangling.append({"edge": e["id"], "kind": "mitigates", "missing": e["compliance_activity_id"]})
        if b is None or b["side"] != "business":
            dangling.append({"edge": e["id"], "kind": "mitigates", "missing": e["business_activity_id"]})
        for r in e["obligation_refs"]:
            if r not in live_refs:
                dangling.append({"edge": e["id"], "kind": "mitigates", "missing": r})
        if not e["obligation_refs"]:
            unlit.append({"edge": e["id"], "compliance": e["compliance_activity_id"], "business": e["business_activity_id"]})

    total = len(acts)
    orphan_ids = {o["activity"] for o in orphans}
    return {
        "activities": total,
        "business": sum(1 for a in acts if a["side"] == "business"),
        "compliance": sum(1 for a in acts if a["side"] == "compliance"),
        "edges": {"implies": len(imp), "operates": len(opr), "mitigates": len(mit)},
        "orphans": orphans,
        "dangling": dangling,
        "vocabulary_violations": bad_vocab,
        "when_violations": bad_when,
        "anchors_not_in_corpus": anchors_not_in_corpus,
        "unlit_mitigates": unlit,
        "completeness": ((total - len(orphan_ids)) / total) if total else None,
        "ok": total > 0 and not orphans and not dangling and not bad_vocab and not bad_when,
    }
