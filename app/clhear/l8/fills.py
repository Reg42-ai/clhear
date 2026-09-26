# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L8 fill fleet (HLD v2 §4.8): generators, reviewers, drift detectors.

A *fill* is best-practice content for one L3 block slot — suggested policy text,
procedure steps, a typology / item set with thresholds, a workflow definition or
a role description. Generators draft fills from the block, its characteristics
and the obligations that require it (deterministic templates; the ``l8.fill``
task class on the router when a live model is available). Reviewers score the
expert rubric; ≥ 85 % endorses. Drift detectors re-derive a fill whose block or
obligations changed (a new version — nothing is deleted, I2).

Every fill traces to its block and obligations (I3) and is written through the
record path with a why-trail. L8 reads L3, L6 and L1 only (I1).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import blocks, characteristics, obligations, requires
from app.clhear.l3.kinds import required_fields
from app.clhear.l8.models import (FILL_KINDS, MATURITIES, METHOD_VERSION, PROVENANCES, REVIEW_DECISIONS, RUBRIC_CRITERIA,
                                  RUBRIC_MIN, fill_reviews, fills)
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l8.fills")

# Which fill kinds a block kind naturally carries (HLD §4.8: policy text, procedure
# steps, typology sets with thresholds, workflow definitions, role descriptions).
KIND_FILLS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # block kind -> ((slot, fill kind, title template), ...)
    "Document": (("policy_text", "text", "Suggested {name} policy language"),
                 ("review_cycle", "numeric", "Review cycle for {name}")),
    "Process": (("procedure_steps", "workflow", "Procedure steps for {name}"),
                ("frequency", "numeric", "Operating frequency for {name}")),
    "Workflow": (("workflow_definition", "workflow", "Workflow definition for {name}"),
                 ("escalation_sla", "numeric", "Escalation SLA for {name}")),
    "Role": (("role_description", "text", "Role description for {name}"),),
    "Body": (("committee_charter", "text", "Committee charter for {name}"),
             ("meeting_cadence", "numeric", "Meeting cadence for {name}")),
    "System": (("typology_set", "item_set", "Typology / rule set for {name}"),
               ("control_configuration", "text", "Configuration baseline for {name}")),
    "Configuration": (("parameter_set", "item_set", "Parameter set for {name}"),),
    "Asset": (("register_fields", "item_set", "Register fields for {name}"),),
}

# Numeric defaults (value, unit, [lo, hi], basis) used when no obligation states a figure.
NUMERIC_DEFAULTS: dict[str, tuple[float, str, list[float], str]] = {
    "review_cycle": (12, "months", [6, 24], "annual review is the common supervisory expectation"),
    "frequency": (1, "per month", [1, 12], "monthly operation unless the obligation states otherwise"),
    "escalation_sla": (5, "business days", [1, 10], "escalation within a working week"),
    "meeting_cadence": (4, "per year", [2, 12], "quarterly meetings"),
}


class InvalidFill(ValueError):
    pass


