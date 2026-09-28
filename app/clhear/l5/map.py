# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L5 activity mappers and junction builder (HLD v2 §4.5).

Two sides, three edges, closed-world on every end:

* every live L2 obligation is mapped to a compliance activity — by a cue read
  from its duty text (screen, monitor, investigate, report, notify, train,
  attest, assess, record, control, test), else to the activity that operates
  the L3 block the obligation requires ("Operate <block>"), so nothing is
  left unmapped; the ``when`` condition of the trigger is the obligation's L4
  applicability predicate, so it may only name schema attributes;
* ``implies``   — L4 product / service -> business activity (curated table;
  ``"*"`` = every live product);
* ``operates``  — compliance activity -> L3 block, derived through the
  obligations the activity triggers and their live ``requires`` edges (plus
  the curated anchors);
* ``mitigates`` — compliance activity -> business activity, lit by the
  obligations both sides share (curated anchors, shared trigger anchors, and
  L4 product predicates through ``implies``).

Edges are re-versioned in place when their obligation refs change and
invalidated when a build no longer produces them (I2); one L5 why-trail per
build (I3); ``clhear.l5.changed`` is published when anything moved. The
optional router step (``l5.activity_map``) only re-homes obligations that
were mapped by the block fallback, choosing among *existing* activities or
proposing one with a side and an action type from the published vocabulary,
and must quote the text it read.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import applies_to, attribute_schema, blocks, implies, mitigates, obligations, operates, products_services, requires
from app.clhear.l5.models import ACTION_TYPES, SIDES
from app.clhear.platform import record
from app.clhear.platform.events import publish_layer_event
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.ids import next_id
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l5.map")

AGENT = "l5.map"
METHOD = "deterministic-v1"
LIVE = ("derived", "validated")
MAX_MAP = 10

# duty-text cue -> (action type, curated compliance activity). Order matters: the
# first cue that matches the duty sentence wins; the block fallback catches the rest.
_COMPLIANCE_CUES: list[tuple[str, str, str]] = [
    (r"\b(?:designate|appoint)(?:s|ed)?\b[^.]{0,60}\b(?:officer|MLRO|DPO|function)\b|\bdata protection officer\b|\bnominated officer\b|\bcompliance officer\b",
     "attest", "ACT-ATTEST-GOVERNANCE"),
    (r"\benhanced (?:customer )?due diligence\b|\bpolitically exposed\b|\bhigh[- ]risk third countr|\bhigh[- ]risk (?:customer|relationship|business)",
     "investigate", "ACT-HIGH-RISK-RELATIONSHIPS"),
    (r"\bcustomer due diligence\b|\bidentif(?:y|ies|ication of)\b[^.]{0,80}\b(?:customer|client|beneficial owner)|\bverif(?:y|ies|ication)\b|\bbeneficial owners?\b|\bsanctions? (?:list|screening)|\bscreen(?:s|ing|ed)?\b",
     "screen", "ACT-SCREEN-CUSTOMERS"),
    (r"\bongoing monitoring\b|\bmonitor(?:s|ing|ed)?\b|\bscrutini[sz]", "monitor", "ACT-MONITOR-TRANSACTIONS"),
    (r"\btrain(?:s|ing|ed)?\b|\bmade aware\b|\bawareness\b", "train", "ACT-TRAIN-STAFF"),
    (r"\bnotif(?:y|ies|ication)\b|\bwithout undue delay\b|\bbreach(?:es)?\b|\bincidents?\b", "notify", "ACT-RESPOND-TO-INCIDENTS"),
    (r"\bsenior management\b|\bmanagement body\b|\bboard\b|\bapprov(?:e|es|al|ed)\b|\bappoint(?:s|ed|ment)?\b|\bnominated officer\b|\bcompliance officer\b|\bdata protection officer\b|\bsign(?:s|ed)? off\b",
     "attest", "ACT-ATTEST-GOVERNANCE"),
    (r"\brisk assessment\b|\bassess(?:es|ment)?\b[^.]{0,40}\brisks?\b|\bimpact assessment\b|\bassessment of the impact\b|\bperiodic(?:al)? review\b|\breview(?:s|ed)? (?:at least|annually|periodically|regularly)\b",
     "assess", "ACT-RUN-RISK-ASSESSMENT"),
    (r"\bkeep(?:s|ing)? (?:a |the )?records?\b|\brecord(?:s|ed|ing)?\b|\bretain(?:s|ed)?\b|\bretention\b|\bregister of\b", "record", "ACT-KEEP-RECORDS"),
    (r"\btest(?:s|ing|ed)?\b|\bresilience\b|\bbusiness continuity\b|\brecovery\b|\bthird[- ]party (?:risk|arrangements?|register)\b|\bICT\b",
     "test", "ACT-TEST-ICT-RESILIENCE"),
    (r"\bsegregat(?:e|es|ed|ion)\b[^.]{0,60}\bcrypto|\bcrypto[- ]assets?\b[^.]{0,80}\b(?:safeguard|segregat|custod|safekeep)",
     "control", "ACT-SAFEGUARD-CRYPTO"),
    (r"\bsecurity of (?:the )?processing\b|\btechnical and organisational measures\b|\bencrypt|\baccess controls?\b|\bpseudonymis", "control", "ACT-SECURE-SYSTEMS"),
    (r"\breport(?:s|ing|ed)?\b[^.]{0,60}\b(?:to the|to a|to any|with)\b[^.]{0,40}\b(?:authority|authorities|regulator|commission|FCA|SEC|FinCEN|competent)\b|\bsuspicious\b|\bdisclos(?:e|es|ure)\b[^.]{0,60}\b(?:authority|regulator|officer)",
     "report", "ACT-REPORT-TO-AUTHORITIES"),
]

