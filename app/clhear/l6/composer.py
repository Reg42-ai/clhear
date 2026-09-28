# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L6 program composer — deterministic set-cover over the real L2–L5 state (HLD v2 §4.6).

compose(profile) is a pure function of (profile facts, L2 registry, L3
blocks + requires edges + characteristics, L4 applicability, L5 junction):
same inputs => same blueprint. The program is the *leanest complete* one:

* hard constraints — every applicable obligation is satisfied by ≥ 1 item;
  a block an obligation *requires* (L3 edge) is mandatory (basis ``required``);
* minimisation — among blocks whose curated selectors satisfy the remaining
  obligations, a greedy set cover picks the fewest (basis ``selected``) and a
  pruning pass removes any item that became redundant;
* proof — for every item the obligations only it satisfies (load-bearing)
  and what would become a gap if it were removed; a program is *minimal*
  when no selected item is redundant.

Gaps are surfaced, never silently accepted. Compositions are stored under a
``BLU-`` id with their items (``ITM-``) and minimality proof; an earlier
blueprint for the same profile is superseded, never deleted (I2).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import BLOCK_KINDS
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import blueprint_items, blueprints, minimality_proofs, obligations
from app.clhear.derived_models import characteristics as characteristics_t
from app.clhear.derived_models import operates as operates_t
from app.clhear.derived_models import requires as requires_t
from app.clhear.l4.predicates import LIVE_STATUS
from app.clhear.l6.models import ENGINE_VERSION, composition_hash, fingerprint
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l6")

AGENT = "l6.compose"
_KIND_ORDER = {k: i for i, k in enumerate(BLOCK_KINDS)}


def when_matches(when: dict, attributes: dict) -> bool:
    """Trigger condition evaluator — the L4 predicate language shared with
    ``applies_to`` edges and validity rules ("*" = present; list = any-of;
    scalar = equality / containment, case-insensitive)."""
    from app.clhear.l4.ontology import matches

    return matches(when, attributes)


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def _l4_applicable(conn, attributes: dict) -> list[dict]:
    """Obligations whose live L4 applies_to edges all match the profile (HLD v2 §4.4)."""
    from app.clhear.l4.predicates import obligations_for_attributes

    try:
        return obligations_for_attributes(conn, attributes)
    except sa.exc.OperationalError:  # pre-m0012 database
        return []


def _duty_quote(ob: dict) -> dict | None:
    found = _json(ob.get("evidence"), {}) or {}
    return found.get("duty") if isinstance(found, dict) else None


def _triggers(ob: dict) -> list[dict]:
    """The duty's conditions that time it rather than decide whether it applies."""
    from app.clhear.l4.predicates import conditions_of

    _, triggers = conditions_of({**ob, "evidence": _json(ob.get("evidence"), {}) or {}})
    return [{"text": t["text"], "evidence": t["quotes"]} for t in triggers]


def _edge_view(e: dict) -> dict:
    return {"requires": e["predicate"], "basis": e["basis"], "rationale": e["rationale"],
            "evidence": _json(e.get("evidence"), {}) or {}}


def _verdict_item(verdict: dict) -> dict:
    ob, es = verdict["obligation"], verdict["edges"]
    return {"derivation_key": ob["id"], "obligation_id": ob.get("stable_id") or ob["id"], "source_key": ob["source_key"],
            "clause_ref": ob["clause_ref"], "title": ob["title"], "status": ob["status"],
            "confidence": float(ob.get("confidence") or 0),
            "duty": ob.get("determination") or ob.get("statement") or "",
            "evidence": _duty_quote(ob), "triggers": _triggers(ob),
            "predicates": [{"predicate": e["predicate"], "basis": e["basis"], "rationale": e["rationale"]} for e in es]}