class NotEndorsable(ValueError):
    """The rubric score is below the endorsement threshold."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _plain(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    for k in ("content", "jurisdictions", "predicates", "obligation_ids", "source_refs", "drift", "rubric"):
        if k in out and isinstance(out[k], str):
            out[k] = _json(out[k], None)
    return out


# --------------------------------------------------------------------------- inputs (L3, L2 via requires)


def block_inputs(conn: Connection, block_id: str) -> dict | None:
    """The block, its live characteristics and the live obligations that require it."""
    b = conn.execute(sa.select(blocks).where(blocks.c.id == block_id, blocks.c.valid_to.is_(None))).mappings().first()
    if b is None:
        return None
    chars = {r["key"]: dict(r) for r in conn.execute(
        sa.select(characteristics).where(characteristics.c.block_id == block_id, characteristics.c.valid_to.is_(None))).mappings()}
    obs = [dict(r) for r in conn.execute(
        sa.select(obligations).select_from(requires.join(obligations, obligations.c.id == requires.c.obligation_id))
        .where(requires.c.block_id == block_id, requires.c.valid_to.is_(None), obligations.c.valid_to.is_(None))
        .order_by(obligations.c.id)).mappings()]
    seen: set[str] = set()
    obs = [o for o in obs if not (o["id"] in seen or seen.add(o["id"]))]
    return {"block": dict(b), "characteristics": chars, "obligations": obs}


def basis_hash(block: dict, obligation_rows: list[dict]) -> str:
    parts = [block["id"], block.get("name") or "", block.get("purpose") or block.get("description") or ""]
    parts += sorted(f"{o['id']}:{o.get('text_hash') or ''}" for o in obligation_rows)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _jurisdictions(obs: list[dict]) -> list[str]:
    return sorted({str(o.get("jurisdiction") or "") for o in obs if o.get("jurisdiction")})


def _why(block: dict, obs: list[dict], *, summary: str, confidence: float, method: str = "l8.fill") -> record.WhyTrail:
    return record.WhyTrail(
        layer="L8", subject_ref=block["id"], reasoning_summary=summary[:1000],
        evidence_refs=[{"block": block["id"]}, *({"obligation": o["id"], "text_hash": o.get("text_hash") or ""} for o in obs[:20])],
        inputs=(block["id"], *[o["id"] for o in obs[:20]]), agent_id=method, skill_version=METHOD_VERSION,
        confidence=confidence, input_layers=("L3", "L2"),
    )


FILL_FIELDS = ("id", "block_id", "slot", "kind", "title", "content", "provenance", "maturity", "jurisdictions", "predicates",
               "obligation_ids", "basis_hash", "source_refs", "rubric_score", "contributor", "contribution_id", "model", "drift", "status")


def reversion(conn: Connection, table: sa.Table, live: dict, new: dict, why: record.WhyTrail, *, reason: str) -> dict:
    """Close the live row and write the next version under the same id (I2, I11).

    ``invalidate`` bumps the closed row's version by one; the new live row sits above it."""
    trail = why.write(conn)
    record.invalidate(conn, table, sa.and_(table.c.id == live["id"], table.c.valid_to.is_(None)), why=trail, reason=reason)
    values = dict(new)
    values["id"] = live["id"]
    values["version"] = (live.get("version") or 1) + 2
    values.setdefault("derived_by", why.agent_id or "")
    values.setdefault("confidence", why.confidence)
    values["inputs_hash"] = record.inputs_hash(*why.inputs) if why.inputs else ""
    if why.model_manifest is not None:
        values["model_manifest"] = why.model_manifest
    return record.write(conn, table, values, why=trail)


# --------------------------------------------------------------------------- deterministic drafting


def _numeric_from_obligations(slot: str, obs: list[dict]) -> dict | None:
    """A figure stated in an obligation (e.g. 'within 5 business days', 'every 12 months')."""
    import re

    unit_words = {"day": "days", "days": "days", "business day": "business days", "business days": "business days",
                  "working day": "business days", "working days": "business days", "month": "months", "months": "months",
                  "year": "years", "years": "years", "hour": "hours", "hours": "hours"}
    pat = re.compile(r"\b(\d{1,3})\s+(business days?|working days?|days?|months?|years?|hours?)\b", re.I)
    for o in obs:
        text = f"{o.get('statement') or ''} {o.get('condition') or ''}"
        m = pat.search(text)
        if m:
            value = float(m.group(1))
            unit = unit_words.get(m.group(2).lower(), m.group(2).lower())
            return {"value": value, "unit": unit, "range": [value, value],
                    "basis": f"stated in {o.get('stable_id') or o['id']}: “{m.group(0)}”", "stated": True, "obligation": o["id"]}
    return None