_LEAD = re.compile(r"^(?:where|when|if|unless|subject to|without prejudice to)\b[^,]*,\s*", re.I)


# ----------------------------------------------------------------- helpers


def _why(subject_ref: str, summary: str, evidence: list[str], *, method: str = METHOD,
         confidence: float | None = 1.0, manifest: dict | None = None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L5", reasoning_summary=summary, evidence_refs=evidence, inputs=(subject_ref, method, *evidence),
        model_manifest=manifest or {"model": "deterministic", "method": method}, skill_version=AGENT,
        confidence=confidence, agent_id=AGENT, subject_ref=subject_ref, input_layers=("L2", "L3", "L4"),
    )


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value


def _rows(conn: Connection, table: sa.Table, *where) -> list[dict]:
    q = sa.select(table)
    for w in where:
        q = q.where(w)
    return [dict(r) for r in conn.execute(q).mappings()]


def live_activities(conn: Connection) -> list[dict]:
    rows = _rows(conn, activities_t, activities_t.c.valid_to.is_(None))
    for r in rows:
        r["triggers"] = _json(r.get("triggers"), [])
    return rows


def _live_obligations(conn: Connection, source_key: str | None = None) -> list[dict]:
    from app.clhear.l1.scopes import limiting

    q = sa.select(obligations).where(obligations.c.status.in_(LIVE))
    limit = limiting(obligations.c.source_key, source_key)
    if limit is not None:
        q = q.where(limit)
    return [dict(r) for r in conn.execute(q.order_by(obligations.c.stable_id, obligations.c.id)).mappings()]


def _live_edges(conn: Connection, table: sa.Table) -> list[dict]:
    rows = _rows(conn, table, table.c.valid_to.is_(None))
    for r in rows:
        if "obligation_refs" in r:
            r["obligation_refs"] = _json(r.get("obligation_refs"), [])
    return rows


def _live_products(conn: Connection) -> dict[str, dict]:
    try:
        return {r["id"]: r for r in _rows(conn, products_services, products_services.c.valid_to.is_(None))}
    except sa.exc.OperationalError:  # pre-m0012 store
        return {}


def _live_blocks(conn: Connection) -> dict[str, dict]:
    return {r["id"]: r for r in _rows(conn, blocks)}


def _canonical(blocks_by_id: dict[str, dict], block_id: str) -> str:
    seen = set()
    while block_id in blocks_by_id and blocks_by_id[block_id].get("canonical_id") and block_id not in seen:
        seen.add(block_id)
        block_id = blocks_by_id[block_id]["canonical_id"]
    return block_id


