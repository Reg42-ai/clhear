# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 decomposers — obligation -> building block(s) (HLD v2 §4.3).

Deterministic-first: the duty sentence of an obligation names what the
organisation must *have* (a policy, an officer, a monitoring system, a
capital level…). :func:`propose_block` infers the kind and a harmonised name;
:func:`find_or_create_block` reuses an existing canonical block of that kind
when the name matches (one AML policy document, cited by hundreds of
obligations), otherwise mints ``BLK-000001``. Every obligation -> block link is
a ``requires`` edge carrying the rationale span and a why-trail (I3). Curated
blocks keep their ``satisfies`` selectors; those anchors become explicit
``requires`` edges too, so the completeness gate has one source of truth.

Change propagation: ``clhear.l2.changed`` (revoked -> edges invalidated;
updated -> edge re-stamped, characteristics backed by the obligation reopened).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import blocks, characteristics, obligations, requires
from app.clhear.l2.registry import duty_span
from app.clhear.l3.kinds import KINDS, infer_kind
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l3.decompose")

AGENT = "l3.decompose"
LIVE = ("derived", "validated")
NAME_MATCH = 0.75  # content-word Jaccard for reusing an existing block of the same kind

_STOP = frozenset({"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "by", "with", "that", "which",
                   "its", "their", "such", "any", "all", "as", "at", "be", "is", "are", "must", "shall", "firm",
                   "firms", "person", "institution", "entity", "it", "this", "these", "those", "not", "no"})
_WORD = re.compile(r"[a-z0-9]+")

# kind -> regex capturing the thing to be had; group "obj" becomes the block name stem.
_NAME_CUES: dict[str, tuple[re.Pattern, str]] = {
    "Role": (re.compile(r"\b(?:appoint|designate|nominate|have|maintain|employ)\s+(?:an?|the|its)?\s*(?P<obj>[\w\- ]{3,60}?(?:officer|MLRO|function|manager|director|person|individual|head of [\w ]{3,30}))\b", re.I), "role"),
    "Body": (re.compile(r"\b(?P<obj>(?:[\w\-]+ ){0,3}(?:committee|board(?: of directors)?|management body|forum))\b", re.I), "body"),
    "Document": (re.compile(r"\b(?P<obj>(?:[\w\-]+ ){0,4}(?:polic(?:y|ies)|procedures?|register|charter|plan|manual|statement|agreement|contract|prospectus|terms of business))\b", re.I), "document"),
    "System": (re.compile(r"\b(?P<obj>(?:[\w\-]+ ){0,3}(?:monitoring system|screening system|systems? and controls|systems?|monitoring|surveillance|screening|software|database))\b", re.I), "system"),
    "Asset": (re.compile(r"\b(?P<obj>own funds|capital(?: resources)?|client money|client assets|(?:professional indemnity )?insurance|liquidity(?: buffer)?|reserves?|collateral)\b", re.I), "asset"),
    "Configuration": (re.compile(r"\b(?P<obj>(?:[\w\-]+ ){0,3}(?:threshold|limit|parameter|maximum|minimum))\b", re.I), "configuration"),
    "Workflow": (re.compile(r"\b(?P<obj>(?:[\w\-]+ ){0,3}(?:escalation|approval|sign-off|workflow))\b", re.I), "workflow"),
    "Process": (re.compile(r"\b(?P<obj>(?:review|assess|report|notify|submit|file|verify|identify|record|retain|train|test|reconcile|disclose|inform|monitor|conduct|carry out|perform)\w*(?: [\w\-]+){0,6})", re.I), "process"),
}

# Leading verbs / qualifiers that describe the duty, not the deliverable.
_LEAD = re.compile(
    r"^(?:(?:establish|maintain|implement|adopt|operate|hold|keep|have|put in place|prepare|produce|draw up|"
    r"appoint|designate|nominate|ensure|develop|document|apply|use|and|or|written|documented|formal|adequate|"
    r"appropriate|effective|robust|sufficient|suitable|an?|the|its|their) )+",
    re.I,
)
# Where a process name stops: the object has been named, the rest is condition.
_PROCESS_CUT = re.compile(r"\s+(?:for|within|at|by|in accordance|under|to the|no later|before|after|where|when|if|on|,|;).*$", re.I)