def draft_content(slot: str, kind: str, block: dict, chars: dict, obs: list[dict]) -> dict:
    """Template drafting from the block's own characteristics and obligation statements — zero LLM."""
    name = block["name"]
    purpose = block.get("purpose") or block.get("description") or ""
    duties = [(o.get("statement") or o.get("title") or "").strip() for o in obs]
    duties = [d for d in duties if d][:8]
    val = lambda k: (chars.get(k) or {}).get("value") or ""  # noqa: E731
    if kind == "text":
        lines = [f"# {name}", "", purpose or f"{name} supports the obligations listed below.", ""]
        if slot == "role_description":
            lines += ["## Accountabilities"] + [f"- {d}" for d in duties] + ["", f"Reports to: {val('reports_to') or 'senior management'}."]
        elif slot == "committee_charter":
            lines += ["## Mandate"] + [f"- {d}" for d in duties] + ["", f"Membership: {val('membership') or 'as designated by senior management'}.",
                                                                    f"Quorum: {val('quorum') or 'a majority of members'}."]
        elif slot == "control_configuration":
            lines += ["## Baseline"] + [f"- {val(k) or k.replace('_', ' ')}" for k in required_fields(block["kind"])]
        else:
            lines += ["## Policy statement"] + [f"- {d}" for d in duties]
            lines += ["", "## Ownership", f"Owner: {val('owner') or 'the designated function'}. Approval: {val('approver') or 'senior management'}.",
                      "", "## Review", f"Reviewed at least every {NUMERIC_DEFAULTS['review_cycle'][0]:.0f} months and on every change to a cited obligation."]
        return {"text": "\n".join(lines)}
    if kind == "numeric":
        stated = _numeric_from_obligations(slot, obs)
        if stated:
            return stated
        value, unit, rng, basis = NUMERIC_DEFAULTS.get(slot, (1, "per year", [1, 4], "default"))
        return {"value": value, "unit": unit, "range": rng, "basis": basis, "stated": False}
    if kind == "item_set":
        items = []
        for o in obs[:12]:
            items.append({"name": (o.get("title") or o.get("action") or o["id"]).strip()[:120], "obligation": o["id"],
                          "threshold": None, "note": (o.get("condition") or "")[:200]})
        if not items:
            items = [{"name": k.replace("_", " "), "threshold": None, "note": val(k)[:200]} for k in required_fields(block["kind"])]
        return {"items": items}
    if kind == "workflow":
        steps = []
        for i, o in enumerate(obs[:10], 1):
            steps.append({"order": i, "name": (o.get("action") or o.get("title") or o["id"]).strip()[:120],
                          "role": val("owner") or val("performed_by") or "designated role",
                          "output": (o.get("object") or "evidence of completion")[:120], "obligation": o["id"]})
        if not steps:
            steps = [{"order": 1, "name": f"Trigger {name}", "role": "designated role", "output": "record"},
                     {"order": 2, "name": f"Perform {name}", "role": "designated role", "output": "evidence"},
                     {"order": 3, "name": "Review and sign off", "role": "second line", "output": "approval"}]
        return {"steps": steps}
    raise InvalidFill(f"unknown fill kind {kind}")


def _llm_draft(llm, slot: str, kind: str, block: dict, chars: dict, obs: list[dict]) -> tuple[dict | None, str]:
    """Structured drafting through the router's ``l8.fill`` task (procurement-clean ladder)."""
    if llm is None or not obs:
        return None, ""
    from app.clhear.platform.gateway import parse_json_object
    from app.clhear.platform.router import complete

    texts = "\n".join(f"- [{o.get('stable_id') or o['id']}] {o.get('statement') or o.get('title')}" for o in obs[:10])
    shape = {"text": '{"text": "<markdown>"}', "numeric": '{"value": <number>, "unit": "<unit>", "range": [lo, hi], "basis": "<why>"}',
             "item_set": '{"items": [{"name": "...", "threshold": <number|null>, "note": "..."}]}',
             "workflow": '{"steps": [{"order": 1, "name": "...", "role": "...", "output": "..."}]}'}[kind]
    prompt = (f"Building block: {block['name']} (kind {block['kind']}). Purpose: {block.get('purpose') or block.get('description')}\n"
              f"Slot: {slot}. Draft best-practice {kind} content that satisfies ONLY the obligations below; do not invent "
              f"requirements, quote thresholds only when a source states them. Characteristics: "
              f"{json.dumps({k: v.get('value') for k, v in chars.items()})}\nReturn JSON shaped {shape}.\n\nObligations:\n{texts}")
    try:
        result = complete(llm, "l8.fill", prompt=prompt, system="You draft compliance program content from legal text. JSON only.",
                          required_keys=[next(iter(json.loads(shape.replace('<markdown>', '""').replace('<number|null>', 'null')
                                                               .replace('<number>', '0').replace('lo, hi', '0, 0').replace('<unit>', '""')
                                                               .replace('<why>', '""')).keys()))], max_tokens=900)
        parsed = parse_json_object(result.text)
    except Exception:
        log.exception("l8.fill failed for %s/%s", block["id"], slot)
        return None, ""
    key = {"text": "text", "numeric": "value", "item_set": "items", "workflow": "steps"}[kind]
    if key not in parsed:
        return None, ""
    return parsed, getattr(result, "model", "") or ""


