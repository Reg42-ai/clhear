# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""CLHEAR obligation concepts: consolidation overlay + jurisdiction-aware
resolution.

A concept is one representative obligation ("Obtain consent before
unsolicited electronic marketing") whose members are clause-anchored derived
obligations across jurisdictions. Resolution is a PURE FUNCTION of
(concept members, requested jurisdiction set):

  - common_core   — requirement facets present in EVERY requested jurisdiction
  - deltas        — per-jurisdiction additions, each backed by its own clauses
  - excluded      — member jurisdictions the profile did not request
  - claim_scope   — the honest claim: exactly these clauses, these
                    jurisdictions, this engine version. Never "global".

A single-jurisdiction profile therefore resolves to just its own facet
(nothing heavier), and a US+EU+UK group resolves to the full union.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import concept_members, concepts, obligations
from app.clhear.platform import record

log = logging.getLogger("clhear.l2.concepts")

RESOLUTION_VERSION = "concept-resolve-v1"


# ------------------------------------------------------------------- store


def upsert_concept(
    engine: Engine,
    *,
    concept_id: str,
    name: str,
    canonical_statement: str,
    themes: list,
    members: list[dict],
    status: str = "curated",
    drafted_by: str = "human",
    approved_by: str | None = None,
) -> dict:
    """Idempotent write. Members referencing unknown obligations are skipped
    and reported — a concept never points at thin air."""
    with engine.begin() as conn:
        known = {
            row.id: row.jurisdiction
            for row in conn.execute(
                sa.select(obligations.c.id, obligations.c.jurisdiction).where(
                    obligations.c.id.in_([m["obligation_id"] for m in members])
                )
            )
        }
        kept, missing = [], []
        for m in members:
            if m["obligation_id"] in known:
                kept.append({**m, "jurisdiction": m.get("jurisdiction") or known[m["obligation_id"]]})
            else:
                missing.append(m["obligation_id"])
        if not kept:
            return {"id": concept_id, "written": False, "missing_members": missing}
        exists = conn.execute(sa.select(concepts.c.id).where(concepts.c.id == concept_id)).first()
        values = dict(
            name=name,
            canonical_statement=canonical_statement,
            themes=themes,
            status=status,
            drafted_by=drafted_by,
            approved_by=approved_by,
            approved_at=datetime.now(timezone.utc) if approved_by else None,
            flag_reason="",
            updated_at=datetime.now(timezone.utc),
        )
        kept_ids = {m["obligation_id"] for m in kept}
        current = {
            r.obligation_id: r
            for r in conn.execute(
                sa.select(concept_members).where(
                    concept_members.c.concept_id == concept_id, record.in_force(concept_members)
                )
            )
        }
        why = record.WhyTrail(
            layer="L2",
            subject_ref=concept_id,
            reasoning_summary=f"concept {concept_id} membership set by {drafted_by}",
            evidence_refs=[{"obligation_id": oid} for oid in sorted(kept_ids)],
            inputs=(concept_id, sorted(kept_ids)),
            agent_id=drafted_by,
            input_layers=("L1",),
        )
        trail_id = why.write(conn)
        if exists:
            conn.execute(concepts.update().where(concepts.c.id == concept_id).values(**values, why_trail_id=trail_id))
            dropped = [oid for oid in current if oid not in kept_ids]
            if dropped:
                record.invalidate(
                    conn,
                    concept_members,
                    sa.and_(
                        concept_members.c.concept_id == concept_id,
                        concept_members.c.obligation_id.in_(dropped),
                        record.in_force(concept_members),
                    ),
                    why=trail_id,
                    reason="member removed on re-consolidation",
                )
        else:
            conn.execute(concepts.insert().values(id=concept_id, **values, why_trail_id=trail_id))
        for m in kept:
            if m["obligation_id"] in current:
                conn.execute(
                    concept_members.update()
                    .where(
                        concept_members.c.concept_id == concept_id,
                        concept_members.c.obligation_id == m["obligation_id"],
                        record.in_force(concept_members),
                    )
                    .values(jurisdiction=m["jurisdiction"], role=m.get("role", "primary"), note=m.get("note", ""))
                )
                continue
            record.write(
                conn,
                concept_members,
                dict(
                    concept_id=concept_id,
                    obligation_id=m["obligation_id"],
                    jurisdiction=m["jurisdiction"],
                    role=m.get("role", "primary"),
                    note=m.get("note", ""),
                ),
                why=trail_id,
            )
    return {"id": concept_id, "written": True, "members": len(kept), "missing_members": missing}


def list_concepts(engine: Engine, status: str | None = None, include_members: bool = True) -> list[dict]:
    query = sa.select(concepts).order_by(concepts.c.id)
    if status:
        query = query.where(concepts.c.status == status)
    else:
        query = query.where(concepts.c.status != "proposed")
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(query).mappings()]
        member_rows = [dict(r) for r in conn.execute(sa.select(concept_members).where(record.in_force(concept_members))).mappings()]
    by_concept: dict[str, list[dict]] = {}
    for m in member_rows:
        by_concept.setdefault(m["concept_id"], []).append(m)
    for c in rows:
        c["updated_at"] = str(c.get("updated_at"))
        c["approved_at"] = str(c["approved_at"]) if c.get("approved_at") else None
        members = by_concept.get(c["id"], [])
        c["jurisdictions"] = sorted({m["jurisdiction"] for m in members if m["jurisdiction"]})
        c["member_count"] = len(members)
        if include_members:
            c["members"] = members
    return rows


