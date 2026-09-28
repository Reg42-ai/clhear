# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 registry core (HLD v2 §4.2): stable ids, structured determinations,
asserts edges, change events, and the why-trail every write carries.

Deterministic first. The structure parser splits a duty clause into
subject / action / condition / object around the modal verb and renders the
plain-language determination text ("<subject> must <action> [<condition>]").
A structured `l2_extract` model pass may refine these fields later
(``extract_structured``) but can never invent a clause: every obligation keeps
its ``asserts`` edge to the exact clause, span and hash it came from.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.derived_models import asserts, l2_change_events, obligations, supersessions
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l2.registry")

AGENT = "l2.registry"

_MODAL = re.compile(
    r"\b(?P<modal>must not|shall not|may not|must|shall|is required to|are required to|"
    r"is obliged to|are obliged to|is responsible for ensuring|are responsible for ensuring|"
    r"should|ought to)\b",
    re.I,
)
_CONDITION = re.compile(
    r"\b(?P<cond>(?:if|where|when|whenever|unless|provided that|in the event that|to the extent that|"
    r"except where|subject to)\b.*)$",
    re.I,
)
_LEADING_NUMBER = re.compile(r"^(?:\(?\d+[\.\)]\s*|\([a-z0-9]+\)\s*|[A-Z]{2,6}\s+\d+(?:\.\d+)*[A-Z]?\s*)+")
_SENTENCE_END = re.compile(r"(?<=[.;:])\s+(?=[A-Z(\d])")
_STRIP_TRAIL = re.compile(r"[\s,;:.]+$")

TYPE_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("prohibition", re.compile(r"\b(?:must not|shall not|may not|prohibited|is not permitted)\b", re.I)),
    ("reporting", re.compile(r"\b(?:report|notify|notification|submit|file|inform the)\b", re.I)),
    ("disclosure", re.compile(r"\b(?:disclose|disclosure|publish|make available|provide .{0,40}information)\b", re.I)),
    ("record_keeping", re.compile(r"\b(?:record|records|retain|keep .{0,20}(?:records|register)|maintain .{0,20}(?:records|register|log))\b", re.I)),
    ("authorisation", re.compile(r"\b(?:authoris|authoriz|licen[cs]e|permission|registration|registered)\b", re.I)),
    ("prudential", re.compile(r"\b(?:capital|liquidity|own funds|prudential|solvency|leverage)\b", re.I)),
    ("data_protection", re.compile(r"\b(?:personal data|data subjects?|privacy|processing of (?:personal )?(?:data|information)|"
                                   r"consent|data protection|personal information)\b", re.I)),
    ("security", re.compile(r"\b(?:security|encrypt|access control|authenticat|vulnerabilit|incident|breach|"
                            r"malware|cyber|confidentiality|integrity and availability)\b", re.I)),
    ("risk_management", re.compile(r"\b(?:risk assessment|assess(?:es|ing)? (?:the )?risks?|risk management|"
                                   r"risks? (?:to|of)|mitigat|impact assessment)\b", re.I)),
    ("training", re.compile(r"\b(?:train(?:ing|ed)?|awareness|competen(?:ce|t))\b", re.I)),
    ("safety", re.compile(r"\b(?:safety|hazard|injur|harm to (?:health|persons)|protective equipment)\b", re.I)),
    ("governance", re.compile(r"\b(?:governance|senior management|management body|board|policies and procedures|"
                              r"systems and controls|compliance function|oversight|responsibilit)\b", re.I)),
    ("consumer_protection", re.compile(r"\b(?:client|customer|consumer|investor|retail|best interests|fair|clearly|not misleading)\b", re.I)),
    ("conduct", re.compile(r"\b(?:conduct|act honestly|integrity|due skill|due regard)\b", re.I)),
)


