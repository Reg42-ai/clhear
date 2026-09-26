# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Approval console (HLD v2 I4, §3, §8 item 11).

Agents propose; humans ratify — and what a human ratifies has to stick. Three
queues sit behind ``/console``:

* **low-confidence queue** — live determinations whose confidence is below the
  layer's threshold (:data:`record.LOW_CONFIDENCE_THRESHOLDS`), per layer, with a
  one-click *validate* that records a named maintainer vouching for the row at
  its current basis;
* **modification requests** — ``l2_modification`` / ``l3_modification`` proposals
  from the layer browsers and ``l2_review`` escalations from the second-model
  review; approving one applies the change through the record write path
  (versioned, why-trailed, never a delete) and stores a human edit;
* **contribution reviews** — ``community_*`` proposals (mirrored submissions),
  decided here and synced back to the contributor.

Every accepted decision lands in ``l0_platform.human_edits`` with the basis hash
it was taken on. :func:`reproduce_human_edits` runs after each layer's step in
the nightly stack: an edit whose row still has the same basis but no longer
carries the accepted value is **re-asserted** (status ``reproduced``); an edit
whose basis changed (the clause was amended, the obligation re-derived) is
**escalated** — a ``human_edit_conflict`` proposal shows the maintainer the old
edit next to the new derivation, and nothing is silently overwritten either way.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import (
    activities,
    applies_to,
    blocks,
    characteristics,
    obligation_reviews,
    obligations,
    requires,
)
from app.clhear.models import human_edits, proposals
from app.clhear.platform import events as l0_events
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.console")

MODIFICATION_KINDS = ("l2_modification", "l3_modification", "l2_review", "human_edit_conflict")
CONTRIBUTION_PREFIX = "community_"
QUEUE_LAYERS = ("L2", "L3", "L4", "L5")

# Fields a maintainer may change per layer table; anything else is refused at
# approval time rather than written blind.
L2_FIELDS = {"title", "statement", "determination", "subject", "action", "condition", "object",
             "obligation_type", "addressee", "modality", "jurisdiction", "effective_from", "effective_to", "status"}