def _question(e: dict) -> dict:
    """What a profile must answer to decide one open edge."""
    pred, found = e["predicate"] or {}, _json(e.get("evidence"), {}) or {}
    if "jurisdictions" in pred:
        return {"key": "jurisdictions", "ask": "Where do you operate?", "value": pred["jurisdictions"],
                "evidence": [found["source"]] if found.get("source") else []}
    if "roles" in pred:
        labels = found.get("labels") or {}
        names = [labels.get(r, r) for r in pred["roles"]]
        return {"key": "roles", "ask": "Are you " + " or ".join(f"'{n}'" for n in names) + "?", "value": pred["roles"],
                "evidence": found.get("subject") or []}
    if "condition" in pred:
        return {"key": "conditions", "id": pred["condition"], "ask": f"Does this hold for you: {e['rationale']}?",
                "value": pred.get("fact"), "evidence": found.get("condition") or []}
    return {"key": "retired", "ask": "This duty carries an applicability edge from an earlier version; rebuild the scope.",
            "value": pred, "evidence": []}


def resolve_anchor_in(conn: Connection, anchor: dict) -> list[dict]:
    """Anchor {source_key, refs[]} -> derived obligation rows (may be empty:
    the anchor's source may be restricted or its clauses not duty-detected)."""
    query = sa.select(obligations).where(obligations.c.source_key == anchor["source_key"])
    refs = anchor.get("refs")
    if refs:
        query = query.where(obligations.c.clause_ref.in_(refs))
    # same live-status contract as L4 / L5: stale (revoked) and rejected duties leave the program
    rows = conn.execute(query.where(obligations.c.status.in_(LIVE_STATUS)).order_by(obligations.c.id)).mappings().all()
    return [dict(r) for r in rows]


def resolve_anchor(engine: Engine, anchor: dict) -> list[dict]:
    with engine.connect() as conn:
        return resolve_anchor_in(conn, anchor)


def _live_requires(conn) -> dict[str, list[dict]]:
    """obligation id -> [{block_id, rationale, method}] with a live L3 ``requires`` edge."""
    out: dict[str, list[dict]] = {}
    try:
        rows = conn.execute(
            sa.select(requires_t.c.obligation_id, requires_t.c.block_id, requires_t.c.rationale, requires_t.c.method)
            .where(requires_t.c.valid_to.is_(None)).order_by(requires_t.c.id)
        ).all()
    except sa.exc.OperationalError:  # pre-m0011 database
        return out
    for oid, bid, rationale, method in rows:
        out.setdefault(oid, []).append({"block_id": bid, "rationale": rationale or "", "method": method or ""})
    return out