def duty_span(text: str) -> tuple[int, int] | None:
    """(start, end) of the sentence carrying the first modal, as offsets into
    ``text`` — the highlighted span on the obligation page."""
    if not text:
        return None
    m = _MODAL.search(text)
    if not m:
        return None
    starts = [0] + [s.end() for s in _SENTENCE_END.finditer(text)]
    start = max(s for s in starts if s <= m.start())
    later = [s for s in starts if s > m.start()]
    end = later[0] if later else len(text)
    # trim trailing whitespace of the sentence
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def parse_structure(text: str) -> dict:
    """Split a duty sentence into subject / modal / action / condition / object."""
    span = duty_span(text or "")
    if span is None:
        return {"subject": "", "modal": "", "action": "", "condition": "", "object": ""}
    sentence = " ".join(text[span[0]:span[1]].split())
    sentence = _LEADING_NUMBER.sub("", sentence).strip()
    m = _MODAL.search(sentence)
    if m is None:
        return {"subject": "", "modal": "", "action": "", "condition": "", "object": ""}
    subject = _STRIP_TRAIL.sub("", sentence[: m.start()]).strip()
    rest = sentence[m.end():].strip()
    condition = ""
    cm = _CONDITION.search(rest)
    if cm and cm.start() > 0:
        condition = _STRIP_TRAIL.sub("", cm.group("cond")).strip()
        rest = _STRIP_TRAIL.sub("", rest[: cm.start()]).strip()
    elif cm is None:
        # Leading condition: "Where X, a firm must Y" -> subject carries it.
        lead = re.match(r"^(?P<cond>(?:if|where|when|whenever|unless|subject to)\b[^,]{3,200}),\s*(?P<subj>.+)$", subject, re.I)
        if lead:
            condition = lead.group("cond").strip()
            subject = lead.group("subj").strip()
    rest = _STRIP_TRAIL.sub("", rest)
    # Object: the noun phrase after the first verb group — keep it simple and
    # honest: the first prepositional/object tail after the verb.
    obj = ""
    om = re.match(r"^(?P<verb>[a-z]+(?:\s+(?:and|or)\s+[a-z]+)?(?:\s+(?:that|to|the|a|an|its|their|all|any|such|with|for|of|on|in))?)\s+(?P<obj>.+)$", rest, re.I)
    if om:
        obj = om.group("obj").strip()
    subject = re.sub(r"^(?:each|every|an?|the)\s+", "", subject, flags=re.I).strip()
    return {
        "subject": subject[:200],
        "modal": m.group("modal").lower(),
        "action": rest[:400],
        "condition": condition[:300],
        "object": obj[:300],
    }


def classify_type(text: str, modality: str = "") -> str:
    if modality == "must-not":
        return "prohibition"
    for kind, pattern in TYPE_RULES:
        if pattern.search(text or ""):
            return kind
    return "other"


def determination_text(structure: dict, fallback: str = "") -> str:
    subject = structure.get("subject") or "The addressee"
    modal = structure.get("modal") or "must"
    action = structure.get("action") or ""
    condition = structure.get("condition") or ""
    if not action:
        return " ".join((fallback or "").split())[:480]
    text = f"{subject[0].upper()}{subject[1:]} {modal} {action}"
    if condition:
        text += f", {condition[0].lower()}{condition[1:]}"
    text = re.sub(r"\s+([,;:.])", r"\1", text).replace(",,", ",")
    return text.rstrip(".;, ") + "."


def structured_fields(text: str, modality: str) -> dict:
    """Everything the extractor persists beyond the legacy title/statement."""
    structure = parse_structure(text)
    return {
        "subject": structure["subject"],
        "action": structure["action"],
        "condition": structure["condition"],
        "object": structure["object"],
        "obligation_type": classify_type(text, modality),
        "determination": determination_text(structure, fallback=text),
    }