L3_FIELDS = {"name", "purpose", "kind", "status", "description"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def _plain(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _conf(v) -> float | None:
    return None if v is None else float(v)


# --------------------------------------------------------------------------- low-confidence queue


def thresholds() -> dict[str, float]:
    return dict(record.LOW_CONFIDENCE_THRESHOLDS)


def _validated_refs(conn: Connection, layer: str) -> dict[str, str]:
    """subject_ref -> basis_hash of live validations for the layer."""
    rows = conn.execute(
        sa.select(human_edits.c.subject_ref, human_edits.c.basis_hash)
        .where(human_edits.c.layer == layer, human_edits.c.kind == "validation",
               human_edits.c.status.in_(("accepted", "reproduced")))
    ).all()
    return {r.subject_ref: r.basis_hash for r in rows}


def _queue_l2(conn: Connection, threshold: float) -> list[dict]:
    validated = _validated_refs(conn, "L2")
    out = []
    for r in conn.execute(
        sa.select(obligations.c.id, obligations.c.stable_id, obligations.c.title, obligations.c.confidence,
                  obligations.c.review_confidence, obligations.c.source_key, obligations.c.clause_ref,
                  obligations.c.text_hash, obligations.c.jurisdiction)
        .where(obligations.c.status == "derived")
    ).mappings():
        conf = r["confidence"]
        if not record.needs_human("L2", conf):
            continue
        if validated.get(r["id"]) == r["text_hash"]:
            continue
        out.append({
            "layer": "L2", "table": "obligations", "subject_ref": r["id"], "public_id": r["stable_id"] or r["id"],
            "title": r["title"], "confidence": _conf(conf), "review_confidence": _conf(r["review_confidence"]),
            "threshold": threshold, "basis_hash": r["text_hash"],
            "context": f"{r['source_key']} {r['clause_ref']} · {r['jurisdiction']}",
            "href": f"/l2#{r['stable_id'] or r['id']}",
        })
    return out


def _queue_l3(conn: Connection, threshold: float) -> list[dict]:
    validated = _validated_refs(conn, "L3")
    out = []
    for r in conn.execute(
        sa.select(requires.c.id, requires.c.obligation_id, requires.c.block_id, requires.c.confidence,
                  requires.c.method, requires.c.obligation_text_hash, blocks.c.name)
        .join(blocks, blocks.c.id == requires.c.block_id)
        .where(requires.c.valid_to.is_(None), requires.c.confidence.is_not(None), requires.c.confidence < threshold)
    ).mappings():
        if validated.get(r["id"]) == (r["obligation_text_hash"] or ""):
            continue
        out.append({
            "layer": "L3", "table": "requires", "subject_ref": r["id"], "public_id": r["id"],
            "title": f"{r['obligation_id']} requires {r['name']}", "confidence": _conf(r["confidence"]),
            "threshold": threshold, "basis_hash": r["obligation_text_hash"] or "",
            "context": f"method {r['method']}", "href": f"/l3#{r['block_id']}",
        })
    for r in conn.execute(
        sa.select(characteristics.c.id, characteristics.c.block_id, characteristics.c.key, characteristics.c.value,
                  characteristics.c.confidence, characteristics.c.method, blocks.c.name)
        .join(blocks, blocks.c.id == characteristics.c.block_id)
        .where(characteristics.c.valid_to.is_(None), characteristics.c.status == "unbacked")
    ).mappings():
        ref = f"CHR-{r['id']}"
        if ref in validated:
            continue
        out.append({
            "layer": "L3", "table": "characteristics", "subject_ref": ref, "public_id": f"{r['block_id']}/{r['key']}",
            "title": f"{r['name']} · {r['key']} = {r['value']!s}", "confidence": _conf(r["confidence"]),
            "threshold": threshold, "basis_hash": "",
            "context": f"unbacked ({r['method']}) — value not grounded in the backing text", "href": f"/l3#{r['block_id']}",
        })
    return out


def _queue_l4(conn: Connection, threshold: float) -> list[dict]:
    validated = _validated_refs(conn, "L4")
    out = []
    for r in conn.execute(
        sa.select(applies_to.c.id, applies_to.c.obligation_id, applies_to.c.predicate, applies_to.c.basis,
                  applies_to.c.confidence, applies_to.c.obligation_text_hash)
        .where(applies_to.c.valid_to.is_(None), applies_to.c.confidence.is_not(None), applies_to.c.confidence < threshold)
    ).mappings():
        if validated.get(r["id"]) == (r["obligation_text_hash"] or ""):
            continue
        out.append({
            "layer": "L4", "table": "applies_to", "subject_ref": r["id"], "public_id": r["id"],
            "title": f"{r['obligation_id']} applies to {json.dumps(_json(r['predicate'], {}), sort_keys=True)}",
            "confidence": _conf(r["confidence"]), "threshold": threshold, "basis_hash": r["obligation_text_hash"] or "",
            "context": f"basis {r['basis']}", "href": f"/l2#{r['obligation_id']}",
        })
    return out


def _queue_l5(conn: Connection, threshold: float) -> list[dict]:
    validated = _validated_refs(conn, "L5")
    out = []
    for r in conn.execute(
        sa.select(activities.c.id, activities.c.name, activities.c.side, activities.c.confidence, activities.c.inputs_hash)
        .where(activities.c.valid_to.is_(None), activities.c.confidence.is_not(None), activities.c.confidence < threshold)
    ).mappings():
        if validated.get(r["id"]) == (r["inputs_hash"] or ""):
            continue
        out.append({
            "layer": "L5", "table": "activities", "subject_ref": r["id"], "public_id": r["id"],
            "title": r["name"], "confidence": _conf(r["confidence"]), "threshold": threshold,
            "basis_hash": r["inputs_hash"] or "", "context": f"{r['side']} activity", "href": f"/l5#{r['id']}",
        })
    return out


_QUEUES = {"L2": _queue_l2, "L3": _queue_l3, "L4": _queue_l4, "L5": _queue_l5}


def low_confidence_queue(engine: Engine, *, layer: str | None = None, limit: int = 200) -> dict:
    """Live determinations below their layer threshold, oldest-basis first is not
    knowable, so lowest confidence first (None sorts first: no confidence at all)."""
    th = thresholds()
    layers = [layer.upper()] if layer else list(QUEUE_LAYERS)
    items: list[dict] = []
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for lay in layers:
            fn = _QUEUES.get(lay)
            if fn is None:
                continue
            rows = fn(conn, th[lay])
            counts[lay] = len(rows)
            items.extend(rows)
    items.sort(key=lambda i: (i["confidence"] is not None, float(i["confidence"] or 0), i["subject_ref"]))
    return {"thresholds": {k: th[k] for k in layers}, "counts": counts, "total": len(items), "items": items[:limit]}


# --------------------------------------------------------------------------- human edits ledger


def _record_edit(conn: Connection, *, layer: str, kind: str, table_name: str, subject_ref: str, field: str,
                 before, after, basis_hash: str, accepted_by: str, proposal_id: str | None, rationale: str) -> str:
    # a newer decision on the same (row, field) supersedes the older one
    conn.execute(
        human_edits.update()
        .where(human_edits.c.layer == layer, human_edits.c.subject_ref == subject_ref, human_edits.c.field == field,
               human_edits.c.kind == kind, human_edits.c.status.in_(("accepted", "reproduced", "escalated")))
        .values(status="superseded", last_outcome="superseded by a later decision", last_checked_at=_now())
    )
    eid = next_id(conn, "EDT")
    conn.execute(human_edits.insert().values(
        id=eid, layer=layer, kind=kind, table_name=table_name, subject_ref=subject_ref, field=field,
        before=_plain(before), after=_plain(after), basis_hash=basis_hash or "", proposal_id=proposal_id,
        accepted_by=accepted_by, accepted_at=_now(), rationale=rationale[:1000], status="accepted",
    ))
    l0_events.emit(conn, layer=layer, kind="clhear.l0.human_edit", subject_ref=subject_ref,
                   payload={"edit_id": eid, "kind": kind, "field": field, "by": accepted_by}, producer="platform.console")
    return eid


def list_edits(engine: Engine, *, status: str | None = None, layer: str | None = None, limit: int = 200) -> list[dict]:
    q = sa.select(human_edits).order_by(human_edits.c.accepted_at.desc(), human_edits.c.id.desc()).limit(limit)
    if status:
        q = q.where(human_edits.c.status == status)
    if layer:
        q = q.where(human_edits.c.layer == layer.upper())
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings()]


def _why(layer: str, subject_ref: str, summary: str, *, approver: str, inputs=(), confidence=None) -> record.WhyTrail:
    return record.WhyTrail(layer=layer, subject_ref=subject_ref, reasoning_summary=summary, agent_id=f"human:{approver}",
                           skill_version="console-v1", inputs=tuple(inputs), confidence=confidence,
                           evidence_refs=[{"kind": "maintainer", "id": approver}])


def _append_review(row_review, event: dict) -> list:
    rv = _json(row_review, [])
    return list(rv) + [event]


# --------------------------------------------------------------------------- validate (queue)


def validate(engine: Engine, *, layer: str, subject_ref: str, approver: str, note: str = "") -> dict:
    """A named maintainer vouches for a low-confidence determination as it stands.
    L2 rows are promoted to ``validated`` (the existing named-human gate); every
    layer gets a ``validated`` review event and a human edit at the current basis."""
    with engine.begin() as conn:
        return _validate(conn, layer=layer, subject_ref=subject_ref, approver=approver, note=note)


def _validate(conn: Connection, *, layer: str, subject_ref: str, approver: str, note: str = "") -> dict:
    layer = layer.upper()
    if layer == "L2":
        from app.clhear.l2 import registry

        oid = registry.resolve_obligation_id(conn, subject_ref) or subject_ref
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().first()
        if ob is None:
            raise KeyError(subject_ref)
        trail = _why("L2", oid, f"maintainer validated the obligation as derived. {note}".strip(), approver=approver,
                     inputs=(ob["text_hash"],), confidence=1.0).write(conn)
        conn.execute(obligations.update().where(obligations.c.id == oid).values(
            status="validated" if ob["status"] == "derived" else ob["status"], validated_by=approver, validated_at=_now(),
            why_trail_id=trail, version=(ob["version"] or 1) + 1,
            review=_append_review(ob["review"], {"event": "validated", "by": approver, "at": _now().isoformat(),
                                                 "why_trail_id": trail, "note": note})))
        eid = _record_edit(conn, layer="L2", kind="validation", table_name="obligations", subject_ref=oid, field="",
                           before=ob["status"], after="validated", basis_hash=ob["text_hash"], accepted_by=approver,
                           proposal_id=None, rationale=note)
        l0_events.emit(conn, layer="L2", kind="ObligationValidated", subject_ref=oid,
                       payload={"approver": approver, "via": "console"}, producer="platform.console")
        return {"edit_id": eid, "layer": "L2", "subject_ref": oid, "status": "validated"}
    table, key, basis_col = {
        "L3": (requires, requires.c.id, "obligation_text_hash"),
        "L4": (applies_to, applies_to.c.id, "obligation_text_hash"),
        "L5": (activities, activities.c.id, "inputs_hash"),
    }.get(layer, (None, None, None))
    if layer == "L3" and subject_ref.startswith("CHR-"):
        table, key, basis_col = characteristics, characteristics.c.id, None
        subject_key: Any = int(subject_ref[4:])
    else:
        subject_key = subject_ref
    if table is None:
        raise ValueError(f"no low-confidence queue for {layer}")
    row = conn.execute(sa.select(table).where(key == subject_key, table.c.valid_to.is_(None))).mappings().first()
    if row is None:
        raise KeyError(subject_ref)
    basis = (row[basis_col] or "") if basis_col else ""
    trail = _why(layer, subject_ref, f"maintainer validated the determination as derived. {note}".strip(),
                 approver=approver, inputs=(basis,), confidence=1.0).write(conn)
    values = {"review": _append_review(row["review"], {"event": "validated", "by": approver, "at": _now().isoformat(),
                                                       "why_trail_id": trail, "note": note}),
              "why_trail_id": trail, "version": (row["version"] or 1) + 1}
    if table is characteristics:
        values["status"] = "backed"
        values["method"] = "human-validated"
        values["backing_span"] = row["backing_span"] or note
    conn.execute(table.update().where(key == subject_key).values(**values))
    eid = _record_edit(conn, layer=layer, kind="validation", table_name=table.name, subject_ref=subject_ref, field="",
                       before=None, after="validated", basis_hash=basis, accepted_by=approver, proposal_id=None,
                       rationale=note)
    return {"edit_id": eid, "layer": layer, "subject_ref": subject_ref, "status": "validated"}


# --------------------------------------------------------------------------- apply approved modifications


def _apply_l2_field(conn: Connection, *, oid: str, field: str, value, approver: str, proposal_id: str | None,
                    rationale: str) -> dict:
    ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().first()
    if ob is None:
        raise KeyError(oid)
    if field not in L2_FIELDS:
        raise ValueError(f"field {field!r} is not editable on obligations")
    before = ob[field]
    trail = _why("L2", oid, f"maintainer edit: {field} — {rationale}".strip(" —"), approver=approver,
                 inputs=(ob["text_hash"], field, value), confidence=1.0).write(conn)
    values: dict[str, Any] = {field: value, "why_trail_id": trail, "version": (ob["version"] or 1) + 1,
                              "review": _append_review(ob["review"], {"event": "human_edit", "field": field, "by": approver,
                                                                       "at": _now().isoformat(), "why_trail_id": trail,
                                                                       "proposal_id": proposal_id})}
    if field in ("effective_from", "effective_to") and isinstance(value, str):
        values[field] = datetime.fromisoformat(value).date() if value else None
    conn.execute(obligations.update().where(obligations.c.id == oid).values(**values))
    eid = _record_edit(conn, layer="L2", kind="field_edit", table_name="obligations", subject_ref=oid, field=field,
                       before=before, after=value, basis_hash=ob["text_hash"], accepted_by=approver,
                       proposal_id=proposal_id, rationale=rationale)
    return {"edit_id": eid, "subject_ref": oid, "field": field, "before": _plain(before), "after": value}


def _apply_l3_field(conn: Connection, *, block_id: str, field: str, value, approver: str, proposal_id: str | None,
                    rationale: str) -> dict:
    b = conn.execute(sa.select(blocks).where(blocks.c.id == block_id)).mappings().first()
    if b is None:
        raise KeyError(block_id)
    if field.startswith("characteristic:"):
        key = field.split(":", 1)[1]
        live = conn.execute(sa.select(characteristics).where(characteristics.c.block_id == block_id,
                                                              characteristics.c.key == key,
                                                              characteristics.c.valid_to.is_(None))).mappings().first()
        before = live["value"] if live else None
        why = _why("L3", block_id, f"maintainer set characteristic {key} — {rationale}".strip(" —"), approver=approver,
                   inputs=(block_id, key, value), confidence=1.0)
        trail = why.write(conn)
        if live is not None:
            record.invalidate(conn, characteristics, characteristics.c.id == live["id"], why=trail,
                              reason=f"replaced by maintainer edit ({proposal_id or 'console'})")
        record.write(conn, characteristics, {"block_id": block_id, "key": key, "value": str(value), "status": "backed",
                                             "backing_obligation_id": None, "backing_span": rationale[:500],
                                             "method": "human-edit", "confidence": 1.0},
                     why=trail, valid_from=_now().date())
        eid = _record_edit(conn, layer="L3", kind="field_edit", table_name="characteristics", subject_ref=block_id,
                           field=field, before=before, after=str(value), basis_hash="", accepted_by=approver,
                           proposal_id=proposal_id, rationale=rationale)
        return {"edit_id": eid, "subject_ref": block_id, "field": field, "before": before, "after": str(value)}
    if field.startswith("requires:"):
        from app.clhear.l3 import decompose

        ob_ref = field.split(":", 1)[1]
        from app.clhear.l2 import registry

        oid = registry.resolve_obligation_id(conn, ob_ref) or ob_ref
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().first()
        if ob is None:
            raise KeyError(ob_ref)
        action = str(value).lower()
        why = _why("L3", block_id, f"maintainer {action} requires {oid} -> {block_id} — {rationale}".strip(" —"),
                   approver=approver, inputs=(ob["text_hash"], block_id, action), confidence=1.0)
        trail = why.write(conn)
        if action in ("add", "link", "true"):
            decompose.link(conn, obligation=dict(ob), block_id=block_id, method="human-edit", why=trail, rationale=rationale)
            after = "linked"
        elif action in ("remove", "unlink", "false"):
            record.invalidate(conn, requires, sa.and_(requires.c.obligation_id == oid, requires.c.block_id == block_id,
                                                      requires.c.valid_to.is_(None)), why=trail,
                              reason=f"maintainer removed the edge ({proposal_id or 'console'})")
            after = "unlinked"
        else:
            raise ValueError("requires:<obligation> takes add | remove")
        eid = _record_edit(conn, layer="L3", kind="field_edit", table_name="requires", subject_ref=block_id, field=field,
                           before=None, after=after, basis_hash=ob["text_hash"], accepted_by=approver,
                           proposal_id=proposal_id, rationale=rationale)
        return {"edit_id": eid, "subject_ref": block_id, "field": field, "after": after}
    if field not in L3_FIELDS:
        raise ValueError(f"field {field!r} is not editable on blocks")
    before = b[field]
    trail = _why("L3", block_id, f"maintainer edit: {field} — {rationale}".strip(" —"), approver=approver,
                 inputs=(block_id, field, value), confidence=1.0).write(conn)
    conn.execute(blocks.update().where(blocks.c.id == block_id).values(
        **{field: value}, why_trail_id=trail, version=(b["version"] or 1) + 1,
        review=_append_review(b["review"], {"event": "human_edit", "field": field, "by": approver,
                                            "at": _now().isoformat(), "why_trail_id": trail, "proposal_id": proposal_id})))
    eid = _record_edit(conn, layer="L3", kind="field_edit", table_name="blocks", subject_ref=block_id, field=field,
                       before=before, after=value, basis_hash="", accepted_by=approver, proposal_id=proposal_id,
                       rationale=rationale)
    return {"edit_id": eid, "subject_ref": block_id, "field": field, "before": before, "after": value}


def _apply_l2_review(conn: Connection, *, proposal: dict, decision: str, approver: str) -> dict:
    """An ``l2_review`` escalation says the second model doubts the obligation.
    Approving agrees (expert verdict *incorrect*, obligation rejected); rejecting
    the escalation clears it (expert verdict *correct*). Either way the expert
    verdict overrides the model in ``precision()``."""
    from app.clhear.l2 import registry

    draft = _json(proposal.get("draft"), {})
    oid = registry.resolve_obligation_id(conn, draft.get("derivation_key") or draft.get("obligation_id") or
                                         proposal["subject_ref"]) or proposal["subject_ref"]
    ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().first()
    if ob is None:
        raise KeyError(oid)
    verdict = "incorrect" if decision == "approved" else "correct"
    conn.execute(obligation_reviews.insert().values(
        obligation_id=oid, reviewer_kind="expert", reviewer=approver, verdict=verdict, confidence=1.0,
        text_hash=ob["text_hash"], notes=f"console decision on {proposal['id']}", derived_by=f"human:{approver}",
        reviewed_at=_now()))
    trail = _why("L2", oid, f"expert verdict {verdict} on review escalation {proposal['id']}", approver=approver,
                 inputs=(ob["text_hash"], verdict), confidence=1.0).write(conn)
    values: dict[str, Any] = {"review_confidence": 1.0, "why_trail_id": trail, "version": (ob["version"] or 1) + 1,
                              "review": _append_review(ob["review"], {"event": "expert_verdict", "verdict": verdict,
                                                                       "by": approver, "at": _now().isoformat(),
                                                                       "why_trail_id": trail})}
    if verdict == "incorrect":
        values.update(status="rejected", validated_by=approver, validated_at=_now())
    elif ob["status"] == "derived":
        values.update(status="validated", validated_by=approver, validated_at=_now())
    conn.execute(obligations.update().where(obligations.c.id == oid).values(**values))
    eid = _record_edit(conn, layer="L2", kind="verdict", table_name="obligations", subject_ref=oid, field="status",
                       before=ob["status"], after=values.get("status", ob["status"]), basis_hash=ob["text_hash"],
                       accepted_by=approver, proposal_id=proposal["id"], rationale=proposal.get("rationale") or "")
    return {"edit_id": eid, "subject_ref": oid, "verdict": verdict, "status": values.get("status", ob["status"])}


def apply_decision(engine: Engine, proposal: dict, *, approver: str, override: dict | None = None) -> dict | None:
    """Side effects of deciding a modification-kind proposal. Returns the edit made
    (or None when the kind has no console side effect)."""
    kind = proposal.get("kind")
    decision = proposal.get("status")
    draft = _json(proposal.get("draft"), {})
    if override:
        draft = {**draft, **override}
    with engine.begin() as conn:
        if kind == "l2_review":
            return _apply_l2_review(conn, proposal=proposal, decision=decision, approver=approver)
        if kind == "human_edit_conflict" and decision != "approved":
            edit_id = (draft.get("edit") or {}).get("id")
            if edit_id:
                conn.execute(human_edits.update().where(human_edits.c.id == edit_id).values(
                    status="superseded", last_checked_at=_now(),
                    last_outcome=f"derivation stands: {approver} let the new basis override the edit ({proposal['id']})"))
            return {"edit_id": edit_id, "outcome": "derivation stands"}
        if decision != "approved":
            return None
        rationale = proposal.get("rationale") or ""
        if kind == "l2_modification":
            from app.clhear.l2 import registry

            oid = registry.resolve_obligation_id(conn, draft.get("derivation_key") or draft.get("obligation_id") or
                                                 proposal["subject_ref"]) or proposal["subject_ref"]
            return _apply_l2_field(conn, oid=oid, field=draft["field"], value=draft.get("proposed_value"),
                                   approver=approver, proposal_id=proposal["id"], rationale=rationale)
        if kind == "l3_modification":
            return _apply_l3_field(conn, block_id=draft.get("block_id") or proposal["subject_ref"], field=draft["field"],
                                   value=draft.get("proposed_value"), approver=approver, proposal_id=proposal["id"],
                                   rationale=rationale)
        if kind == "human_edit_conflict":
            # re-accepting the edit on the new basis: the original edit is superseded by a fresh one
            edit = draft.get("edit") or {}
            if edit.get("layer") == "L2":
                from app.clhear.l2 import registry

                oid = registry.resolve_obligation_id(conn, edit["subject_ref"]) or edit["subject_ref"]
                if edit.get("kind") == "field_edit":
                    out = _apply_l2_field(conn, oid=oid, field=edit["field"], value=edit.get("after"), approver=approver,
                                          proposal_id=proposal["id"], rationale=f"re-accepted after basis change: {rationale}")
                else:
                    out = _validate(conn, layer="L2", subject_ref=oid, approver=approver,
                                    note=f"re-accepted after basis change: {rationale}")
                return out
            if edit.get("layer") == "L3" and edit.get("kind") == "field_edit":
                return _apply_l3_field(conn, block_id=edit["subject_ref"], field=edit["field"], value=edit.get("after"),
                                       approver=approver, proposal_id=proposal["id"],
                                       rationale=f"re-accepted after basis change: {rationale}")
    return None


def decide(engine: Engine, proposal_id: str, decision: str, approver: str, *, override: dict | None = None) -> dict:
    """The one decision path for every proposal kind: status flip (l0_proposals),
    then the kind's side effect — community sync, concept apply, or the console's
    modification apply — recorded as a human edit."""
    pending = l0_proposals.get_proposal(engine, proposal_id)
    if pending is not None and pending.get("kind") == "community_contribution":
        # HLD v2 §6 / I12: a console decision is one reviewer's vote; the contribution
        # flow keeps the two-reviewer rule and flips this proposal itself when it lands.
        from app.clhear.platform import contributions

        contribution = contributions.review_from_console(engine, pending, decision=decision, approver=approver)
        decided = l0_proposals.get_proposal(engine, proposal_id) or pending
        decided["contribution"] = contribution
        return decided
    action = l0_proposals.approve if decision == "approved" else l0_proposals.reject
    decided = action(engine, proposal_id, approver)
    kind = str(decided.get("kind", ""))
    if kind.startswith(CONTRIBUTION_PREFIX):
        from app.clhear import community

        community.sync_submission_from_proposal(engine, decided)
        if decided.get("status") == "approved":
            with engine.begin() as conn:
                _record_edit(conn, layer=str(decided.get("layer") or "community"), kind="contribution",
                             table_name="proposals", subject_ref=decided["subject_ref"], field=kind, before=None,
                             after=_json(decided.get("draft"), {}), basis_hash="", accepted_by=approver,
                             proposal_id=proposal_id, rationale=decided.get("rationale") or "")
    if kind == "l2_concept" and decided.get("status") == "approved":
        from app.clhear.l2.consolidate import apply_approved_concept

        decided["concept"] = apply_approved_concept(engine, decided)
    if kind in MODIFICATION_KINDS:
        try:
            decided["edit"] = apply_decision(engine, decided, approver=approver, override=override)
        except (KeyError, ValueError) as exc:
            decided["edit_error"] = str(exc)
            log.warning("console: proposal %s decided but not applied: %s", proposal_id, exc)
    return decided


# --------------------------------------------------------------------------- reproduce or escalate


def _current_value(conn: Connection, edit: dict) -> tuple[Any, str, bool]:
    """(current value, current basis hash, row exists) for a human edit's target."""
    table_name, ref, field = edit["table_name"], edit["subject_ref"], edit["field"]
    if table_name == "obligations":
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == ref)).mappings().first()
        if ob is None:
            return None, "", False
        if edit["kind"] == "validation":
            return ob["status"], ob["text_hash"], True
        if edit["kind"] == "verdict":
            return ob["status"], ob["text_hash"], True
        return _plain(ob[field]), ob["text_hash"], True
    if table_name == "blocks":
        b = conn.execute(sa.select(blocks).where(blocks.c.id == ref)).mappings().first()
        return (None, "", False) if b is None else (b[field], "", True)
    if table_name == "characteristics" and field.startswith("characteristic:"):
        key = field.split(":", 1)[1]
        live = conn.execute(sa.select(characteristics.c.value).where(characteristics.c.block_id == ref,
                                                                     characteristics.c.key == key,
                                                                     characteristics.c.valid_to.is_(None))).first()
        return (live[0] if live else None), "", True
    if table_name == "requires" and field.startswith("requires:"):
        from app.clhear.l2 import registry

        oid = registry.resolve_obligation_id(conn, field.split(":", 1)[1]) or field.split(":", 1)[1]
        ob = conn.execute(sa.select(obligations.c.text_hash).where(obligations.c.id == oid)).first()
        live = conn.execute(sa.select(requires.c.id).where(requires.c.obligation_id == oid, requires.c.block_id == ref,
                                                          requires.c.valid_to.is_(None))).first()
        return ("linked" if live else "unlinked"), (ob[0] if ob else ""), ob is not None
    # generic validations on edge tables: the row is either still live at the same basis or not
    table, key, basis_col = {
        "requires": (requires, requires.c.id, "obligation_text_hash"),
        "applies_to": (applies_to, applies_to.c.id, "obligation_text_hash"),
        "activities": (activities, activities.c.id, "inputs_hash"),
        "characteristics": (characteristics, characteristics.c.id, None),
    }.get(table_name, (None, None, None))
    if table is None:
        return None, "", False
    subject_key: Any = int(ref[4:]) if table is characteristics and ref.startswith("CHR-") else ref
    row = conn.execute(sa.select(table).where(key == subject_key)).mappings().first()
    if row is None:
        return None, "", False
    return ("live" if row["valid_to"] is None else "invalidated"), (row[basis_col] or "" if basis_col else ""), True


