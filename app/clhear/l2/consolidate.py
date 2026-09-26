# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Nightly consolidation: auto-apply cross-jurisdiction concept groupings.

Deterministic blocking finds candidate groups; the router drafts the
representative name + canonical statement. Concepts go live immediately as
`ai_generated` (closed-world OBL: members + n-gram restricted guard). A
proposal is recorded already-applied for the audit trail.
"""
from __future__ import annotations

import json
import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.platform import record
from app.clhear.derived_models import obligations
from app.clhear.l2.concepts import consolidated_ids, upsert_concept
from app.clhear.models import proposals as proposals_t
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l2.consolidate")

MAX_CANDIDATES_PER_RUN = 5
MIN_SIMILARITY = 0.30
MAX_GROUP = 6

_STOP = frozenset(
    "a an and are as at be by for from has have if in is it its may must not of on or "
    "shall should such that the their there this to under where which with within".split()
)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in re.findall(r"[a-z]{3,}", text.lower()) if t not in _STOP)


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_candidates(engine: Engine, limit: int = MAX_CANDIDATES_PER_RUN) -> list[list[dict]]:
    """Cross-jurisdiction groups of unconsolidated, live obligations."""
    done = consolidated_ids(engine)
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sa.select(
                    obligations.c.id, obligations.c.title, obligations.c.statement,
                    obligations.c.jurisdiction, obligations.c.themes, obligations.c.source_key,
                ).where(obligations.c.status.in_(("derived", "validated")))
            ).mappings()
        ]
    from app.clhear.l1.scopes import in_scope

    rows = [r for r in rows if r["id"] not in done and r["jurisdiction"] and in_scope(r["source_key"])]
    for r in rows:
        r["_tok"] = _tokens(f"{r['title']} {r['statement']}")
        r["_themes"] = set(r["themes"] if isinstance(r["themes"], list) else [])

    groups: list[list[dict]] = []
    used: set[str] = set()
    # Deterministic order: strongest anchors first (longer statements = richer signal).
    for seed in sorted(rows, key=lambda r: (-len(r["_tok"]), r["id"])):
        if seed["id"] in used or len(groups) >= limit:
            continue
        group = [seed]
        for other in sorted(rows, key=lambda r: r["id"]):
            if other["id"] in used or other["id"] == seed["id"] or len(group) >= MAX_GROUP:
                continue
            if other["jurisdiction"] == seed["jurisdiction"]:
                continue  # consolidation is cross-jurisdiction by definition
            if seed["_themes"] and other["_themes"] and not (seed["_themes"] & other["_themes"]):
                continue
            if _jaccard(seed["_tok"], other["_tok"]) >= MIN_SIMILARITY:
                group.append(other)
        if len({g["jurisdiction"] for g in group}) >= 2:
            groups.append(group)
            used.update(g["id"] for g in group)
    return groups


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _already_proposed(engine: Engine, member_ids: list[str]) -> bool:
    wanted = set(member_ids)
    with engine.connect() as conn:
        for row in conn.execute(sa.select(proposals_t.c.draft).where(proposals_t.c.kind == "l2_concept")):
            draft = row.draft if isinstance(row.draft, dict) else json.loads(row.draft or "{}")
            if set(m["obligation_id"] for m in draft.get("members", [])) & wanted:
                return True
        from app.clhear.derived_models import concept_members

        live = {
            r.obligation_id
            for r in conn.execute(
                sa.select(concept_members.c.obligation_id).where(
                    concept_members.c.obligation_id.in_(member_ids), record.in_force(concept_members)
                )
            )
        }
        if live & wanted:
            return True
    return False


def _closed_world_members(group: list[dict], notes: dict[str, str]) -> list[dict]:
    """Membership is closed-world: only existing OBL: ids, never invented."""
    members = []
    for g in group:
        oid = g["id"]
        if not str(oid).startswith("OBL:"):
            continue
        members.append({
            "obligation_id": oid, "jurisdiction": g["jurisdiction"], "role": "primary",
            "note": notes.get(oid, ""),
        })
    return members


def draft_and_propose(engine: Engine, llm, limit: int = MAX_CANDIDATES_PER_RUN) -> dict:
    """Auto-apply consolidations as ai_generated. `llm` is a Router or Gateway."""
    proposed = skipped = drafted_llm = applied = 0
    concept_ids: list[str] = []
    for group in find_candidates(engine, limit=limit):
        member_ids = [g["id"] for g in group]
        if _already_proposed(engine, member_ids):
            skipped += 1
            continue
        name = group[0]["title"]
        canonical = ""
        notes: dict[str, str] = {}
        model = ""
        routing = "structure-only (no LLM)"
        if llm is not None:
            try:
                prompt = (
                    "These regulatory obligations from different jurisdictions appear to impose the "
                    "same underlying duty. Draft ONE representative consolidation. Respond with JSON: "
                    '{"name": <=90 chars imperative title, "canonical_statement": <=350 chars neutral '
                    "restatement that is NOT verbatim from any text, \"member_notes\": {obligation_id: "
                    "<=80 chars on what this jurisdiction adds}}. Member keys MUST be ids from the list.\n\n"
                    + "\n\n".join(
                        f"[{g['id']}] ({g['jurisdiction']}, {g['source_key']})\n{g['title']}\n{g['statement'][:400]}"
                        for g in group
                    )
                )
                result = complete(
                    llm, "l2.consolidate",
                    prompt=prompt,
                    system="You consolidate legal obligations. JSON only. Never copy sentences verbatim. Never invent obligation ids.",
                    required_keys=["name", "canonical_statement", "member_notes"],
                    max_tokens=700,
                )
                parsed = parse_json_object(result.text)
                name = str(parsed["name"])[:120]
                canonical = str(parsed["canonical_statement"])[:500]
                raw_notes = {str(k): str(v)[:120] for k, v in dict(parsed.get("member_notes", {})).items()}
                known = set(member_ids)
                notes = {k: v for k, v in raw_notes.items() if k in known}
                drafted_llm += 1
                model = result.model
                routing = getattr(result, "provider", "") + f" {model}"
            except Exception:
                log.exception("router drafting failed; applying structure-only candidate")
        members = _closed_world_members(group, notes)
        if len(members) < 2:
            skipped += 1
            continue
        concept_id = f"CON:{_slug(name)}"
        draft = {
            "id": concept_id,
            "name": name,
            "canonical_statement": canonical,
            "themes": sorted({t for g in group for t in (g["themes"] if isinstance(g["themes"], list) else [])}),
            "drafted_by": "gateway" if canonical else "candidate-only",
            "members": members,
        }
        written = upsert_concept(
            engine,
            concept_id=concept_id,
            name=name,
            canonical_statement=canonical,
            themes=draft["themes"],
            members=members,
            status="curated",
            drafted_by=draft["drafted_by"],
            approved_by="ai-auto-apply",
        )
        if not written.get("written"):
            skipped += 1
            continue
        from app.clhear.governance import mark_generated

        mark_generated(
            engine, layer="L2", subject_ref=concept_id, generated_by=model or "candidate-only",
            routing_reason=routing, detail={"members": [m["obligation_id"] for m in members]},
        )
        with engine.begin() as conn:
            pid = l0_proposals.create_proposal(
                conn,
                layer="l2",
                kind="l2_concept",
                subject_ref=concept_id,
                draft=draft,
                rationale=f"auto-applied cross-jurisdiction consolidation ({len(members)} obligations)",
                confidence=0.6 if canonical else 0.4,
            )
            conn.execute(
                proposals_t.update().where(proposals_t.c.id == pid).values(
                    status="approved", approver="ai-auto-apply",
                )
            )
        proposed += 1
        applied += 1
        concept_ids.append(concept_id)
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L2", fleet="l2.consolidate",
            reasoning=f"Weaver: {applied} concepts auto-applied as ai_generated; {skipped} skipped",
            detail={"applied": applied, "skipped": skipped, "llm_drafted": drafted_llm},
        )
    except Exception:
        log.exception("consolidate ai_ops failed")
    return {
        "proposed": proposed,
        "applied": applied,
        "skipped_already_proposed": skipped,
        "llm_drafted": drafted_llm,
        "concept_ids": concept_ids,
    }


def apply_approved_concept(engine: Engine, proposal: dict) -> dict:
    """Called when an l2_concept proposal is approved in the review console."""
    draft = proposal.get("draft") or {}
    if isinstance(draft, str):
        draft = json.loads(draft)
    return upsert_concept(
        engine,
        concept_id=draft["id"],
        name=draft["name"],
        canonical_statement=draft.get("canonical_statement", ""),
        themes=draft.get("themes", []),
        members=draft.get("members", []),
        status="curated",
        drafted_by=draft.get("drafted_by", "gateway"),
        approved_by=proposal.get("approver"),
    )