def why_for(obligation_id: str, *, clause_id: int | None, text_hash: str, method: str,
            confidence: float | None, summary: str, model_manifest: dict | None = None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L2",
        subject_ref=obligation_id,
        reasoning_summary=summary,
        evidence_refs=[{"layer": "L1", "clause_id": clause_id, "text_hash": text_hash}],
        inputs=(obligation_id, text_hash, method),
        model_manifest=model_manifest or {},
        skill_version=method,
        confidence=confidence,
        agent_id=AGENT,
        input_layers=("L1",),
    )


def ensure_stable_id(conn: Connection, obligation_id: str) -> str:
    row = conn.execute(sa.select(obligations.c.stable_id).where(obligations.c.id == obligation_id)).first()
    if row is None:
        raise KeyError(obligation_id)
    if row[0]:
        return row[0]
    sid = next_id(conn, "OBL")
    conn.execute(obligations.update().where(obligations.c.id == obligation_id).values(stable_id=sid))
    return sid


def resolve_obligation_id(conn: Connection, ref: str) -> str | None:
    """Accept either the public OBL-000001 id or the derivation key."""
    col = obligations.c.stable_id if re.match(r"^OBL-\d{6,}$", ref or "") else obligations.c.id
    return conn.execute(sa.select(obligations.c.id).where(col == ref)).scalar()


def upsert_assert(
    conn: Connection,
    *,
    obligation_id: str,
    clause_id: int,
    source_key: str,
    clause_ref: str,
    text: str,
    text_hash: str,
    strength: str = "explicit",
    why: record.WhyTrail | str,
) -> str:
    """Open (or refresh) the asserts edge obligation <- clause. An edge whose
    clause hash changed is invalidated and a new one written (I2)."""
    # Edge identity is (obligation, source, clause ref): a new L1 version gives
    # the same provision a new clause row, which supersedes the old edge.
    live_edges = conn.execute(
        sa.select(asserts)
        .where(asserts.c.obligation_id == obligation_id)
        .where(asserts.c.source_key == source_key)
        .where(asserts.c.clause_ref == clause_ref)
        .where(asserts.c.valid_to.is_(None))
    ).mappings().all()
    span = duty_span(text or "")
    for live in live_edges:
        if live["clause_id"] == clause_id and live["text_hash"] == text_hash and live["strength"] == strength:
            return live["id"]
        reason = "clause text changed" if live["text_hash"] != text_hash else "clause re-versioned"
        record.invalidate(conn, asserts, asserts.c.id == live["id"], why=why, reason=reason)
    ast_id = next_id(conn, "AST")
    record.write(
        conn,
        asserts,
        {
            "id": ast_id,
            "obligation_id": obligation_id,
            "clause_id": clause_id,
            "source_key": source_key,
            "clause_ref": clause_ref,
            "span_start": span[0] if span else None,
            "span_end": span[1] if span else None,
            "strength": strength,
            "text_hash": text_hash,
        },
        why=why,
        valid_from=datetime.now(timezone.utc).date(),
    )
    return ast_id


def record_change(
    conn: Connection,
    *,
    obligation_id: str,
    kind: str,
    cause_clause_ids: list[int],
    source_key: str,
    old_text_hash: str = "",
    new_text_hash: str = "",
    effective_date: date | None,
    effective_date_basis: str,
    cause_l1_change_event_id: int | None = None,
    detail: dict | None = None,
    why: record.WhyTrail | str,
    publish: bool = True,
) -> str:
    """One L2 change event + the `clhear.l2.changed` bus event (same transaction)."""
    from app.clhear.platform import events as l0_events

    chg_id = next_id(conn, "CHG")
    record.write(
        conn,
        l2_change_events,
        {
            "id": chg_id,
            "obligation_id": obligation_id,
            "kind": kind,
            "cause_clause_ids": list(cause_clause_ids),
            "cause_l1_change_event_id": cause_l1_change_event_id,
            "source_key": source_key,
            "old_text_hash": old_text_hash,
            "new_text_hash": new_text_hash,
            "effective_date": effective_date,
            "effective_date_basis": effective_date_basis,
            "detail": detail or {},
        },
        why=why,
        valid_from=effective_date or datetime.now(timezone.utc).date(),
    )
    if publish:
        stable = conn.execute(sa.select(obligations.c.stable_id).where(obligations.c.id == obligation_id)).scalar()
        l0_events.publish_layer_event(
            conn,
            layer="l2",
            event="changed",
            subject_ref=stable or obligation_id,
            payload={
                "change_event_id": chg_id,
                "obligation_id": stable or obligation_id,
                "derivation_key": obligation_id,
                "change": kind,
                "source": source_key,
                "cause_clause_ids": list(cause_clause_ids),
                "cause_l1_change_event_id": cause_l1_change_event_id,
                "effective_date": effective_date.isoformat() if effective_date else None,
                "effective_date_basis": effective_date_basis,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            },
            producer="l2.change",
        )
    return chg_id