def _escalate(conn: Connection, edit: dict, *, current, current_basis: str) -> str:
    return l0_proposals.create_proposal(
        conn, layer=edit["layer"], kind="human_edit_conflict", subject_ref=edit["subject_ref"],
        draft={"edit": {k: _plain(v) for k, v in edit.items() if k in ("id", "layer", "kind", "table_name", "subject_ref",
                                                                       "field", "before", "after", "basis_hash",
                                                                       "accepted_by", "accepted_at", "rationale")},
               "current_value": _plain(current), "current_basis_hash": current_basis},
        rationale=(f"basis changed since {edit['accepted_by']} accepted this edit ({edit['basis_hash'][:8]} -> "
                   f"{current_basis[:8]}); the new derivation reads {current!r}. Re-accept or let the derivation stand."),
        confidence=None,
    )


def reproduce_human_edits(engine: Engine, *, layer: str | None = None) -> dict:
    """Nightly (and on demand): every accepted edit is checked against the live row.

    * same basis, value intact  → ``checks`` += 1 (nothing to do)
    * same basis, value lost    → re-asserted through the same write path (a fresh edit
      supersedes the old one, which keeps the lineage in ``last_outcome``)
    * basis changed             → ``escalated`` with a ``human_edit_conflict`` proposal
    * row gone / invalidated    → ``escalated`` too (a human decision must not vanish quietly)
    """
    q = sa.select(human_edits).where(human_edits.c.status.in_(("accepted", "reproduced")))
    if layer:
        q = q.where(human_edits.c.layer == layer.upper())
    out = {"checked": 0, "intact": 0, "reproduced": 0, "escalated": 0, "details": []}
    with engine.connect() as conn:
        edits = [dict(r) for r in conn.execute(q.order_by(human_edits.c.id)).mappings()]
    for edit in edits:
        with engine.begin() as conn:
            current, basis, exists = _current_value(conn, edit)
            out["checked"] += 1
            outcome, status = "intact", edit["status"]
            if not exists or (edit["kind"] != "field_edit" and current in ("invalidated",)):
                pid = _escalate(conn, edit, current=current, current_basis=basis)
                outcome, status = f"escalated: row missing or invalidated ({pid})", "escalated"
                conn.execute(human_edits.update().where(human_edits.c.id == edit["id"]).values(escalation_proposal_id=pid))
            elif basis != (edit["basis_hash"] or "") and (edit["basis_hash"] or basis):
                pid = _escalate(conn, edit, current=current, current_basis=basis)
                outcome, status = f"escalated: basis changed ({pid})", "escalated"
                conn.execute(human_edits.update().where(human_edits.c.id == edit["id"]).values(escalation_proposal_id=pid))
            elif edit["kind"] == "field_edit" and _plain(current) != _plain(edit["after"]):
                _reassert(conn, edit)
                outcome, status = "reproduced: value re-asserted (see the newer edit)", "superseded"
            elif edit["kind"] in ("validation", "verdict") and edit["table_name"] == "obligations" \
                    and current != edit["after"] and current == "derived":
                # a re-derivation on the same text reset the status: the human decision stands
                conn.execute(obligations.update().where(obligations.c.id == edit["subject_ref"]).values(
                    status=edit["after"], validated_by=edit["accepted_by"], validated_at=_now()))
                outcome, status = "reproduced: status re-asserted", "reproduced"
            conn.execute(human_edits.update().where(human_edits.c.id == edit["id"]).values(
                checks=(edit["checks"] or 0) + 1, last_checked_at=_now(), last_outcome=outcome, status=status))
        key = outcome.split(":")[0]
        out[key if key in out else "intact"] += 1
        if outcome != "intact":
            out["details"].append({"edit_id": edit["id"], "subject_ref": edit["subject_ref"], "outcome": outcome})
    return out