def get_concept(engine: Engine, concept_id: str) -> dict | None:
    for c in list_concepts(engine, status=None):
        if c["id"] == concept_id:
            return c
    return None


def consolidated_ids(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return {row.obligation_id for row in conn.execute(sa.select(concept_members.c.obligation_id).where(record.in_force(concept_members)))}


def flag_stale_concepts(engine: Engine) -> list[str]:
    """Nightly: a concept whose member went stale/rejected must be re-reviewed
    before it resolves again."""
    flagged = []
    with engine.begin() as conn:
        rows = conn.execute(
            sa.select(concept_members.c.concept_id, concept_members.c.obligation_id, obligations.c.status)
            .join(obligations, obligations.c.id == concept_members.c.obligation_id, isouter=True)
            .where(record.in_force(concept_members))
        ).all()
        bad_by_concept: dict[str, list[str]] = {}
        for row in rows:
            if row.status not in ("derived", "validated"):
                bad_by_concept.setdefault(row.concept_id, []).append(f"{row.obligation_id} ({row.status})")
        for concept_id, reasons in bad_by_concept.items():
            conn.execute(
                concepts.update()
                .where(concepts.c.id == concept_id)
                .where(concepts.c.status != "flagged")
                .values(status="flagged", flag_reason="; ".join(reasons)[:400])
            )
            flagged.append(concept_id)
    return flagged


# --------------------------------------------------------------- resolution


def _member_payload(engine: Engine, member: dict) -> dict:
    with engine.connect() as conn:
        row = conn.execute(sa.select(obligations).where(obligations.c.id == member["obligation_id"])).first()
    if row is None:
        return {**member, "resolved": False}
    return {
        "obligation_id": row.id,
        "jurisdiction": member["jurisdiction"],
        "role": member["role"],
        "note": member["note"],
        "title": row.title,
        "statement": row.statement,
        "status": row.status,
        "confidence": float(row.confidence),
        "source_key": row.source_key,
        "clause_ref": row.clause_ref,
        "text_hash": row.text_hash,
        "resolved": True,
    }


def resolve_concept(engine: Engine, concept: dict, jurisdictions: list[str]) -> dict:
    """Deterministic resolution for one jurisdiction set. Flagged concepts do
    not resolve (their members drifted; a human must re-confirm first)."""
    requested = sorted({j for j in jurisdictions if j})
    if concept.get("status") == "flagged":
        return {
            "concept_id": concept["id"],
            "resolvable": False,
            "reason": f"flagged for review: {concept.get('flag_reason', '')}",
            "resolution_version": RESOLUTION_VERSION,
        }
    members = [_member_payload(engine, m) for m in concept.get("members", [])]
    members = [m for m in members if m.get("resolved")]
    member_jurs = sorted({m["jurisdiction"] for m in members})
    applicable = [m for m in members if m["jurisdiction"] in requested]
    excluded = [j for j in member_jurs if j not in requested]

    # Common core: facets whose jurisdiction list covers EVERY requested
    # jurisdiction that this concept knows about. With clause-anchored members
    # a facet is per-jurisdiction, so the core is the intersection semantics:
    # jurisdictions requested AND present; deltas carry each one's own clauses.
    covered_requested = [j for j in requested if j in member_jurs]
    uncovered_requested = [j for j in requested if j not in member_jurs]
    deltas = {
        j: [m for m in applicable if m["jurisdiction"] == j]
        for j in covered_requested
    }
    return {
        "concept_id": concept["id"],
        "name": concept["name"],
        "canonical_statement": concept["canonical_statement"],
        "resolvable": bool(applicable),
        "resolution_version": RESOLUTION_VERSION,
        "requested_jurisdictions": requested,
        "covered_jurisdictions": covered_requested,
        "uncovered_jurisdictions": uncovered_requested,
        "excluded_jurisdictions": excluded,
        "facets": deltas,
        "weight": {"obligations": len(applicable), "jurisdictions": len(covered_requested)},
        "claim_scope": (
            f"Covers exactly the cited clauses for {', '.join(covered_requested) or 'no requested jurisdiction'}"
            + (f"; NOT covered here: {', '.join(uncovered_requested)} (no ingested basis yet)" if uncovered_requested else "")
            + ". This is not a claim of global compliance."
        ),
    }


def resolve_all(engine: Engine, jurisdictions: list[str]) -> list[dict]:
    return [
        resolve_concept(engine, c, jurisdictions)
        for c in list_concepts(engine)
    ]
