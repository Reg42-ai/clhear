# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 decomposers — obligation -> building block(s) (HLD v2 §4.3).

Deterministic-first, and only from the duty's own words: the measure is what
the duty tells the addressee to do or to have, quoted from the clause ("keep a
log of security incidents", "an inventory of the systems ..."). Its kind is
read from the same words (:func:`app.clhear.l3.kinds.kind_from_words`); a duty
that names nothing concrete gets no measure and an evidence gap instead.
:func:`find_or_create_block` reuses an existing canonical block of that kind
when the name matches, otherwise mints ``BLK-000001``. Every obligation ->
block link is a ``requires`` edge carrying the quoted duty and a why-trail.

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
from app.clhear import evidence
from app.clhear.l2.registry import duty_span, field_quotes, obligation_clauses
from app.clhear.l3.kinds import KINDS, kind_from_words
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

# Where a measure's name stops: the thing to do or have has been named; what
# follows is its condition, timing or purpose. Plain English grammar.
_CUT = re.compile(
    r"\s*[;:].*$|,?\s+(?:where|when|whenever|if|unless|within|no later than|before|after|upon|at least|"
    r"without (?:undue )?delay|in accordance with|taking into account|so that|in order to|so as to|to ensure|"
    r"to enable|to allow|to prevent|as soon as|provided that|subject to)\b.*$",
    re.I | re.S)
# "appoint a person ... and record the appointment": a second verb phrase starts the next measure.
_SECOND_ACTION = re.compile(r"\s+and\s+(?:(?:shall|must|should|will)\b.*$|(?=\w+\s+(?:the|a|an|its|their|all|any|each|every)\b).*$)",
                            re.I | re.S)
_DETERMINER = re.compile(r"^(?:(?:a|an|the|its|their|his|her|all|any|each|every|such|that|those|these)\s+)+", re.I)
MAX_NAME_WORDS = 12


def measure_phrase(action: str) -> str:
    """The action up to where its condition, timing or purpose begins."""
    phrase = _CUT.sub("", " ".join((action or "").split()))
    phrase = _SECOND_ACTION.sub("", phrase).strip(" ,;:.")
    return " ".join(phrase.split()[:MAX_NAME_WORDS])


def _content(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def name_similarity(a: str, b: str) -> float:
    ca, cb = _content(a), _content(b)
    return len(ca & cb) / len(ca | cb) if (ca | cb) else 0.0


def propose_block(obligation: dict, clauses: list[dict]) -> dict | None:
    """The measure a duty names, in its own words, or None when it names none.

    Active duties name an action ("review user access rights") or a thing to
    have ("an inventory of the systems ..."); a passive duty ("personal data
    shall be kept ...") names its subject and what must be true of it. Every
    part of the name is quoted from ``clauses``; the kind is read from the same
    words, or ``Unspecified``."""
    action = obligation.get("action") or ""
    subject = obligation.get("subject") or ""
    passive = action.lower().startswith(("be ", "been "))
    phrase = measure_phrase(action.split(" ", 1)[1] if passive and " " in action else action)
    if not phrase:
        return None
    if passive:
        if not subject:
            return None
        parts, kind, kind_word = [subject, phrase], *kind_from_words("", subject)
        name = f"{subject[0].upper()}{subject[1:]}: {phrase}"
    else:
        verb, _, rest = phrase.partition(" ")
        obj = _DETERMINER.sub("", rest).strip()
        kind, kind_word = kind_from_words(verb, obj)
        if kind == "Unspecified":
            kind, kind_word = "Process", verb  # the duty says to do something: an activity to carry out
        named = phrase if kind == "Process" or not obj else obj
        parts, name = [named], named[0].upper() + named[1:]
    quotes = []
    for part in parts:
        found = field_quotes(part, clauses)
        if not found:
            return None
        quotes.extend(found)
    kind_quote = field_quotes(kind_word, clauses) if kind_word else []
    duty = (obligation.get("evidence") or {}).get("duty") if isinstance(obligation.get("evidence"), dict) else None
    return {"kind": kind, "name": name[:160], "purpose": (duty or {}).get("quote") or phrase,
            "evidence": {"name": quotes, "kind": kind_quote or []}}


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
                         method: str = "deterministic", evidence: dict | None = None) -> tuple[str, bool]:
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
            "evidence": evidence or {},
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
    """Open the requires edge obligation -> block (idempotent per pair). Its
    rationale is the duty as quoted from the clause."""
    existing = live_edge(conn, obligation["id"], block_id)
    if existing is not None:
        return None
    found = obligation.get("evidence") if isinstance(obligation.get("evidence"), dict) else {}
    duty = found.get("duty")
    rid = next_id(conn, "REQ")
    record.write(
        conn,
        requires,
        {
            "id": rid,
            "obligation_id": obligation["id"],
            "block_id": block_id,
            "rationale": rationale if rationale is not None else ((duty or {}).get("quote") or ""),
            "rationale_start": (duty or {}).get("start") if rationale is None else None,
            "rationale_end": (duty or {}).get("end") if rationale is None else None,
            "method": method,
            "obligation_text_hash": obligation.get("text_hash") or "",
            "evidence": {"duty": [duty] if duty else []},
        },
        why=why,
        valid_from=datetime.now(timezone.utc).date(),
    )
    return rid


def _selector_covers(selector: dict, ob: dict) -> bool:  # legacy selectors on pre-0.2 blocks
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
    """Give every live obligation without a live requires edge the measure its
    own words name. A duty that names nothing concrete stays without one, and
    an evidence gap says which source would name it."""
    from app.clhear.l1.scopes import active_name

    scope = active_name() or ""
    linked_derived = created = examined = gaps = 0
    with engine.begin() as conn:
        todo = [ob for ob in _live_obligations(conn, source_key) if live_edge(conn, ob["id"]) is None]
        if limit:
            todo = todo[:limit]
        for ob in todo:
            examined += 1
            ref = ob["stable_id"] or ob["id"]
            proposal = propose_block(ob, obligation_clauses(conn, ob))
            if proposal is None:
                evidence.record_gap(conn, scope=scope, layer="L3", kind="no_measure", subject=ob["id"],
                                    source_key=ob["source_key"], clause_ref=ob["clause_ref"],
                                    missing="a measure named by the text")
                gaps += 1
                continue
            quotes = proposal["evidence"]["name"] + proposal["evidence"]["kind"]
            trail = why_for(ref, method="deterministic", confidence=0.8,
                            summary=f"measure named by the duty's own words: '{proposal['name']}' ({proposal['kind']})",
                            evidence_refs=[ob["id"], *quotes]).write(conn)
            bid, was_created = find_or_create_block(conn, kind=proposal["kind"], name=proposal["name"],
                                                    purpose=proposal["purpose"], why=trail,
                                                    evidence=proposal["evidence"])
            created += int(was_created)
            if link(conn, obligation=ob, block_id=bid, method="deterministic", why=trail):
                linked_derived += 1
    out = {"examined": examined, "linked_derived": linked_derived, "blocks_created": created, "gaps": gaps}
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