# --------------------------------------------------------------------------- write path


def create_fill(engine: Engine, *, block_id: str, slot: str, kind: str, content: dict, provenance: str = "agent",
                title: str = "", jurisdictions: list[str] | None = None, predicates: dict | None = None,
                source_refs: list | None = None, contributor: str = "", contribution_id: str | None = None,
                model: str = "deterministic", confidence: float = 0.6, summary: str = "") -> dict:
    """One fill, through the record path. Raises InvalidFill for unknown block / kind / provenance."""
    if kind not in FILL_KINDS:
        raise InvalidFill(f"kind must be one of {FILL_KINDS}")
    if provenance not in PROVENANCES:
        raise InvalidFill(f"provenance must be one of {PROVENANCES}")
    with engine.begin() as conn:
        inputs = block_inputs(conn, block_id)
        if inputs is None:
            raise InvalidFill(f"unknown block {block_id}")
        block, obs = inputs["block"], inputs["obligations"]
        row = {
            "id": next_id(conn, "FIL"), "block_id": block_id, "slot": slot, "kind": kind,
            "title": title or f"{slot.replace('_', ' ')} for {block['name']}", "content": content, "provenance": provenance,
            "maturity": "draft", "jurisdictions": jurisdictions or _jurisdictions(obs), "predicates": predicates or {},
            "obligation_ids": [o["id"] for o in obs], "basis_hash": basis_hash(block, obs), "source_refs": source_refs or [],
            "contributor": contributor, "contribution_id": contribution_id, "model": model, "status": "current",
        }
        why = _why(block, obs, summary=summary or f"{provenance} fill for {block_id}/{slot} ({kind}) drafted from "
                                                 f"{len(obs)} obligations; model {model}", confidence=confidence)
        out = record.write(conn, fills, row, why=why)
    return get(engine, out["id"])


def generate_fills(engine: Engine, llm=None, *, block_id: str | None = None, limit: int | None = None) -> dict:
    """Draft the standard slots for every current block that lacks a current fill in that slot."""
    with engine.connect() as conn:
        q = sa.select(blocks.c.id, blocks.c.kind).where(blocks.c.valid_to.is_(None), blocks.c.canonical_id.is_(None))
        if block_id:
            q = q.where(blocks.c.id == block_id)
        targets = [(r.id, r.kind) for r in conn.execute(q.order_by(blocks.c.id))]
        existing = {(r.block_id, r.slot) for r in conn.execute(
            sa.select(fills.c.block_id, fills.c.slot).where(fills.c.valid_to.is_(None), fills.c.status == "current"))}
    drafted, skipped, llm_used = [], 0, 0
    for bid, bkind in targets:
        for slot, fkind, title_t in KIND_FILLS.get(bkind, ()):
            if (bid, slot) in existing:
                skipped += 1
                continue
            if limit is not None and len(drafted) >= limit:
                break
            with engine.connect() as conn:
                inputs = block_inputs(conn, bid)
            if inputs is None or not inputs["obligations"]:
                continue  # nothing to trace to (I3) — the traceability gate would reject it
            block, chars, obs = inputs["block"], inputs["characteristics"], inputs["obligations"]
            content, model = _llm_draft(llm, slot, fkind, block, chars, obs)
            if content is None:
                content, model = draft_content(slot, fkind, block, chars, obs), "deterministic"
            else:
                llm_used += 1
            row = create_fill(engine, block_id=bid, slot=slot, kind=fkind, content=content, provenance="agent",
                              title=title_t.format(name=block["name"]), model=model or "deterministic",
                              confidence=0.7 if model and model != "deterministic" else 0.6)
            drafted.append(row["id"])
    return {"drafted": len(drafted), "skipped_existing": skipped, "llm": llm_used, "ids": drafted[:50], "method": METHOD_VERSION}