def record_supersession(
    conn: Connection,
    *,
    old_obligation_id: str,
    new_obligation_id: str,
    cause_change_event_id: str | None,
    effective_date: date | None,
    note: str,
    why: record.WhyTrail | str,
) -> str:
    sup_id = next_id(conn, "SUP")
    record.write(
        conn,
        supersessions,
        {
            "id": sup_id,
            "old_obligation_id": old_obligation_id,
            "new_obligation_id": new_obligation_id,
            "cause_change_event_id": cause_change_event_id,
            "effective_date": effective_date,
            "note": note,
        },
        why=why,
        valid_from=effective_date or datetime.now(timezone.utc).date(),
    )
    return sup_id


def backfill_stable_ids_and_asserts(conn: Connection) -> dict:
    """Migration helper: give pre-HLD-v2 obligations a stable id, structured
    fields and an explicit asserts edge to their basis clause."""
    from app.clhear.l1.models import clauses, source_versions, sources

    rows = conn.execute(sa.select(obligations)).mappings().all()
    ids = edges = 0
    for ob in rows:
        if not ob["stable_id"]:
            ensure_stable_id(conn, ob["id"])
            ids += 1
        if not ob["determination"]:
            fields = structured_fields(ob["statement"] or "", ob["modality"] or "")
            conn.execute(obligations.update().where(obligations.c.id == ob["id"]).values(**fields))
        clause = conn.execute(
            sa.select(clauses.c.id, clauses.c.text, clauses.c.text_hash)
            .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(sources.c.key == ob["source_key"])
            .where(clauses.c.ref == ob["clause_ref"])
            .where(source_versions.c.status == "in_force")
            .order_by(source_versions.c.id.desc())
            .limit(1)
        ).first()
        if clause is None:
            continue
        has_edge = conn.execute(
            sa.select(asserts.c.id)
            .where(asserts.c.obligation_id == ob["id"])
            .where(asserts.c.clause_id == clause.id)
            .where(asserts.c.valid_to.is_(None))
            .limit(1)
        ).first()
        if has_edge:
            continue
        upsert_assert(
            conn,
            obligation_id=ob["id"],
            clause_id=clause.id,
            source_key=ob["source_key"],
            clause_ref=ob["clause_ref"],
            text=clause.text or "",
            text_hash=clause.text_hash,
            strength="explicit" if (ob["method"] or "").startswith("deterministic") else "implied",
            why=why_for(ob["id"], clause_id=clause.id, text_hash=clause.text_hash, method=ob["method"] or "",
                        confidence=float(ob["confidence"] or 0) or None,
                        summary="migration m0010: basis edge for pre-existing obligation"),
        )
        edges += 1
    return {"stable_ids": ids, "asserts": edges}


__all__ = [
    "AGENT",
    "backfill_stable_ids_and_asserts",
    "classify_type",
    "determination_text",
    "duty_span",
    "ensure_stable_id",
    "parse_structure",
    "record_change",
    "record_supersession",
    "resolve_obligation_id",
    "structured_fields",
    "upsert_assert",
    "why_for",
]