def _requires_by_obligation(conn: Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    try:
        rows = conn.execute(sa.select(requires.c.obligation_id, requires.c.block_id).where(requires.c.valid_to.is_(None))).all()
    except sa.exc.OperationalError:  # pre-m0011 store
        return out
    for oid, bid in rows:
        out.setdefault(oid, []).append(bid)
    return out


def _predicates_by_obligation(conn: Connection) -> dict[str, dict]:
    """obligation id -> merged live L4 applicability predicate."""
    out: dict[str, dict] = {}
    try:
        rows = conn.execute(sa.select(applies_to.c.obligation_id, applies_to.c.predicate).where(applies_to.c.valid_to.is_(None))).all()
    except sa.exc.OperationalError:  # pre-m0012 store
        return out
    for oid, pred in rows:
        out.setdefault(oid, {}).update(_json(pred, {}) or {})
    return out


def _anchor_index(obs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(o["source_key"], o["clause_ref"]): o for o in obs}


def resolve_anchor(anchor: dict, index: dict[tuple[str, str], dict]) -> list[dict]:
    """Anchor {source_key, refs[]} -> live obligations at those refs (may be empty)."""
    if not isinstance(anchor, dict) or not anchor.get("source_key"):
        return []
    refs = anchor.get("refs") or []
    if refs:
        return [index[(anchor["source_key"], r)] for r in refs if (anchor["source_key"], r) in index]
    return [o for (sk, _), o in index.items() if sk == anchor["source_key"]]


def _ref(ob: dict) -> str:
    return ob.get("stable_id") or ob["id"]


def duty_text(ob: dict) -> str:
    text = ob.get("determination") or ob.get("statement") or ob.get("title") or ""
    return _LEAD.sub("", text.strip())


# ----------------------------------------------------------------- deterministic mapper


def classify(ob: dict) -> tuple[str, str, str] | None:
    """Duty text -> (action_type, curated activity id, cue) or None."""
    text = duty_text(ob)
    for pattern, action, act_id in _COMPLIANCE_CUES:
        m = re.search(pattern, text, re.I)
        if m:
            return action, act_id, m.group(0)
    return None


def _trigger_for(ob: dict, predicates: dict[str, dict], *, method: str, cue: str) -> dict:
    when = dict(predicates.get(ob["id"]) or {})
    if not when and ob.get("jurisdiction") and ob["jurisdiction"].upper() not in ("", "XX", "UNKNOWN"):
        when = {"jurisdictions": ob["jurisdiction"].upper()}
    return {"anchor": {"source_key": ob["source_key"], "refs": [ob["clause_ref"]]}, "when": when,
            "method": method, "cue": cue[:120], "obligation": _ref(ob)}


def _covered(acts: list[dict]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for a in acts:
        for t in a["triggers"]:
            anc = t.get("anchor") or {}
            for ref in anc.get("refs") or []:
                out.setdefault((anc.get("source_key"), ref), []).append(a["id"])
    return out


def _append_trigger(conn: Connection, act: dict, trigger: dict, why: str) -> bool:
    triggers = list(act["triggers"])
    if any(t.get("anchor") == trigger["anchor"] and (t.get("when") or {}) == trigger["when"] for t in triggers):
        return False
    triggers.append(trigger)
    act["triggers"] = triggers
    conn.execute(activities_t.update().where(activities_t.c.id == act["id"]).values(
        triggers=triggers, why_trail_id=why, updated_at=datetime.now(timezone.utc)))
    return True


def find_or_create_activity(conn: Connection, acts: list[dict], *, side: str, action_type: str, name: str,
                            description: str, owner: str, why: record.WhyTrail | str, status: str = "derived") -> dict:
    if side not in SIDES:
        raise ValueError(f"unknown side {side!r}")
    if action_type not in ACTION_TYPES[side]:
        raise ValueError(f"unknown action type {action_type!r} for side {side}")
    folded = name.strip().lower()
    for a in acts:
        if a["side"] == side and a["name"].strip().lower() == folded:
            return a
    aid = next_id(conn, "ACT")
    row = {"id": aid, "name": name[:160], "description": description[:400], "business_owner": owner[:80],
           "triggers": [], "status": status, "side": side, "action_type": action_type, "canonical_id": None}
    record.write(conn, activities_t, row, why=why, valid_from=datetime.now(timezone.utc).date())
    row["triggers"] = []
    acts.append(row)
    return row


def map_deterministic(conn: Connection, *, source_key: str | None = None, limit: int | None = None) -> dict:
    """Every live obligation without a trigger gets one on a compliance activity."""
    acts = live_activities(conn)
    by_id = {a["id"]: a for a in acts}
    covered = _covered(acts)
    obs = [o for o in _live_obligations(conn, source_key) if (o["source_key"], o["clause_ref"]) not in covered]
    if limit is not None:
        obs = obs[:limit]
    if not obs:
        return {"mapped": 0, "by_cue": 0, "by_block": 0, "unmapped": 0, "activities_created": 0}
    predicates = _predicates_by_obligation(conn)
    req = _requires_by_obligation(conn)
    blocks_by_id = _live_blocks(conn)
    created = by_cue = by_block = unmapped = 0
    trail = _why("l5.map", f"deterministic activity mapping of {len(obs)} obligations", [_ref(o) for o in obs[:50]]).write(conn)
    for ob in obs:
        hit = classify(ob)
        if hit is not None and hit[1] in by_id:
            action, act_id, cue = hit
            if _append_trigger(conn, by_id[act_id], _trigger_for(ob, predicates, method="cue", cue=cue), trail):
                by_cue += 1
            continue
        block_ids = [_canonical(blocks_by_id, b) for b in req.get(ob["id"], [])]
        block = next((blocks_by_id[b] for b in block_ids if b in blocks_by_id), None)
        if block is None:
            unmapped += 1
            continue
        before = len(acts)
        act = find_or_create_activity(
            conn, acts, side="compliance", action_type="control", name=f"Operate {block['name']}",
            description=f"Run and maintain the {block['kind'].lower()} '{block['name']}' the obligations below require.",
            owner="Compliance", why=trail)
        created += len(acts) - before
        by_id[act["id"]] = act
        if _append_trigger(conn, act, _trigger_for(ob, predicates, method="block", cue=f"requires {block['id']}"), trail):
            by_block += 1
    return {"mapped": by_cue + by_block, "by_cue": by_cue, "by_block": by_block, "unmapped": unmapped,
            "activities_created": created}


# ----------------------------------------------------------------- junction builder


def _curated() -> list[dict]:
    from app.clhear import curated

    return curated.load("l5_activities")


def _upsert_edge(conn: Connection, table: sa.Table, key_cols: tuple[str, ...], values: dict, live: dict, trail: str,
                 prefix: str, *, today, preserve_existing: bool = False) -> str:
    key = tuple(values[c] for c in key_cols)
    existing = live.get(key) if preserve_existing else live.pop(key, None)
    if existing is None:
        rid = next_id(conn, prefix)
        record.write(conn, table, {"id": rid, **values}, why=trail, valid_from=today)
        return "added"
    if preserve_existing or all(existing.get(k) == v for k, v in values.items()):
        return "unchanged"
    review = list(_json(existing.get("review"), []) or []) + [
        {"event": "re-derived", "at": datetime.now(timezone.utc).isoformat(), "why_trail_id": trail}]
    conn.execute(table.update().where(table.c.id == existing["id"]).values(
        **{k: v for k, v in values.items()}, version=(existing.get("version") or 1) + 1, review=review, why_trail_id=trail))
    return "changed"


def build_junction(engine: Engine, *, publish: bool = True) -> dict:
    with engine.begin() as conn:
        return build_junction_in(conn, publish=publish)


def build_junction_in(conn: Connection, *, publish: bool = True) -> dict:
    """Derive implies / operates / mitigates from the curated table and the
    live L2–L4 state. Idempotent: unchanged edges untouched, changed edges
    re-versioned, vanished edges invalidated.

    While a source scope is active, edges already in the database are left
    as they are. The build only adds edges for the scope; it does not
    rewrite or close edges that belong to the rest of the corpus.
    """
    from app.clhear.l1.scopes import keys as scope_keys

    preserve = scope_keys() is not None
    today = datetime.now(timezone.utc).date()
    curated_rows = {c["id"]: c for c in _curated()}
    acts = live_activities(conn)
    by_id = {a["id"]: a for a in acts}
    obs = _live_obligations(conn)
    index = _anchor_index(obs)
    products = _live_products(conn)
    blocks_by_id = _live_blocks(conn)
    req = _requires_by_obligation(conn)
    predicates = _predicates_by_obligation(conn)
    product_by_name = {p["name"].lower(): pid for pid, p in products.items()}
    for pid, p in products.items():
        for alias in p.get("aliases") or []:
            product_by_name.setdefault(alias.lower(), pid)

    trail = _why("l5.junction", "L5 junction build: implies from curated products, operates via triggered obligations' "
                 "requires edges, mitigates via shared obligations and L4 product predicates",
                 [f"activities:{len(acts)}", f"obligations:{len(obs)}", f"products:{len(products)}"]).write(conn)
    counts = {"added": 0, "unchanged": 0, "changed": 0, "invalidated": 0}

    def tally(outcome: str) -> None:
        counts[outcome] += 1

    # --- implies: product -> business activity
    live_imp = {(e["product_id"], e["activity_id"]): e for e in _live_edges(conn, implies)}
    for a in acts:
        if a["side"] != "business":
            continue
        spec = (curated_rows.get(a["id"]) or {}).get("implied_by")
        if spec is None:
            continue
        pids = sorted(products) if spec == "*" else [p for p in spec if p in products]
        for pid in pids:
            rationale = ("every product implies this activity" if spec == "*"
                         else f"{products[pid]['name']} implies {a['name'].lower()}")
            tally(_upsert_edge(conn, implies, ("product_id", "activity_id"),
                               {"product_id": pid, "activity_id": a["id"], "rationale": rationale, "method": "curated"},
                               live_imp, trail, "IMP", today=today, preserve_existing=preserve))
    if not preserve:
        for e in live_imp.values():
            if e.get("method") != "llm":
                record.invalidate(conn, implies, implies.c.id == e["id"], why=trail, reason="no longer produced by the junction build")
                tally("invalidated")

    # --- triggered obligations per activity (resolved through the live registry)
    triggered: dict[str, dict[str, dict]] = {}
    for a in acts:
        slot = triggered.setdefault(a["id"], {})
        for t in a["triggers"]:
            for ob in resolve_anchor(t.get("anchor") or {}, index):
                slot[ob["id"]] = ob

    # --- operates: compliance activity -> block
    live_opr = {(e["activity_id"], e["block_id"]): e for e in _live_edges(conn, operates)}
    for a in acts:
        if a["side"] != "compliance":
            continue
        per_block: dict[str, list[str]] = {}
        for bid in (curated_rows.get(a["id"]) or {}).get("operates") or []:
            if bid in blocks_by_id:
                per_block.setdefault(_canonical(blocks_by_id, bid), [])
        for oid, ob in triggered[a["id"]].items():
            for bid in req.get(oid, []):
                per_block.setdefault(_canonical(blocks_by_id, bid), []).append(_ref(ob))
        for bid, refs in per_block.items():
            refs = sorted(set(refs))
            method = "curated" if bid in ((curated_rows.get(a["id"]) or {}).get("operates") or []) else "deterministic"
            rationale = (f"{a['name']} operates {blocks_by_id[bid]['name']}" +
                         (f" for {len(refs)} obligation(s) it implements" if refs else " (curated anchor)"))
            tally(_upsert_edge(conn, operates, ("activity_id", "block_id"),
                               {"activity_id": a["id"], "block_id": bid, "obligation_refs": refs, "rationale": rationale,
                                "method": method}, live_opr, trail, "OPR", today=today, preserve_existing=preserve))
    if not preserve:
        for e in live_opr.values():
            if e.get("method") != "llm":
                record.invalidate(conn, operates, operates.c.id == e["id"], why=trail, reason="no longer produced by the junction build")
                tally("invalidated")

    # --- mitigates: compliance activity -> business activity, lit by shared obligations
    business = [a for a in acts if a["side"] == "business"]
    compliance = [a for a in acts if a["side"] == "compliance"]
    implied_by_product: dict[str, list[str]] = {}
    for e in _live_edges(conn, implies):
        implied_by_product.setdefault(e["product_id"], []).append(e["activity_id"])
    pairs: dict[tuple[str, str], dict] = {}

    def light(c_id: str, b_id: str, refs: list[str], basis: str) -> None:
        slot = pairs.setdefault((c_id, b_id), {"refs": set(), "basis": set()})
        slot["refs"].update(refs)
        slot["basis"].add(basis)

    for c in compliance:
        curated_targets: list[str] = []
        for m in (curated_rows.get(c["id"]) or {}).get("mitigates") or []:
            if m.get("business") in by_id and by_id[m["business"]]["side"] == "business":
                light(c["id"], m["business"], [_ref(o) for o in resolve_anchor(m.get("anchor") or {}, index)], "curated")
                if m["business"] not in curated_targets:
                    curated_targets.append(m["business"])
        c_obs = triggered[c["id"]]
        if not c_obs:
            continue
        # every obligation the compliance activity implements lights the pairs it is reviewed to govern
        for b_id in curated_targets:
            light(c["id"], b_id, [_ref(o) for o in c_obs.values()], "implements")
        for b in business:
            shared = set(c_obs) & set(triggered[b["id"]])
            if shared:
                light(c["id"], b["id"], [_ref(c_obs[o]) for o in shared], "shared-anchor")
        for oid, ob in c_obs.items():
            pred = predicates.get(oid) or {}
            names = pred.get("products")
            if not names:
                continue
            for name in ([names] if isinstance(names, str) else names):
                pid = product_by_name.get(str(name).lower())
                for b_id in implied_by_product.get(pid, []) if pid else []:
                    light(c["id"], b_id, [_ref(ob)], "l4-product")
    live_mit = {(e["compliance_activity_id"], e["business_activity_id"]): e for e in _live_edges(conn, mitigates)}
    for (c_id, b_id), slot in sorted(pairs.items()):
        refs = sorted(slot["refs"])
        basis = ", ".join(sorted(slot["basis"]))
        rationale = f"{by_id[c_id]['name']} governs {by_id[b_id]['name'].lower()} ({basis}; {len(refs)} obligation(s))"
        tally(_upsert_edge(conn, mitigates, ("compliance_activity_id", "business_activity_id"),
                           {"compliance_activity_id": c_id, "business_activity_id": b_id, "obligation_refs": refs,
                            "rationale": rationale, "method": "curated" if "curated" in slot["basis"] else "deterministic"},
                           live_mit, trail, "MIT", today=today, preserve_existing=preserve))
    if not preserve:
        for e in live_mit.values():
            if e.get("method") != "llm":
                record.invalidate(conn, mitigates, mitigates.c.id == e["id"], why=trail, reason="no longer produced by the junction build")
                tally("invalidated")

    changed = counts["added"] + counts["changed"] + counts["invalidated"]
    if changed and publish:
        publish_layer_event(conn, layer="L5", event="changed", subject_ref="l5.junction",
                            payload={"counts": counts, "why_trail_id": trail}, producer=AGENT)
    return {**counts, "changed_total": changed, "why_trail_id": trail, "activities": len(acts)}


# ----------------------------------------------------------------- LLM refinement (closed-world)


def _llm_prompt(ob: dict, acts: list[dict]) -> str:
    menu = "\n".join(f"- {a['id']} [{a['side']}/{a['action_type']}] {a['name']}" for a in acts if a["status"] != "invalidated")
    vocab = json.dumps({s: sorted(t) for s, t in ACTION_TYPES.items()})
    return (
        "Choose the ONE activity this obligation is about. Prefer an existing activity id from the menu. "
        "Only if none fits, propose a new one with side and action_type taken from the vocabulary. "
        "Quote the words of the obligation text you based the choice on (verbatim substring).\n"
        f"Vocabulary: {vocab}\n"
        f"Menu:\n{menu}\n\n"
        'JSON: {"activity_id": "" | null, "new": {"side": "", "action_type": "", "name": ""} | null, "quote": ""}\n\n'
        f"{_ref(ob)} [{ob.get('jurisdiction')}] {ob.get('title')}\n{(ob.get('statement') or '')[:600]}"
    )


def llm_refine(engine: Engine, llm, ob: dict) -> dict:
    """Re-home one block-mapped obligation on the activity a model picks from
    the closed menu. Unquoted or out-of-vocabulary answers are discarded."""
    with engine.connect() as conn:
        acts = live_activities(conn)
    try:
        result = complete(llm, "l5.activity_map", prompt=_llm_prompt(ob, acts),
                          system="Closed-world mapper. Only ids from the menu or terms from the vocabulary. JSON only.",
                          required_keys=["quote"], max_tokens=300)
        parsed = parse_json_object(result.text)
    except Exception:
        log.exception("L5 llm refine failed for %s", ob["id"])
        return {"obligation": _ref(ob), "outcome": "error"}
    quote = str(parsed.get("quote") or "").strip()
    text = (ob.get("statement") or "") + " " + (ob.get("determination") or "")
    if not quote or quote.lower() not in text.lower():
        return {"obligation": _ref(ob), "outcome": "discarded", "reason": "quote not in obligation text"}
    by_id = {a["id"]: a for a in acts}
    manifest = {"model": getattr(result, "model", ""), "task": "l5.activity_map"}
    conf = getattr(result, "confidence", None) or 0.8
    with engine.begin() as conn:
        predicates = _predicates_by_obligation(conn)
        trail = _why(_ref(ob), f"l5.activity_map re-homed {_ref(ob)}: '{quote[:80]}'", [_ref(ob)], method="llm",
                     confidence=conf, manifest=manifest).write(conn)
        target = by_id.get(str(parsed.get("activity_id") or ""))
        if target is None and isinstance(parsed.get("new"), dict):
            new = parsed["new"]
            side, action, name = str(new.get("side") or ""), str(new.get("action_type") or ""), str(new.get("name") or "").strip()
            if side in SIDES and action in ACTION_TYPES[side] and name:
                target = find_or_create_activity(conn, acts, side=side, action_type=action, name=name,
                                                 description=f"Proposed by l5.activity_map from: '{quote[:120]}'",
                                                 owner="Compliance", why=trail, status="ai_generated")
        if target is None:
            return {"obligation": _ref(ob), "outcome": "discarded", "reason": "no activity resolved"}
        target["triggers"] = _json(conn.execute(sa.select(activities_t.c.triggers).where(activities_t.c.id == target["id"])).scalar(), [])
        _append_trigger(conn, target, _trigger_for(ob, predicates, method="llm", cue=quote), trail)
        # the block-fallback trigger stays on its activity (I2) but is marked superseded
        for a in acts:
            if a["id"] == target["id"]:
                continue
            changed = False
            for t in a["triggers"]:
                if t.get("method") == "block" and t.get("obligation") == _ref(ob) and not t.get("superseded_by"):
                    t["superseded_by"] = target["id"]
                    changed = True
            if changed:
                conn.execute(activities_t.update().where(activities_t.c.id == a["id"]).values(triggers=a["triggers"], why_trail_id=trail))
    return {"obligation": _ref(ob), "outcome": "mapped", "activity": target["id"]}


def map_activities(engine: Engine, llm=None, limit: int = MAX_MAP, *, source_key: str | None = None) -> dict:
    """Nightly mapper: deterministic pass over every unmapped obligation, then
    the router step for up to ``limit`` block-fallback mappings, then the
    junction build."""
    with engine.begin() as conn:
        det = map_deterministic(conn, source_key=source_key)
    refined: list[dict] = []
    if llm is not None and limit:
        with engine.connect() as conn:
            acts = live_activities(conn)
            index = _anchor_index(_live_obligations(conn, source_key))
        fallback = []
        for a in acts:
            for t in a["triggers"]:
                if t.get("method") == "block" and not t.get("superseded_by"):
                    fallback.extend(resolve_anchor(t["anchor"], index))
        for ob in fallback[:limit]:
            refined.append(llm_refine(engine, llm, ob))
    junction = build_junction(engine)
    written = det["mapped"] + sum(1 for r in refined if r["outcome"] == "mapped")
    out = {"written": written, "deterministic": det, "llm": refined, "junction": junction,
           "rejected": sum(1 for r in refined if r["outcome"] in ("discarded", "error"))}
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L5", fleet="l5.map",
            reasoning=f"Cartographer: {det['mapped']} obligations mapped deterministically ({det['by_cue']} by cue, "
                      f"{det['by_block']} via their block), {len(refined)} router refinements; junction "
                      f"+{junction['added']} ~{junction['changed']} -{junction['invalidated']} edges",
            detail=out,
        )
    except Exception:
        log.exception("L5 ai_ops failed")
    return out


# ----------------------------------------------------------------- propagation (I1)


def on_l2_changed(engine: Engine, payload: dict) -> dict:
    """An L2 change: revoked -> the obligation leaves every edge it lit (edges
    with nothing left are invalidated); added -> map the source; updated ->
    the anchor still holds, the junction is rebuilt."""
    oid = payload.get("derivation_key") or payload.get("obligation_id")
    change = payload.get("change") or payload.get("kind")
    if not oid:
        return {"ignored": True, "reason": "no obligation in payload"}
    with engine.begin() as conn:
        ob = conn.execute(sa.select(obligations).where(sa.or_(obligations.c.id == oid, obligations.c.stable_id == oid))).mappings().first()
        if ob is None:
            return {"ignored": True, "reason": f"unknown obligation {oid}"}
        ob = dict(ob)
        ref = _ref(ob)
        out = {"obligation": ref, "change": change, "unlit": 0, "invalidated": 0}
        if change == "revoked" or ob["status"] not in LIVE:
            trail = _why(ref, f"L2 change '{change}' on {ref}: junction edges it lit are withdrawn", [ref]).write(conn)
            for table, col in ((mitigates, mitigates.c.obligation_refs), (operates, operates.c.obligation_refs)):
                for e in _live_edges(conn, table):
                    refs = e["obligation_refs"]
                    if ref not in refs and ob["id"] not in refs:
                        continue
                    remaining = [r for r in refs if r not in (ref, ob["id"])]
                    if remaining or e.get("method") == "curated":
                        conn.execute(table.update().where(table.c.id == e["id"]).values(
                            obligation_refs=remaining, version=(e.get("version") or 1) + 1, why_trail_id=trail))
                        out["unlit"] += 1
                    else:
                        record.invalidate(conn, table, table.c.id == e["id"], why=trail, reason=f"obligation {change}")
                        out["invalidated"] += 1
            return out
        if change == "added":
            out["mapped"] = map_deterministic(conn, source_key=ob["source_key"])
    out["junction"] = build_junction(engine)
    return out


def on_l4_changed(engine: Engine, payload: dict | None = None) -> dict:
    """An ontology change re-derives implies (products may have come or gone)."""
    return build_junction(engine)


# ----------------------------------------------------------------- reads


def activity_map(conn: Connection, attributes: dict, *, matched_obligations: set[str] | None = None) -> dict:
    """The activity map for one profile: business activities its products imply
    (plus any whose trigger matches the profile), the compliance activities
    that govern them, edges lit by obligation."""
    from app.clhear.l4.ontology import matches

    acts = live_activities(conn)
    by_id = {a["id"]: a for a in acts}
    products = _live_products(conn)
    lookup = {p["name"].lower(): pid for pid, p in products.items()}
    for pid, p in products.items():
        lookup[pid.lower()] = pid
        for alias in p.get("aliases") or []:
            lookup.setdefault(alias.lower(), pid)
    wanted = attributes.get("products") or []
    wanted = [wanted] if isinstance(wanted, str) else list(wanted)
    my_products = [lookup[str(w).lower()] for w in wanted if str(w).lower() in lookup]
    index = _anchor_index(_live_obligations(conn))

    business: dict[str, dict] = {}
    for e in _live_edges(conn, implies):
        if e["product_id"] in my_products and e["activity_id"] in by_id:
            slot = business.setdefault(e["activity_id"], {**_public(by_id[e["activity_id"]]), "implied_by": [], "obligations": []})
            slot["implied_by"].append({"product_id": e["product_id"], "product": products.get(e["product_id"], {}).get("name", e["product_id"]),
                                       "edge_id": e["id"], "rationale": e["rationale"]})
    def lit(refs: list[str]) -> list[str]:
        return [r for r in refs if matched_obligations is None or r in matched_obligations]

    for a in acts:
        if a["side"] != "business":
            continue
        hits = [t for t in a["triggers"] if matches(t.get("when") or {}, attributes)]
        refs = sorted({_ref(o) for t in hits for o in resolve_anchor(t.get("anchor") or {}, index)})
        if not refs:
            continue
        slot = business.setdefault(a["id"], {**_public(a), "implied_by": [], "obligations": []})
        slot["obligations"] = lit(refs)
    compliance: dict[str, dict] = {}
    edges: list[dict] = []
    for e in _live_edges(conn, mitigates):
        if e["business_activity_id"] not in business or e["compliance_activity_id"] not in by_id:
            continue
        c = by_id[e["compliance_activity_id"]]
        refs = lit(e["obligation_refs"])
        slot = compliance.setdefault(c["id"], {**_public(c), "governs": [], "obligations": []})
        slot["governs"].append(e["business_activity_id"])
        slot["obligations"] = sorted(set(slot["obligations"]) | set(refs))
        edges.append({"id": e["id"], "compliance_activity_id": c["id"], "business_activity_id": e["business_activity_id"],
                      "obligation_refs": refs, "rationale": e["rationale"], "method": e["method"], "lit": bool(refs)})
    opr = {}
    for e in _live_edges(conn, operates):
        if e["activity_id"] in compliance:
            opr.setdefault(e["activity_id"], []).append({"block_id": e["block_id"], "obligation_refs": e["obligation_refs"], "edge_id": e["id"]})
    for cid, slot in compliance.items():
        slot["operates"] = opr.get(cid, [])
    all_refs = sorted({r for e in edges for r in e["obligation_refs"]} | {r for b in business.values() for r in b["obligations"]})
    return {"business": sorted(business.values(), key=lambda a: a["name"]),
            "compliance": sorted(compliance.values(), key=lambda a: a["name"]),
            "edges": edges, "products": my_products, "obligations": all_refs,
            "counts": {"business": len(business), "compliance": len(compliance), "edges": len(edges),
                       "lit_edges": sum(1 for e in edges if e["lit"]), "obligations": len(all_refs)}}


def _public(a: dict) -> dict:
    return {"id": a["id"], "name": a["name"], "side": a["side"], "action_type": a["action_type"],
            "description": a.get("description", ""), "business_owner": a.get("business_owner", ""), "status": a.get("status")}


def coverage(engine: Engine) -> dict:
    """Obligation -> activity coverage (for the scorecard)."""
    with engine.connect() as conn:
        acts = live_activities(conn)
        obs = _live_obligations(conn)
    covered = _covered(acts)
    mapped = [o for o in obs if (o["source_key"], o["clause_ref"]) in covered]
    by_method: dict[str, int] = {}
    for a in acts:
        for t in a["triggers"]:
            by_method[t.get("method") or "curated"] = by_method.get(t.get("method") or "curated", 0) + 1
    return {"obligations": len(obs), "mapped": len(mapped), "unmapped": len(obs) - len(mapped),
            "ratio": (len(mapped) / len(obs)) if obs else None, "triggers_by_method": by_method,
            "activities": {"business": sum(1 for a in acts if a["side"] == "business"),
                           "compliance": sum(1 for a in acts if a["side"] == "compliance")}}