# --------------------------------------------------------------------------- review (rubric ≥ 85 %)


def rubric_score(rubric: dict) -> float:
    vals = []
    for c in RUBRIC_CRITERIA:
        v = rubric.get(c)
        if v is None:
            raise InvalidFill(f"rubric needs every criterion: {RUBRIC_CRITERIA}")
        v = float(v)
        if not 0.0 <= v <= 1.0:
            raise InvalidFill(f"rubric.{c} must be within 0..1")
        vals.append(v)
    return round(sum(vals) / len(vals), 3)


def review_fill(engine: Engine, fill_id: str, *, reviewer: str, rubric: dict, decision: str, note: str = "") -> dict:
    """Record an expert rubric review. `endorse` needs score ≥ RUBRIC_MIN and moves the fill to endorsed;
    `revise` leaves it reviewed; `reject` supersedes the current version (nothing is deleted)."""
    if decision not in REVIEW_DECISIONS:
        raise InvalidFill(f"decision must be one of {REVIEW_DECISIONS}")
    score = rubric_score(rubric)
    if decision == "endorse" and score < RUBRIC_MIN:
        raise NotEndorsable(f"score {score:.3f} is below the {RUBRIC_MIN:.0%} endorsement threshold")
    with engine.begin() as conn:
        row = conn.execute(sa.select(fills).where(fills.c.id == fill_id, fills.c.valid_to.is_(None))).mappings().first()
        if row is None:
            raise KeyError(fill_id)
        conn.execute(fill_reviews.insert().values(fill_id=fill_id, reviewer=reviewer, rubric=rubric, score=score,
                                                  decision=decision, note=note[:2000]))
        maturity = {"endorse": "endorsed", "revise": "reviewed", "reject": row["maturity"]}[decision]
        new = {k: _json(row[k], None) if k in ("content", "jurisdictions", "predicates", "obligation_ids", "source_refs", "drift") else row[k]
               for k in FILL_FIELDS}
        new.update({"maturity": maturity, "rubric_score": score, "status": "superseded" if decision == "reject" else "current"})
        inputs = block_inputs(conn, row["block_id"]) or {"block": {"id": row["block_id"], "name": ""}, "obligations": []}
        why = _why(inputs["block"], inputs["obligations"], method="l8.review", confidence=score,
                   summary=f"{reviewer} {decision}d {fill_id}: rubric {score:.3f} ({', '.join(f'{c}={rubric[c]}' for c in RUBRIC_CRITERIA)}). {note}")
        reversion(conn, fills, dict(row), new, why, reason=f"review: {decision}")
    return get(engine, fill_id) or history(engine, fill_id)[-1]


# --------------------------------------------------------------------------- drift