def _live_characteristics(conn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    try:
        rows = conn.execute(
            sa.select(characteristics_t).where(characteristics_t.c.valid_to.is_(None)).order_by(characteristics_t.c.key)
        ).mappings().all()
    except sa.exc.OperationalError:
        return out
    for r in rows:
        out.setdefault(r["block_id"], []).append(
            {"id": r["id"], "key": r["key"], "value": r["value"], "status": r["status"],
             "backing_obligation_id": r["backing_obligation_id"], "backing_span": r["backing_span"] or "",
             "evidence": ((_json(r.get("evidence"), {}) or {}).get("value") or [])})
    return out


def _live_operates(conn) -> dict[str, list[dict]]:
    """block id -> compliance activities that operate it (L5 ``operates`` edges)."""
    out: dict[str, list[dict]] = {}
    try:
        rows = conn.execute(
            sa.select(operates_t.c.activity_id, operates_t.c.block_id, operates_t.c.obligation_refs, activities_t.c.name)
            .join(activities_t, activities_t.c.id == operates_t.c.activity_id)
            .where(operates_t.c.valid_to.is_(None)).order_by(operates_t.c.id)
        ).all()
    except sa.exc.OperationalError:  # pre-m0013 database
        return out
    for aid, bid, refs, name in rows:
        out.setdefault(bid, []).append({"activity_id": aid, "name": name, "obligation_refs": _json(refs, [])})
    return out


def _selector_covers(selector: dict, obligation: dict) -> bool:
    if selector["source_key"] != obligation["source_key"]:
        return False
    refs = selector.get("refs")
    return not refs or obligation["clause_ref"] in refs


def _canonical(blocks_by_id: dict[str, dict], block: dict | None) -> dict | None:
    seen = set()
    while block is not None and block.get("canonical_id") and block["canonical_id"] in blocks_by_id and block["id"] not in seen:
        seen.add(block["id"])
        block = blocks_by_id[block["canonical_id"]]
    return block


def _block_sort_key(b: dict) -> tuple:
    return (0 if b.get("status") == "curated" else 1, b["id"])


# ----------------------------------------------------------------- the pure composition


def _minimise(triggered: dict[str, dict], candidates: dict[str, list[dict]], required: dict[str, list[dict]],
              blocks_by_id: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Return (selected block ids in pick order, pick log). Hard-required
    blocks first, then a greedy set cover over what they leave uncovered,
    then a pruning pass so no selected item is redundant."""
    selected: list[str] = []
    for oid in sorted(triggered):
        for r in required.get(oid, ()):
            if r["block_id"] not in selected:
                selected.append(r["block_id"])
    def covered_by_selection(oid: str, chosen: list[str]) -> bool:
        return any(c["id"] in chosen for c in candidates.get(oid, ()))
    uncovered = {oid for oid in triggered if candidates.get(oid) and not covered_by_selection(oid, selected)}
    log_: list[str] = [f"required:{b}" for b in selected]
    while uncovered:
        # most uncovered obligations first; curated before derived; then lowest id (deterministic)
        ranked = []
        for bid in sorted({c["id"] for oid in uncovered for c in candidates[oid]}):
            gain = sorted(oid for oid in uncovered if any(c["id"] == bid for c in candidates[oid]))
            ranked.append((-len(gain), 0 if blocks_by_id.get(bid, {}).get("status") == "curated" else 1, bid, gain))
        ranked.sort()
        _, _, best, best_gain = ranked[0]
        selected.append(best)
        log_.append(f"selected:{best} covers {len(best_gain)}")
        uncovered -= set(best_gain)
    # pruning: a selected (non-required) block whose obligations are all satisfied by others is redundant
    required_ids = {r["block_id"] for rs in required.values() for r in rs}
    for bid in reversed([b for b in selected if b not in required_ids]):
        others = [b for b in selected if b != bid]
        if all(covered_by_selection(oid, others) for oid in triggered if any(c["id"] == bid for c in candidates.get(oid, ()))):
            selected.remove(bid)
            log_.append(f"pruned:{bid}")
    return selected, log_


def compose(engine: Engine, profile: dict, requested_by: str = "", release: str = "", log_request: bool = True) -> dict:
    """Profile facts -> tailored blueprint with items, minimality proof, explicit gaps + provenance.

    Pure given the store; ``log_request`` persists it as a ``BLU-`` blueprint."""
    with engine.connect() as conn:
        result = compose_with(conn, profile, release=release)
    if log_request:
        with engine.begin() as conn:
            result["blueprint_id"] = store_blueprint(conn, result, profile, requested_by=requested_by, release=release)["stable_id"]
    return result


def compose_in(conn: Connection, profile: dict, *, requested_by: str = "", release: str = "") -> dict:
    """Compose and store inside the caller's transaction (migrations, workers)."""
    result = compose_with(conn, profile, release=release)
    result["blueprint_id"] = store_blueprint(conn, result, profile, requested_by=requested_by, release=release)["stable_id"]
    return result


def _scope_source_keys(profile: dict) -> tuple[str | None, list[str] | None]:
    """(scope name, source keys) the blueprint is for; (None, None) = the whole store."""
    if profile.get("source_keys") is not None:
        return profile.get("scope") or None, sorted(profile["source_keys"])
    from app.clhear.l1 import scopes

    keys = scopes.keys()
    return (scopes.active_name() or None, sorted(keys)) if keys is not None else (None, None)


def compose_with(conn: Connection, profile: dict, *, release: str = "") -> dict:
    attributes = profile.get("attributes", {}) or {}
    wanted_activities = profile.get("activities")  # None = evaluate all curated
    profile_id = profile.get("profile_id")
    scope_name, scope_keys = _scope_source_keys(profile)
    # Host blueprints: every duty in scope gets an explicit verdict from L4.
    verdicts = None
    if wanted_activities is None:
        from app.clhear.l4.predicates import applicability

        try:
            verdicts = applicability(conn, attributes, source_keys=scope_keys)
        except sa.exc.OperationalError:  # pre-m0012 database
            verdicts = None

    activity_rows = [dict(r) for r in conn.execute(sa.select(activities_t).order_by(activities_t.c.id)).mappings()]
    block_rows = [dict(r) for r in conn.execute(sa.select(blocks_t).order_by(blocks_t.c.id)).mappings()]
    requires_edges = _live_requires(conn)
    if verdicts is not None:
        l4_applicable = [_verdict_item(v) for v in verdicts.values() if v["applies"]]
    else:
        l4_applicable = _l4_applicable(conn, attributes) if wanted_activities is None else []
    chars = _live_characteristics(conn)
    operated = _live_operates(conn)
    for a in activity_rows:
        a["triggers"] = _json(a.get("triggers"), [])
    for b in block_rows:
        b["satisfies"] = _json(b.get("satisfies"), [])
        b["evidence_artifacts"] = _json(b.get("evidence_artifacts"), [])
    blocks_by_id = {b["id"]: b for b in block_rows}
    activities_by_id = {a["id"]: a for a in activity_rows}

    withheld = set(profile.get("withheld") or ())
    if verdicts is not None and withheld:
        verdicts = {oid: v for oid, v in verdicts.items() if oid not in withheld}
        l4_applicable = [i for i in l4_applicable if i["derivation_key"] not in withheld]
    triggered: dict[str, dict] = {}  # obligation id -> {obligation, activities, conditions}
    unresolved_anchors: list[dict] = []
    # L4 applicability edges trigger directly (every edge matched the profile).
    for item in l4_applicable:
        ob = {"id": item["derivation_key"], "source_key": item["source_key"], "clause_ref": item["clause_ref"],
              "title": item["title"], "status": item["status"], "confidence": item["confidence"],
              "stable_id": item["obligation_id"], "duty": item.get("duty", ""), "evidence": item.get("evidence"),
              "triggers": item.get("triggers") or []}
        slot = triggered.setdefault(ob["id"], {"obligation": ob, "activities": [], "conditions": []})
        slot["activities"].append("L4:applies_to")
        slot["conditions"].append({k: v for p in item["predicates"] for k, v in p["predicate"].items()})
    for act in activity_rows if verdicts is None else ():
        if wanted_activities is not None and act["id"] not in wanted_activities:
            continue
        if act.get("valid_to") is not None:
            continue
        for trigger in act["triggers"]:
            if not when_matches(trigger.get("when", {}) or {}, attributes):
                continue
            resolved = resolve_anchor_in(conn, trigger["anchor"])
            if not resolved:
                unresolved_anchors.append(
                    {"activity": act["id"], "anchor": trigger["anchor"],
                     "reason": "no derived obligation at this anchor (restricted source or non-duty clause)"}
                )
            for ob in resolved:
                if verdicts is not None and not (ob["id"] in verdicts and verdicts[ob["id"]]["applies"]):
                    continue  # an activity never adds a duty that does not apply, or one outside the scope
                slot = triggered.setdefault(ob["id"], {"obligation": ob, "activities": [], "conditions": []})
                if act["id"] not in slot["activities"]:
                    slot["activities"].append(act["id"])
                    slot["conditions"].append(trigger.get("when", {}) or {})

    # Candidates per obligation: curated selectors (alternatives) + L3 requires edges (mandatory).
    candidates: dict[str, list[dict]] = {}
    required: dict[str, list[dict]] = {}
    for oid, slot in triggered.items():
        ob = slot["obligation"]
        # A scope's blueprint uses only its derived requires edges; selectors are a pre-0.2 catalog's.
        cands = [] if verdicts is not None else [
            b for b in block_rows if b.get("valid_to") is None and not b.get("canonical_id")
            and any(_selector_covers(sel, ob) for sel in b["satisfies"])]
        for edge in requires_edges.get(oid, ()):
            b = _canonical(blocks_by_id, blocks_by_id.get(edge["block_id"]))
            if b is None or b["id"] in withheld:
                continue
            required.setdefault(oid, []).append({**edge, "block_id": b["id"]})
            if b["id"] not in {c["id"] for c in cands}:
                cands.append(b)
        candidates[oid] = sorted(cands, key=_block_sort_key)

    selected, pick_log = _minimise(triggered, candidates, required, blocks_by_id)
    selected_set = set(selected)

    coverage = []
    for oid, slot in sorted(triggered.items()):
        ob = slot["obligation"]
        cands = candidates[oid]
        satisfied_by = [c["id"] for c in cands if c["id"] in selected_set]
        coverage.append(
            {
                "obligation_id": oid,
                "stable_id": ob.get("stable_id"),
                "source_key": ob["source_key"],
                "clause_ref": ob["clause_ref"],
                "title": ob["title"],
                "duty": ob.get("duty") or "",
                "evidence": ob.get("evidence"),
                "triggers": ob.get("triggers") or [],
                "status": ob["status"],
                "confidence": float(ob["confidence"] or 0),
                "triggered_by": slot["activities"],
                "conditions": slot["conditions"],
                "state": "covered" if cands else "gap",
                "covered_by": [b["id"] for b in cands],
                "satisfied_by": satisfied_by,
                "required_blocks": [r["block_id"] for r in required.get(oid, ())],
            }
        )
    by_oid = {c["obligation_id"]: c for c in coverage}

    # Items: one per selected block, characteristics resolved for this profile.
    items: list[dict] = []
    for bid in sorted(selected_set, key=lambda b: (_KIND_ORDER.get(blocks_by_id[b].get("kind"), 99), b)):
        b = blocks_by_id[bid]
        satisfied = sorted(oid for oid, c in by_oid.items() if bid in c["covered_by"])
        required_for = sorted(oid for oid in satisfied if bid in by_oid[oid]["required_blocks"])
        only_here = sorted(oid for oid in satisfied if by_oid[oid]["satisfied_by"] == [bid])
        load_bearing = sorted(set(required_for) | set(only_here))
        resolved_chars = []
        for ch in chars.get(bid, []):
            if f"characteristic:{ch['id']}" in withheld:
                continue
            backing = ch["backing_obligation_id"]
            resolved_chars.append({**ch, "in_profile": backing is None or backing in triggered or ch["status"] != "backed"})
        items.append(
            {
                "block_id": bid,
                "kind": b.get("kind") or "Process",
                "name": b["name"],
                "purpose": b.get("purpose") or "",
                "capability": b.get("capability") or "",
                "status": b.get("status"),
                "evidence": _json(b.get("evidence"), {}) or {},
                "basis": "required" if required_for else "selected",
                "obligations_satisfied": satisfied,
                "required_by": required_for,
                "load_bearing_for": load_bearing,
                "characteristics": resolved_chars,
                "activities_operated": [
                    {"activity_id": o["activity_id"], "name": o["name"],
                     "obligation_refs": [r for r in o["obligation_refs"] if r in by_oid or r in {c.get("stable_id") for c in coverage}]}
                    for o in operated.get(bid, []) if o["activity_id"] not in withheld
                ],
                "evidence_artifacts": b["evidence_artifacts"],
                "triggered_by": sorted({a for oid in satisfied for a in by_oid[oid]["triggered_by"]}),
            }
        )
    from app.clhear.l6.explain import explain_item

    for it in items:
        it["explanation"] = explain_item(it, by_oid, attributes, blocks_by_id, activities_by_id)

    # Minimality proof.
    proof = []
    for it in items:
        gaps_if_removed = [oid for oid in it["obligations_satisfied"] if by_oid[oid]["satisfied_by"] == [it["block_id"]]]
        redundant = it["basis"] == "selected" and not it["load_bearing_for"]
        proof.append({
            "block_id": it["block_id"], "basis": it["basis"], "load_bearing_for": it["load_bearing_for"],
            "removal_impact": {"gaps": gaps_if_removed, "still_covered": [oid for oid in it["obligations_satisfied"] if oid not in gaps_if_removed]},
            "redundant": redundant,
        })
    minimality = {
        "checked": True,
        "minimal": not any(p["redundant"] for p in proof),
        "items": len(items),
        "required": sum(1 for i in items if i["basis"] == "required"),
        "selected": sum(1 for i in items if i["basis"] == "selected"),
        "load_bearing": sum(1 for p in proof if p["load_bearing_for"]),
        "redundant": [p["block_id"] for p in proof if p["redundant"]],
        "proof": proof,
        "pick_log": pick_log,
    }

    # Honesty sweep: derived obligations in matching jurisdictions that no
    # curated activity anchors yet — the long tail is visible, not hidden.
    jurisdictions = set(attributes.get("jurisdictions", []) or [])
    unmapped_count = 0
    unmapped_sample = []
    if jurisdictions and verdicts is None:
        rows = conn.execute(
            sa.select(obligations.c.id, obligations.c.source_key, obligations.c.clause_ref, obligations.c.title)
            .where(obligations.c.jurisdiction.in_(jurisdictions))
            .where(obligations.c.status.in_(LIVE_STATUS))
            .order_by(obligations.c.id)
        ).all()
        for row in rows:
            if row.id not in triggered:
                unmapped_count += 1
                if len(unmapped_sample) < 25:
                    unmapped_sample.append(
                        {"obligation_id": row.id, "source_key": row.source_key,
                         "clause_ref": row.clause_ref, "title": row.title}
                    )

    not_applicable, undetermined = [], []
    questions: dict[str, dict] = {}
    for oid, verdict in sorted((verdicts or {}).items()):
        ob = verdict["obligation"]
        head = {"obligation_id": oid, "stable_id": ob.get("stable_id"), "source_key": ob["source_key"],
                "clause_ref": ob["clause_ref"], "title": ob["title"], "duty": ob.get("determination") or "",
                "evidence": _duty_quote(ob)}
        if verdict.get("state") == "not_applicable":
            not_applicable.append({**head, "because": [_edge_view(e) for e in verdict["failed"]]})
        elif verdict.get("state") == "undetermined":
            asked = [_question(e) for e in verdict["open"]]
            undetermined.append({**head, "questions": asked})
            for q in asked:
                key = json.dumps({k: q.get(k) for k in ("key", "id", "value")}, sort_keys=True, default=str)
                slot = questions.setdefault(key, {**q, "duties": []})
                slot["duties"].append(ob.get("stable_id") or oid)
    open_questions = sorted(questions.values(), key=lambda q: (q["key"], -len(q["duties"]), str(q["value"])))
    gaps = _evidence_gaps(conn, scope_name, coverage, items, verdicts) if verdicts is not None else []
    states = [c["state"] for c in coverage]
    program = {}
    for it in items:
        program.setdefault(it["kind"], []).append(it["block_id"])
    result = {
        "engine_version": ENGINE_VERSION,
        "release": release,
        "profile_id": profile_id,
        "fingerprint": fingerprint(attributes, wanted_activities, scope=scope_keys),
        "scope": {"name": scope_name, "source_keys": scope_keys} if scope_keys is not None else None,
        "profile_attributes": attributes,
        "activities_evaluated": [a["id"] for a in activity_rows if wanted_activities is None or a["id"] in wanted_activities],
        "obligations_triggered": len(coverage),
        "coverage": coverage,
        "blocks": [
            {"id": b["id"], "name": b["name"], "kind": b.get("kind"), "capability": b["capability"],
             "evidence_artifacts": b["evidence_artifacts"]}
            for b in (blocks_by_id[bid] for bid in sorted(selected_set))
        ],
        "items": items,
        "program": {k: program[k] for k in BLOCK_KINDS if k in program},
        "minimality": minimality,
        "coverage_summary": {
            "covered": states.count("covered"),
            "gaps": states.count("gap"),
            "total": len(states),
            "not_applicable": len(not_applicable),
            "undetermined": len(undetermined),
        },
        "not_applicable": not_applicable,
        "undetermined": undetermined,
        "open_questions": open_questions,
        "evidence_gaps": gaps,
        "unresolved_anchors": unresolved_anchors,
        "unmapped_obligations": {"count": unmapped_count, "sample": unmapped_sample,
                                 "note": "derived obligations in your jurisdictions not yet mapped to any activity — visible by design"},
    }
    result["composition_hash"] = composition_hash(result)
    return result


def _evidence_gaps(conn: Connection, scope_name: str | None, coverage: list[dict], items: list[dict],
                   verdicts: dict) -> list[dict]:
    """What the texts in scope could not support for this blueprint, and which
    sources would. Only gaps about duties that apply or may apply, the measures
    chosen, and the scope as a whole are shown."""
    from app.clhear import evidence

    relevant = {oid for oid, v in verdicts.items() if v.get("state") != "not_applicable"}
    chosen = {it["block_id"] for it in items}
    out = []
    for gap in evidence.gaps_for(conn, scope_name or "") if scope_name else []:
        subject = gap["subject"]
        if subject.startswith("OBL:") and subject not in relevant:
            continue
        if gap["kind"] == "characteristic_unspecified" and subject.split(":", 1)[0] not in chosen:
            continue
        out.append(gap)
    return out


# ----------------------------------------------------------------- persistence (I2: supersede, never delete)


def _why(subject_ref: str, summary: str, evidence: list[str], *, inputs: tuple = ()) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L6", reasoning_summary=summary, evidence_refs=evidence, inputs=(subject_ref, *inputs),
        model_manifest={"model": "deterministic", "method": ENGINE_VERSION}, skill_version=AGENT,
        confidence=1.0, agent_id=AGENT, subject_ref=subject_ref, input_layers=("L2", "L3", "L4", "L5"),
    )


def _current_for(conn: Connection, fp: str) -> dict | None:
    row = conn.execute(
        sa.select(blueprints).where(blueprints.c.fingerprint == fp, blueprints.c.status == "current")
        .order_by(blueprints.c.id.desc())
    ).mappings().first()
    return dict(row) if row else None


def store_blueprint(conn: Connection, result: dict, profile: dict, *, requested_by: str = "", release: str = "") -> dict:
    """Persist a composition as a ``BLU-`` blueprint with its items and proof.

    Same profile + same composition + same release => the current blueprint is
    reused (idempotent). A different composition supersedes the current one
    (status ``superseded``, ``valid_to`` closed) — the old rows stay for
    history and diff."""
    fp = result["fingerprint"]
    current = _current_for(conn, fp)
    if current:
        comp = _json(current.get("composition"), {}) or {}
        if comp.get("composition_hash") == result["composition_hash"] and (current.get("release") or "") == (release or ""):
            return {"stable_id": current["stable_id"], "reused": True, "superseded": None, "item_ids": {}}
    blu = next_id(conn, "BLU")
    evidence = [c["obligation_id"] for c in result["coverage"][:50]]
    trail = _why(blu, f"{ENGINE_VERSION}: {len(result['items'])} items cover {result['coverage_summary']['covered']}/"
                      f"{result['coverage_summary']['total']} applicable obligations "
                      f"({result['minimality']['required']} required, {result['minimality']['selected']} selected; "
                      f"minimal={result['minimality']['minimal']})",
                 evidence, inputs=(fp, result["composition_hash"])).write(conn)
    today = datetime.now(timezone.utc).date()
    superseded = None
    if current:
        record.invalidate(conn, blueprints, blueprints.c.id == current["id"], why=trail,
                          reason=f"superseded by {blu}: composition changed")
        conn.execute(blueprints.update().where(blueprints.c.id == current["id"]).values(status="superseded"))
        superseded = current["stable_id"]
    stored = {k: v for k, v in result.items() if k != "blueprint_id"}
    record.write(conn, blueprints, dict(
        requested_by=requested_by, release=release, profile=profile,
        result={"coverage_summary": result["coverage_summary"], "obligations_triggered": result["obligations_triggered"],
                "blocks": [b["id"] for b in result["blocks"]], "obligation_ids": [c["obligation_id"] for c in result["coverage"]],
                "activities": result["activities_evaluated"], "minimal": result["minimality"]["minimal"]},
        engine_version=ENGINE_VERSION, stable_id=blu, profile_id=result.get("profile_id"), fingerprint=fp,
        composition=stored, status="current",
    ), why=trail, valid_from=today, jurisdictions=list(result["profile_attributes"].get("jurisdictions", []) or []))
    item_ids: dict[str, str] = {}
    for it in result["items"]:
        iid = next_id(conn, "ITM")
        item_ids[it["block_id"]] = iid
        record.write(conn, blueprint_items, dict(
            id=iid, blueprint_id=blu, block_id=it["block_id"], kind=it["kind"], name=it["name"], basis=it["basis"],
            characteristics=it["characteristics"], obligations_satisfied=it["obligations_satisfied"],
            activities_operated=[a["activity_id"] for a in it["activities_operated"]],
            load_bearing_for=it["load_bearing_for"], explanation=it["explanation"],
        ), why=trail, valid_from=today)
    for p in result["minimality"]["proof"]:
        record.write(conn, minimality_proofs, dict(
            blueprint_id=blu, item_id=item_ids[p["block_id"]], block_id=p["block_id"],
            load_bearing_for=p["load_bearing_for"], removal_impact=p["removal_impact"], redundant=p["redundant"],
        ), why=trail, valid_from=today)
    return {"stable_id": blu, "reused": False, "superseded": superseded, "item_ids": item_ids}


def compose_for_profile(engine: Engine, profile_id: str, *, requested_by: str = "l6.compose", release: str = "",
                        log_request: bool = True, withheld=None) -> dict:
    """Compose for a stored L4 profile (``PRF-``); raises KeyError when unknown."""
    from app.clhear.l4 import validate as l4_validate

    with engine.connect() as conn:
        row = l4_validate.get_profile(conn, profile_id)
    if row is None:
        raise KeyError(profile_id)
    attributes = _json(row["attributes"], {})
    return compose(engine, {"attributes": attributes, "activities": None, "profile_id": profile_id,
                            "withheld": sorted(withheld or ())},
                   requested_by=requested_by, release=release, log_request=log_request)


def get_blueprint(conn: Connection, blueprint_id: str) -> dict | None:
    """Stored blueprint by ``BLU-`` id: composition + stored item ids + proof rows + history."""
    row = conn.execute(sa.select(blueprints).where(blueprints.c.stable_id == blueprint_id)).mappings().first()
    if row is None:
        return None
    comp = _json(row["composition"], {}) or {}
    items = {r["block_id"]: dict(r) for r in conn.execute(
        sa.select(blueprint_items).where(blueprint_items.c.blueprint_id == blueprint_id)).mappings()}
    for it in comp.get("items", []):
        stored = items.get(it["block_id"])
        it["id"] = stored["id"] if stored else None
    proof = [dict(r) for r in conn.execute(
        sa.select(minimality_proofs).where(minimality_proofs.c.blueprint_id == blueprint_id)).mappings()]
    for p in proof:
        p["load_bearing_for"] = _json(p["load_bearing_for"], [])
        p["removal_impact"] = _json(p["removal_impact"], {})
    return {
        "blueprint_id": blueprint_id, "status": row["status"], "profile_id": row["profile_id"], "fingerprint": row["fingerprint"],
        "release": row["release"], "requested_by": row["requested_by"], "engine_version": row["engine_version"],
        "created_at": str(row["created_at"]), "valid_from": str(row["valid_from"]) if row["valid_from"] else None,
        "valid_to": str(row["valid_to"]) if row["valid_to"] else None, "version": row["version"],
        "why_trail_id": row["why_trail_id"], "review": _json(row["review"], []), "profile": _json(row["profile"], {}),
        "composition": comp, "stored_items": len(items), "proof_rows": proof,
    }


def list_blueprints(conn: Connection, *, profile_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    q = sa.select(blueprints).where(blueprints.c.stable_id.isnot(None)).order_by(blueprints.c.id.desc()).limit(limit)
    if profile_id:
        q = q.where(blueprints.c.profile_id == profile_id)
    if status:
        q = q.where(blueprints.c.status == status)
    out = []
    for r in conn.execute(q).mappings():
        res = _json(r["result"], {}) or {}
        out.append({
            "blueprint_id": r["stable_id"], "status": r["status"], "profile_id": r["profile_id"], "release": r["release"],
            "requested_by": r["requested_by"], "engine_version": r["engine_version"], "created_at": str(r["created_at"]),
            "coverage_summary": res.get("coverage_summary"), "blocks": res.get("blocks", []), "minimal": res.get("minimal"),
            "fingerprint": r["fingerprint"],
        })
    return out


def history(conn: Connection, blueprint_id: str) -> list[dict]:
    """Every blueprint composed for the same profile fingerprint, oldest first."""
    row = conn.execute(sa.select(blueprints.c.fingerprint).where(blueprints.c.stable_id == blueprint_id)).first()
    if row is None:
        return []
    rows = conn.execute(
        sa.select(blueprints).where(blueprints.c.fingerprint == row.fingerprint).order_by(blueprints.c.id)).mappings()
    return [{"blueprint_id": r["stable_id"], "status": r["status"], "release": r["release"], "created_at": str(r["created_at"]),
             "valid_from": str(r["valid_from"]) if r["valid_from"] else None, "valid_to": str(r["valid_to"]) if r["valid_to"] else None,
             "review": _json(r["review"], [])} for r in rows]