def _reassert(conn: Connection, edit: dict) -> None:
    by = edit["accepted_by"]
    rationale = f"reproduced human edit {edit['id']} (accepted by {by})"
    if edit["table_name"] == "obligations":
        _apply_l2_field(conn, oid=edit["subject_ref"], field=edit["field"], value=edit["after"], approver=by,
                        proposal_id=edit.get("proposal_id"), rationale=rationale)
    elif edit["table_name"] in ("blocks", "characteristics", "requires"):
        _apply_l3_field(conn, block_id=edit["subject_ref"], field=edit["field"],
                        value=edit["after"] if edit["table_name"] != "requires" else
                        ("add" if edit["after"] == "linked" else "remove"),
                        approver=by, proposal_id=edit.get("proposal_id"), rationale=rationale)


# --------------------------------------------------------------------------- summary


def summary(engine: Engine) -> dict:
    with engine.connect() as conn:
        pending = conn.execute(sa.select(proposals.c.kind, sa.func.count()).where(proposals.c.status == "proposed")
                               .group_by(proposals.c.kind)).all()
        edits = conn.execute(sa.select(human_edits.c.status, sa.func.count()).group_by(human_edits.c.status)).all()
    by_kind = {k: n for k, n in pending}
    queue = low_confidence_queue(engine, limit=0)
    return {
        "thresholds": thresholds(),
        "low_confidence": queue["counts"],
        "modification_requests": sum(n for k, n in by_kind.items() if k in MODIFICATION_KINDS),
        "contribution_reviews": sum(n for k, n in by_kind.items() if k.startswith(CONTRIBUTION_PREFIX)),
        "other_proposals": sum(n for k, n in by_kind.items()
                               if k not in MODIFICATION_KINDS and not k.startswith(CONTRIBUTION_PREFIX)),
        "pending_by_kind": by_kind,
        "human_edits": {s: n for s, n in edits},
    }


def list_requests(engine: Engine, *, status: str = "proposed", contributions: bool = False, limit: int = 200) -> list[dict]:
    q = sa.select(proposals).order_by(proposals.c.created_at.desc()).limit(limit)
    if status:
        q = q.where(proposals.c.status == status)
    if contributions:
        q = q.where(proposals.c.kind.like(f"{CONTRIBUTION_PREFIX}%"))
    else:
        q = q.where(proposals.c.kind.in_(MODIFICATION_KINDS))
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings()]