def detect_drift(engine: Engine, *, rederive: bool = True) -> dict:
    """A fill whose block or obligations changed (basis hash differs) is re-derived as a new
    draft version carrying the drift note; the previous version is closed, not deleted."""
    with engine.connect() as conn:
        current = [dict(r) for r in conn.execute(
            sa.select(fills).where(fills.c.valid_to.is_(None), fills.c.status == "current")).mappings()]
        drifted = []
        for f in current:
            inputs = block_inputs(conn, f["block_id"])
            if inputs is None:
                drifted.append((f, None, "block no longer current"))
                continue
            h = basis_hash(inputs["block"], inputs["obligations"])
            if h != f["basis_hash"]:
                drifted.append((f, inputs, "block or obligation text changed"))
    rederived = []
    for f, inputs, reason in drifted:
        if not rederive:
            continue
        with engine.begin() as conn:
            if inputs is None:
                new = {k: _json(f[k], None) if k in ("content", "jurisdictions", "predicates", "obligation_ids", "source_refs", "drift") else f[k]
                       for k in FILL_FIELDS}
                new.update({"maturity": "draft", "status": "superseded",
                            "drift": {"detected_at": _now().isoformat(), "reason": reason, "previous_basis_hash": f["basis_hash"]}})
                why = record.WhyTrail(layer="L8", subject_ref=f["block_id"], reasoning_summary=f"drift: {reason}; fill {f['id']} retired",
                                      evidence_refs=[{"fill": f["id"]}], inputs=(f["block_id"],), agent_id="l8.drift",
                                      skill_version=METHOD_VERSION, confidence=1.0, input_layers=("L3",))
                reversion(conn, fills, f, new, why, reason=f"drift: {reason}")
                rederived.append({"id": f["id"], "action": "retired", "reason": reason})
                continue
            block, chars, obs = inputs["block"], inputs["characteristics"], inputs["obligations"]
            content = _json(f["content"], {}) if f["provenance"] != "agent" else draft_content(f["slot"], f["kind"], block, chars, obs)
            new = {"id": f["id"], "block_id": f["block_id"], "slot": f["slot"], "kind": f["kind"], "title": f["title"], "content": content,
                   "provenance": f["provenance"], "maturity": "draft", "jurisdictions": _jurisdictions(obs), "predicates": _json(f["predicates"], {}),
                   "obligation_ids": [o["id"] for o in obs], "basis_hash": basis_hash(block, obs), "source_refs": _json(f["source_refs"], []),
                   "contributor": f["contributor"], "contribution_id": f["contribution_id"], "model": "deterministic" if f["provenance"] == "agent" else f["model"],
                   "rubric_score": None, "status": "current",
                   "drift": {"detected_at": _now().isoformat(), "reason": reason, "previous_basis_hash": f["basis_hash"],
                             "previous_maturity": f["maturity"]}}
            why = _why(block, obs, method="l8.drift", confidence=0.6,
                       summary=f"drift on {f['id']}: {reason}; re-derived from {len(obs)} obligations, maturity reset to draft")
            reversion(conn, fills, f, new, why, reason=f"drift: {reason}")
            rederived.append({"id": f["id"], "action": "rederived", "reason": reason, "previous_maturity": f["maturity"]})
    return {"checked": len(current), "drifted": len(drifted), "rederived": len(rederived), "details": rederived[:50]}


# --------------------------------------------------------------------------- reads


def get(engine: Engine, fill_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(fills).where(fills.c.id == fill_id, fills.c.valid_to.is_(None))).mappings().first()
        if row is None:
            return None
        out = _plain(row)
        out["reviews"] = [_plain(r) for r in conn.execute(
            sa.select(fill_reviews).where(fill_reviews.c.fill_id == fill_id).order_by(fill_reviews.c.id)).mappings()]
        out["versions"] = conn.execute(sa.select(sa.func.count()).select_from(fills).where(fills.c.id == fill_id)).scalar()
        why = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == row["why_trail_id"])).mappings().first()
        out["why"] = _plain(why) if why else None
    return out


def list_fills(engine: Engine, *, block_id: str | None = None, maturity: str | None = None, kind: str | None = None,
               provenance: str | None = None, limit: int = 200) -> list[dict]:
    q = sa.select(fills).where(fills.c.valid_to.is_(None), fills.c.status == "current").order_by(fills.c.block_id, fills.c.slot).limit(limit)
    if block_id:
        q = q.where(fills.c.block_id == block_id)
    if maturity:
        q = q.where(fills.c.maturity == maturity)
    if kind:
        q = q.where(fills.c.kind == kind)
    if provenance:
        q = q.where(fills.c.provenance == provenance)
    with engine.connect() as conn:
        return [_plain(r) for r in conn.execute(q).mappings()]


