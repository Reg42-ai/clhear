# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L6 checks — independent verification of a composition (HLD v2 §4.6 gates).

``verify_minimality`` re-derives, from the coverage matrix alone, whether any
selected item can be removed without breaking coverage and compares that
with the composer's own proof. ``check_blueprint`` runs the structural
invariants a blueprint must hold (every covered obligation satisfied by an
item that is in the program, every item satisfying ≥ 1 obligation, required
blocks present, ids closed-world).
"""
from __future__ import annotations

from app.clhear.l6.models import ITEM_BASES


def verify_minimality(composition: dict) -> dict:
    """Try removing each selected item: if coverage survives, it is redundant."""
    items = composition.get("items") or []
    coverage = composition.get("coverage") or []
    program = {i["block_id"] for i in items}
    required = {b for c in coverage for b in c.get("required_blocks") or []}
    redundant: list[str] = []
    for it in items:
        bid = it["block_id"]
        if bid in required:
            continue
        without = program - {bid}
        survives = all(
            c["state"] == "gap" or any(b in without for b in c.get("covered_by") or [])
            for c in coverage
        )
        if survives:
            redundant.append(bid)
    claimed = set((composition.get("minimality") or {}).get("redundant") or [])
    return {
        "minimal": not redundant,
        "redundant": sorted(redundant),
        "proof_agrees": set(redundant) == claimed and bool((composition.get("minimality") or {}).get("checked")),
        "items": len(items),
        "required": len(required & program),
    }


def check_blueprint(composition: dict) -> dict:
    items = composition.get("items") or []
    coverage = composition.get("coverage") or []
    program = {i["block_id"] for i in items}
    problems: list[dict] = []
    for c in coverage:
        if c["state"] == "covered":
            if not c.get("satisfied_by"):
                problems.append({"kind": "covered_without_item", "obligation_id": c["obligation_id"]})
            elif not set(c["satisfied_by"]) <= program:
                problems.append({"kind": "satisfier_not_in_program", "obligation_id": c["obligation_id"]})
        for b in c.get("required_blocks") or []:
            if b not in program:
                problems.append({"kind": "required_block_missing", "obligation_id": c["obligation_id"], "block_id": b})
    known = {c["obligation_id"] for c in coverage}
    for it in items:
        if not it.get("obligations_satisfied"):
            problems.append({"kind": "item_without_obligation", "block_id": it["block_id"]})
        if it.get("basis") not in ITEM_BASES:
            problems.append({"kind": "bad_basis", "block_id": it["block_id"], "basis": it.get("basis")})
        unknown = [o for o in it.get("obligations_satisfied") or [] if o not in known]
        if unknown:
            problems.append({"kind": "item_cites_unknown_obligation", "block_id": it["block_id"], "obligations": unknown[:5]})
        if not it.get("explanation"):
            problems.append({"kind": "item_without_explanation", "block_id": it["block_id"]})
    minimality = verify_minimality(composition)
    gaps = [c["obligation_id"] for c in coverage if c["state"] == "gap"]
    return {
        "ok": not problems and minimality["minimal"] and minimality["proof_agrees"],
        "problems": problems,
        "gaps": gaps,
        "complete": not gaps and bool(coverage),
        "minimality": minimality,
        "items": len(items),
        "obligations": len(coverage),
    }