def _content(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def name_similarity(a: str, b: str) -> float:
    ca, cb = _content(a), _content(b)
    return len(ca & cb) / len(ca | cb) if (ca | cb) else 0.0


def _clean(stem: str, *, strip_lead: bool = True) -> str:
    stem = re.sub(r"\s+", " ", stem or "").strip(" ,;:.")
    if strip_lead:
        stem = _LEAD.sub("", stem)
    return stem[:80]


def propose_block(obligation: dict) -> dict:
    """Kind, harmonised name and purpose of the block this obligation most
    directly requires. Names are noun phrases from the text itself, with the
    duty verb stripped, so the same deliverable in two instruments harmonises
    onto one block."""
    text = obligation.get("determination") or obligation.get("statement") or ""
    kind = infer_kind(text)
    pattern, suffix = _NAME_CUES[kind]
    matches = list(pattern.finditer(text))
    # Prefer the most specific noun phrase ("audit committee" over "board") when several are named.
    m = max(matches, key=lambda x: len(x.group("obj").split()), default=None) if kind in ("Body", "Role") else (matches[0] if matches else None)
    if kind == "Process":
        stem = _clean(_PROCESS_CUT.sub("", m.group("obj")) if m else (obligation.get("action") or ""), strip_lead=False)
    else:
        stem = _clean(m.group("obj")) if m else _clean(obligation.get("action") or obligation.get("title") or "")
    if not stem:
        stem = _clean(obligation.get("title") or "requirement") or "Requirement"
    name = stem[0].upper() + stem[1:]
    if kind == "Process" and len(name.split()) < 3 and not re.search(r"(process|procedure|programme|review|assessment)$", name, re.I):
        name = f"{name} process"
    elif kind == "Role" and not re.search(r"(officer|function|manager|director|role|head|person|individual)", name, re.I):
        name = f"{name} role"
    elif kind == "System" and not re.search(r"(system|software|database|controls)$", name, re.I):
        name = f"{name} system"
    purpose = duty_sentence(text) or text[:240]
    return {"kind": kind, "name": name[:160], "purpose": purpose[:400], "suffix": suffix}


def duty_sentence(text: str) -> str:
    span = duty_span(text or "")
    return (text[span[0]:span[1]].strip() if span else "")


def why_for(subject_ref: str, *, method: str, confidence: float | None, summary: str,
            evidence_refs: list[str] | None = None, model_manifest: dict | None = None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L3",
        reasoning_summary=summary,
        evidence_refs=evidence_refs or [],
        inputs=(subject_ref, method, *(evidence_refs or [])),
        model_manifest=model_manifest or {"model": "deterministic", "method": method},
        skill_version=AGENT,
        confidence=confidence,
        agent_id=AGENT,
        subject_ref=subject_ref,
        input_layers=("L2",),
    )


def _canonical_blocks(conn: Connection, kind: str | None = None) -> list[dict]:
    q = sa.select(blocks).where(blocks.c.canonical_id.is_(None))
    if kind:
        q = q.where(blocks.c.kind == kind)
    return [dict(r) for r in conn.execute(q).mappings()]


def find_or_create_block(conn: Connection, *, kind: str, name: str, purpose: str, why: record.WhyTrail | str,
                         method: str = "deterministic") -> tuple[str, bool]:
    """Reuse the best-matching canonical block of this kind, else create one.
    Returns (block_id, created)."""
    if kind not in KINDS:
        raise ValueError(f"unknown block kind {kind}")
    best, score = None, 0.0
    for b in _canonical_blocks(conn, kind):
        s = name_similarity(b["name"], name)
        if s > score:
            best, score = b, s
    if best is not None and score >= NAME_MATCH:
        return best["id"], False
    bid = next_id(conn, "BLK")
    record.write(
        conn,
        blocks,
        {
            "id": bid,
            "name": name,
            "description": purpose,
            "capability": "",
            "evidence_artifacts": [],
            "satisfies": [],
            "implements_controls": [],
            "status": "derived",
            "kind": kind,
            "purpose": purpose,
        },
        why=why,
        valid_from=datetime.now(timezone.utc).date(),
    )
    return bid, True


def live_edge(conn: Connection, obligation_id: str, block_id: str | None = None):
    q = sa.select(requires).where(requires.c.obligation_id == obligation_id).where(requires.c.valid_to.is_(None))
    if block_id:
        q = q.where(requires.c.block_id == block_id)
    return conn.execute(q).mappings().first()


def link(conn: Connection, *, obligation: dict, block_id: str, method: str, why: record.WhyTrail | str,
         rationale: str | None = None) -> str | None:
    """Open the requires edge obligation -> block (idempotent per pair)."""
    existing = live_edge(conn, obligation["id"], block_id)
    if existing is not None:
        return None
    text = obligation.get("determination") or obligation.get("statement") or ""
    span = duty_span(text)
    rid = next_id(conn, "REQ")
    record.write(
        conn,
        requires,
        {
            "id": rid,
            "obligation_id": obligation["id"],
            "block_id": block_id,
            "rationale": rationale if rationale is not None else (text[span[0]:span[1]] if span else text[:300]),
            "rationale_start": span[0] if span and rationale is None else None,
            "rationale_end": span[1] if span and rationale is None else None,
            "method": method,
            "obligation_text_hash": obligation.get("text_hash") or "",
        },
        why=why,
        valid_from=datetime.now(timezone.utc).date(),
    )
    return rid


def _selector_covers(selector: dict, ob: dict) -> bool:
    if not isinstance(selector, dict) or selector.get("source_key") != ob["source_key"]:
        return False
    refs = selector.get("refs")
    return not refs or ob["clause_ref"] in refs


def curated_anchor_blocks(conn: Connection, ob: dict) -> list[dict]:
    return [b for b in _canonical_blocks(conn) if any(_selector_covers(s, ob) for s in (b["satisfies"] or []))]


def _live_obligations(conn: Connection, source_key: str | None = None) -> list[dict]:
    from app.clhear.l1.scopes import limiting

    q = sa.select(obligations).where(obligations.c.status.in_(LIVE))
    limit = limiting(obligations.c.source_key, source_key)
    if limit is not None:
        q = q.where(limit)
    return [dict(r) for r in conn.execute(q.order_by(obligations.c.stable_id, obligations.c.id)).mappings()]


def decompose(engine: Engine, *, source_key: str | None = None, limit: int | None = None) -> dict:
    """Give every live obligation without a live requires edge at least one
    block. Curated anchors first (human-authored mapping wins), then the
    deterministic proposal."""
    linked_curated = linked_derived = created = examined = 0
    with engine.begin() as conn:
        todo = [ob for ob in _live_obligations(conn, source_key) if live_edge(conn, ob["id"]) is None]
        if limit:
            todo = todo[:limit]
        for ob in todo:
            examined += 1
            ref = ob["stable_id"] or ob["id"]
            anchors = curated_anchor_blocks(conn, ob)
            if anchors:
                for b in anchors:
                    why = why_for(ref, method="curated-anchor", confidence=1.0,
                                  summary=f"curated block {b['id']} anchors {ob['source_key']} #{ob['clause_ref']} via satisfies selector",
                                  evidence_refs=[ob["id"]])
                    if link(conn, obligation=ob, block_id=b["id"], method="curated-anchor", why=why):
                        linked_curated += 1
                continue
            proposal = propose_block(ob)
            trail = why_for(ref, method="deterministic", confidence=0.8 if proposal["kind"] != "Process" else 0.7,
                            summary=f"{proposal['kind']} cue in duty sentence -> block '{proposal['name']}'",
                            evidence_refs=[ob["id"]]).write(conn)
            bid, was_created = find_or_create_block(conn, kind=proposal["kind"], name=proposal["name"],
                                                    purpose=proposal["purpose"], why=trail)
            created += int(was_created)
            if link(conn, obligation=ob, block_id=bid, method="deterministic", why=trail):
                linked_derived += 1
    out = {"examined": examined, "linked_curated": linked_curated, "linked_derived": linked_derived, "blocks_created": created}
    log.info("L3 decompose: %s", out)
    return out


def completeness(engine: Engine) -> dict:
    """Share of live obligations with >= 1 live requires edge."""
    with engine.connect() as conn:
        obs = _live_obligations(conn)
        linked = {
            r[0] for r in conn.execute(sa.select(requires.c.obligation_id).where(requires.c.valid_to.is_(None)))
        }
    missing = [o["stable_id"] or o["id"] for o in obs if o["id"] not in linked]
    total = len(obs)
    return {
        "obligations": total,
        "linked": total - len(missing),
        "missing": missing[:40],
        "missing_count": len(missing),
        "rate": round((total - len(missing)) / total, 4) if total else 0.0,
    }


def backfill_kinds_and_requires(conn: Connection) -> dict:
    """Migration helper: kinds for pre-HLD-v2 blocks + requires edges for
    every obligation their satisfies selectors anchor."""
    kinds_set = edges = 0
    obs = _live_obligations(conn)
    for b in conn.execute(sa.select(blocks)).mappings().all():
        b = dict(b)
        if b["id"].startswith("BLK-AI-") and b["kind"] == "Process":
            inferred = infer_kind(f"{b['name']} {b['description']} {b['capability']}")
            if inferred != b["kind"]:
                conn.execute(blocks.update().where(blocks.c.id == b["id"]).values(kind=inferred))
                kinds_set += 1
        if not b["purpose"] and b["description"]:
            conn.execute(blocks.update().where(blocks.c.id == b["id"]).values(purpose=b["description"][:400]))
        for ob in obs:
            if any(_selector_covers(s, ob) for s in (b["satisfies"] or [])):
                why = why_for(ob["stable_id"] or ob["id"], method="curated-anchor", confidence=1.0,
                              summary=f"migration m0011: satisfies selector of {b['id']} -> requires edge",
                              evidence_refs=[ob["id"]])
                if link(conn, obligation=ob, block_id=b["id"], method="curated-anchor", why=why):
                    edges += 1
    return {"kinds_set": kinds_set, "requires": edges}


def on_l2_changed(engine: Engine, payload: dict) -> dict:
    """Propagate an L2 change into L3 (I1: layers derive downward)."""
    oid = payload.get("derivation_key") or payload.get("obligation_id")
    change = payload.get("change") or payload.get("kind")
    if not oid:
        return {"ignored": True, "reason": "no obligation in payload"}
    invalidated_edges = reopened = relinked = 0
    with engine.begin() as conn:
        ob = conn.execute(sa.select(obligations).where(
            sa.or_(obligations.c.id == oid, obligations.c.stable_id == oid))).mappings().first()
        if ob is None:
            return {"ignored": True, "reason": f"unknown obligation {oid}"}
        ob = dict(ob)
        ref = ob["stable_id"] or ob["id"]
        why = why_for(ref, method="l2.changed", confidence=None,
                      summary=f"L2 change '{change}' on {ref} (L2 change event {payload.get('change_event_id')})",
                      evidence_refs=[str(payload.get("change_event_id") or "")])
        trail = why.write(conn)
        edges = conn.execute(sa.select(requires).where(requires.c.obligation_id == ob["id"]).where(requires.c.valid_to.is_(None))).mappings().all()
        if change == "revoked":
            for e in edges:
                record.invalidate(conn, requires, requires.c.id == e["id"], why=trail, reason="obligation revoked")
                invalidated_edges += 1
        elif change == "updated":
            for e in edges:
                if e["obligation_text_hash"] != ob["text_hash"]:
                    record.invalidate(conn, requires, requires.c.id == e["id"], why=trail, reason="obligation text changed")
                    invalidated_edges += 1
                    if link(conn, obligation=ob, block_id=e["block_id"], method=e["method"], why=trail):
                        relinked += 1
        # Characteristics backed by this obligation are no longer known-good.
        backed = conn.execute(sa.select(characteristics).where(characteristics.c.backing_obligation_id == ob["id"])
                              .where(characteristics.c.valid_to.is_(None))).mappings().all()
        for c in backed:
            record.invalidate(conn, characteristics, characteristics.c.id == c["id"], why=trail,
                              reason=f"backing obligation {change}")
            reopened += 1
    out = {"obligation": ref, "change": change, "edges_invalidated": invalidated_edges,
           "edges_relinked": relinked, "characteristics_reopened": reopened}
    if change == "added" or (change == "updated" and not edges):
        out["decompose"] = decompose(engine, source_key=ob["source_key"])
    log.info("L3 propagation: %s", out)
    return out