def history(engine: Engine, fill_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [_plain(r) for r in conn.execute(sa.select(fills).where(fills.c.id == fill_id).order_by(fills.c.version)).mappings()]


def availability(engine: Engine, *, block_ids: list[str] | None = None) -> dict:
    """Public metadata (I9): per block, how many fills exist and at which maturity — never the content."""
    q = sa.select(fills.c.block_id, fills.c.slot, fills.c.kind, fills.c.maturity, fills.c.provenance).where(
        fills.c.valid_to.is_(None), fills.c.status == "current")
    if block_ids:
        q = q.where(fills.c.block_id.in_(block_ids))
    per: dict[str, dict] = {}
    with engine.connect() as conn:
        for r in conn.execute(q):
            b = per.setdefault(r.block_id, {"block_id": r.block_id, "fills": 0, "by_maturity": {m: 0 for m in MATURITIES},
                                             "slots": [], "provenances": {p: 0 for p in PROVENANCES}})
            b["fills"] += 1
            b["by_maturity"][r.maturity] += 1
            b["provenances"][r.provenance] += 1
            b["slots"].append({"slot": r.slot, "kind": r.kind, "maturity": r.maturity})
    for b in per.values():
        b["fills_available"] = b["fills"] > 0
        b["endorsed"] = b["by_maturity"]["endorsed"]
    return {"blocks": sorted(per.values(), key=lambda b: b["block_id"]), "count": len(per),
            "note": "L8 fill content is member content (HLD v2 I9); existence and maturity are public."}


def summary(engine: Engine) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(fills.c.maturity, fills.c.provenance, fills.c.kind, sa.func.count().label("n"))
                            .where(fills.c.valid_to.is_(None), fills.c.status == "current")
                            .group_by(fills.c.maturity, fills.c.provenance, fills.c.kind)).all()
        endorsed = [float(r[0]) for r in conn.execute(sa.select(fills.c.rubric_score).where(
            fills.c.valid_to.is_(None), fills.c.status == "current", fills.c.maturity == "endorsed", fills.c.rubric_score.isnot(None)))]
    by_m = {m: 0 for m in MATURITIES}
    by_p = {p: 0 for p in PROVENANCES}
    by_k = {k: 0 for k in FILL_KINDS}
    for m, p, k, n in rows:
        by_m[m] += n
        by_p[p] += n
        by_k[k] += n
    return {"fills": sum(by_m.values()), "by_maturity": by_m, "by_provenance": by_p, "by_kind": by_k,
            "endorsed_mean_rubric": round(sum(endorsed) / len(endorsed), 3) if endorsed else None,
            "rubric_min": RUBRIC_MIN, "method": METHOD_VERSION}


def traceability(engine: Engine) -> dict:
    """Every current fill names a live block and ≥ 1 live obligation, and carries a why-trail."""
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(fills.c.id, fills.c.block_id, fills.c.obligation_ids, fills.c.why_trail_id)
                                              .where(fills.c.valid_to.is_(None), fills.c.status == "current")).mappings()]
        live_blocks = {r[0] for r in conn.execute(sa.select(blocks.c.id).where(blocks.c.valid_to.is_(None)))}
        live_obs = {r[0] for r in conn.execute(sa.select(obligations.c.id).where(obligations.c.valid_to.is_(None)))}
    untraced = []
    for r in rows:
        obs = _json(r["obligation_ids"], [])
        problems = []
        if r["block_id"] not in live_blocks:
            problems.append("block not live")
        if not obs or not all(o in live_obs for o in obs):
            problems.append("obligation missing or not live")
        if not r["why_trail_id"]:
            problems.append("no why-trail")
        if problems:
            untraced.append({"id": r["id"], "problems": problems})
    n = len(rows)
    return {"fills": n, "traced": n - len(untraced), "untraced": untraced[:20],
            "ratio": round((n - len(untraced)) / n, 4) if n else 1.0}


def rubric_gate(engine: Engine) -> dict:
    """Endorsed fills all scored ≥ RUBRIC_MIN by a recorded review."""
    with engine.connect() as conn:
        endorsed = [dict(r) for r in conn.execute(sa.select(fills.c.id, fills.c.rubric_score).where(
            fills.c.valid_to.is_(None), fills.c.status == "current", fills.c.maturity == "endorsed")).mappings()]
        reviewed_ids = {r[0] for r in conn.execute(sa.select(fill_reviews.c.fill_id).where(fill_reviews.c.decision == "endorse"))}
    bad = [f["id"] for f in endorsed if f["rubric_score"] is None or float(f["rubric_score"]) < RUBRIC_MIN or f["id"] not in reviewed_ids]
    return {"endorsed": len(endorsed), "below_threshold": bad[:20], "threshold": RUBRIC_MIN, "ok": not bad}
