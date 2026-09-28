# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L5 activities: who does what, in the duty's own words.

For every live obligation that L3 gave a measure, the activity is the duty's
action as quoted from the clause ("review user access rights") and its
operator is the addressee the clause names ("the management body"). An
activity ``operates`` the measures its duties require.

Nothing is filled in: a duty addressed to everyone ("every organisation") or
written in the passive does not say who carries it out, so its activity has no
operator and an evidence gap says which source would assign one. No activity
vocabulary, owner or business-activity catalogue is used; business activities
and the ``implies`` / ``mitigates`` edges are built only when texts in scope
link them, which the current readers do not.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear import evidence
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks, obligations, operates, products_services, requires
from app.clhear.l2.registry import PRONOUNS, binds_subject, field_quotes, is_universal, obligation_clauses
from app.clhear.l3.decompose import measure_phrase
from app.clhear.platform import record
from app.clhear.platform.events import publish_layer_event
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l5.map")

AGENT = "l5.map"
METHOD = "grounded-v1"
LIVE = ("derived", "validated")


# ----------------------------------------------------------------- helpers


def _why(subject_ref: str, summary: str, evidence_refs: list, *, confidence: float | None = 1.0) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L5", reasoning_summary=summary, evidence_refs=evidence_refs,
        inputs=(subject_ref, METHOD, *[json.dumps(e, sort_keys=True) for e in evidence_refs]),
        model_manifest={"model": "deterministic", "method": METHOD}, skill_version=AGENT,
        confidence=confidence, agent_id=AGENT, subject_ref=subject_ref, input_layers=("L2", "L3"),
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

    from app.clhear.l4.predicates import canonical_in

    q = sa.select(obligations).where(obligations.c.status.in_(LIVE))
    limit = limiting(obligations.c.source_key, source_key)
    if limit is not None:
        q = q.where(limit)
    return canonical_in([dict(r) for r in conn.execute(q.order_by(obligations.c.stable_id, obligations.c.id)).mappings()])


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
    for oid, bid in conn.execute(sa.select(requires.c.obligation_id, requires.c.block_id).where(requires.c.valid_to.is_(None))):
        out.setdefault(oid, []).append(bid)
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


# ----------------------------------------------------------------- reading the duty


def read_activity(ob: dict, clauses: list[dict]) -> dict | None:
    """The activity a duty describes: its quoted action and, when the text names
    one, its quoted operator. None when the duty states no action."""
    action = ob.get("action") or ""
    subject = " ".join((ob.get("subject") or "").split())
    if action.lower().startswith(("be ", "been ")):
        # Passive: what must be done to the subject; who does it is not stated.
        phrase = measure_phrase(action.split(" ", 1)[1] if " " in action else "")
        parts = [p for p in (subject, phrase) if p]
        quotes = [q for p in parts for q in (field_quotes(p, clauses) or [])] if phrase else []
        if not phrase or len(quotes) < len(parts):
            return None
        name = f"{subject[0].upper()}{subject[1:]}: {phrase}" if subject else phrase[0].upper() + phrase[1:]
        return {"name": name[:160], "operator": "", "action_type": phrase.split()[0].lower(),
                "evidence": {"name": quotes, "operator": []}}
    if not action:
        return None
    phrase = measure_phrase(action)
    quotes = field_quotes(phrase, clauses) if phrase else None
    if not quotes:
        return None
    operator = "" if (not subject or subject.lower() in PRONOUNS or is_universal(subject)
                      or not binds_subject(ob)) else subject
    operator_quotes = (field_quotes(operator, clauses) or []) if operator else []
    if operator and not operator_quotes:
        operator = ""
    verb = phrase.split()[0].lower()
    return {"name": phrase[0].upper() + phrase[1:], "operator": operator, "action_type": verb,
            "evidence": {"name": quotes, "operator": operator_quotes}}


def _find_or_create(conn: Connection, acts: list[dict], reading: dict, why: str) -> dict:
    name, owner = reading["name"], reading["operator"]
    for a in acts:
        if a["name"].strip().lower() == name.lower() and (a.get("business_owner") or "").lower() == owner.lower():
            if _json(a.get("evidence"), {}) != reading["evidence"]:  # quote the duty as it now reads
                conn.execute(activities_t.update().where(activities_t.c.id == a["id"]).values(
                    evidence=reading["evidence"], why_trail_id=why))
                a["evidence"] = reading["evidence"]
            return a
    aid = next_id(conn, "ACT")
    row = {"id": aid, "name": name[:160], "description": "", "business_owner": owner[:80], "triggers": [],
           "status": "derived", "side": "compliance", "action_type": reading["action_type"][:40], "canonical_id": None,
           "evidence": reading["evidence"]}
    record.write(conn, activities_t, row, why=why, valid_from=datetime.now(timezone.utc).date())
    acts.append(row)
    return row


def _add_trigger(conn: Connection, act: dict, ob: dict, why: str) -> bool:
    trigger = {"anchor": {"source_key": ob["source_key"], "refs": [ob["clause_ref"]]}, "when": {},
               "method": METHOD, "obligation": _ref(ob)}
    triggers = list(act["triggers"])
    if any(t.get("anchor") == trigger["anchor"] for t in triggers):
        return False
    triggers.append(trigger)
    act["triggers"] = triggers
    conn.execute(activities_t.update().where(activities_t.c.id == act["id"]).values(
        triggers=triggers, why_trail_id=why, updated_at=datetime.now(timezone.utc)))
    return True


def _duty_quote(ob: dict | None) -> dict | None:
    found = (ob or {}).get("evidence")
    return found.get("duty") if isinstance(found, dict) else None


def _operate(conn: Connection, act: dict, block_id: str, ob: dict, live: dict, why: str, today,
             live_obs: dict[str, dict]) -> str:
    duty = _duty_quote(ob)
    existing = live.get((act["id"], block_id))
    if existing is not None:
        # Only duties still in force, each quoted as it now reads.
        refs = sorted({r for r in existing["obligation_refs"] if r in live_obs} | {_ref(ob)})
        wanted = {"duty": [q for q in (_duty_quote(live_obs.get(r)) for r in refs) if q],
                  "activity": act.get("evidence") or {}}
        if refs == existing["obligation_refs"] and _json(existing.get("evidence"), {}) == wanted:
            return "unchanged"
        conn.execute(operates.update().where(operates.c.id == existing["id"]).values(
            obligation_refs=refs, evidence=wanted, version=(existing.get("version") or 1) + 1, why_trail_id=why))
        existing["obligation_refs"], existing["evidence"] = refs, wanted
        return "changed"
    rid = next_id(conn, "OPR")
    values = {"id": rid, "activity_id": act["id"], "block_id": block_id, "obligation_refs": [_ref(ob)],
              "rationale": (duty or {}).get("quote") or "", "method": METHOD,
              "evidence": {"duty": [duty] if duty else [], "activity": act.get("evidence") or {}}}
    record.write(conn, operates, values, why=why, valid_from=today)
    live[(act["id"], block_id)] = {**values, "version": 1}
    return "added"


def _reconcile(conn: Connection, live_opr: dict, live_obs: dict[str, dict], req: dict[str, list[str]],
               blocks_by_id: dict[str, dict], why: str) -> int:
    """Every operates edge keeps only duties that are live and still require its block, each
    quoted as it now reads; an edge left with none is closed."""
    closed = 0
    for e in list(live_opr.values()):
        refs = sorted(r for r in e["obligation_refs"] if r in live_obs
                      and e["block_id"] in {_canonical(blocks_by_id, b) for b in req.get(live_obs[r]["id"], [])})
        if not refs:
            record.invalidate(conn, operates, operates.c.id == e["id"], why=why,
                              reason="no live duty requires this measure any more")
            live_opr.pop((e["activity_id"], e["block_id"]), None)
            closed += 1
            continue
        found = _json(e.get("evidence"), {}) or {}
        wanted = {**found, "duty": [q for q in (_duty_quote(live_obs[r]) for r in refs) if q]}
        if refs != e["obligation_refs"] or found != wanted:
            conn.execute(operates.update().where(operates.c.id == e["id"]).values(
                obligation_refs=refs, evidence=wanted, version=(e.get("version") or 1) + 1, why_trail_id=why))
            e["obligation_refs"], e["evidence"] = refs, wanted
    return closed


# ----------------------------------------------------------------- the mapper


def map_activities(engine: Engine, llm=None, *, source_key: str | None = None) -> dict:
    """Every live obligation with a measure gets the activity its words describe."""
    from app.clhear.l1.scopes import active_name

    scope = active_name() or ""
    today = datetime.now(timezone.utc).date()
    counts = {"obligations": 0, "mapped": 0, "no_action": 0, "no_operator": 0, "activities_created": 0,
              "operates": {"added": 0, "changed": 0, "unchanged": 0}}
    with engine.begin() as conn:
        acts = live_activities(conn)
        req = _requires_by_obligation(conn)
        blocks_by_id = _live_blocks(conn)
        live_opr = {(e["activity_id"], e["block_id"]): e for e in _live_edges(conn, operates)}
        every = [dict(r) for r in conn.execute(sa.select(obligations).where(obligations.c.status.in_(LIVE))).mappings()]
        live_obs = {_ref(o): o for o in every}
        obs = [o for o in _live_obligations(conn, source_key) if req.get(o["id"])]
        trail = _why("l5.map", f"activities read from the words of {len(obs)} duties",
                     [{"layer": "L2", "table": "obligations", "id": o["id"]} for o in obs[:50]]).write(conn)
        for ob in obs:
            counts["obligations"] += 1
            reading = read_activity(ob, obligation_clauses(conn, ob))
            if reading is None:
                counts["no_action"] += 1
                evidence.record_gap(conn, scope=scope, layer="L5", kind="operator_not_stated", subject=ob["id"],
                                    source_key=ob["source_key"], clause_ref=ob["clause_ref"],
                                    missing="an action and who carries it out")
                continue
            if not reading["operator"]:
                counts["no_operator"] += 1
                evidence.record_gap(conn, scope=scope, layer="L5", kind="operator_not_stated", subject=ob["id"],
                                    source_key=ob["source_key"], clause_ref=ob["clause_ref"],
                                    missing="who carries out this duty", detail={"activity": reading["name"]})
            before = len(acts)
            act = _find_or_create(conn, acts, reading, trail)
            counts["activities_created"] += len(acts) - before
            _add_trigger(conn, act, ob, trail)
            for bid in sorted({_canonical(blocks_by_id, b) for b in req[ob["id"]]}):
                counts["operates"][_operate(conn, act, bid, ob, live_opr, trail, today, live_obs)] += 1
            counts["mapped"] += 1
        counts["operates"]["closed"] = _reconcile(conn, live_opr, live_obs, req, blocks_by_id, trail)
        if (counts["activities_created"] or counts["operates"]["added"] or counts["operates"]["changed"]
                or counts["operates"]["closed"]):
            publish_layer_event(conn, layer="L5", event="changed", subject_ref="l5.map",
                                payload={"counts": counts, "why_trail_id": trail}, producer=AGENT)
    log.info("L5 map: %s", counts)
    return counts


# ----------------------------------------------------------------- propagation (I1)


def on_l2_changed(engine: Engine, payload: dict) -> dict:
    """An L2 change: revoked -> the obligation leaves every operates edge it lit
    (an edge with nothing left is closed); added / updated -> map again."""
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
            trail = _why(ref, f"L2 change '{change}' on {ref}: operates edges it lit are withdrawn",
                         [{"layer": "L2", "table": "obligations", "id": ob["id"]}]).write(conn)
            for e in _live_edges(conn, operates):
                refs = e["obligation_refs"]
                if ref not in refs and ob["id"] not in refs:
                    continue
                remaining = [r for r in refs if r not in (ref, ob["id"])]
                if remaining:
                    conn.execute(operates.update().where(operates.c.id == e["id"]).values(
                        obligation_refs=remaining, version=(e.get("version") or 1) + 1, why_trail_id=trail))
                    out["unlit"] += 1
                else:
                    record.invalidate(conn, operates, operates.c.id == e["id"], why=trail, reason=f"obligation {change}")
                    out["invalidated"] += 1
            return out
    out["mapped"] = map_activities(engine, source_key=ob["source_key"])
    return out


def on_l4_changed(engine: Engine, payload: dict | None = None) -> dict:
    """Applicability does not change who does what: nothing to re-derive."""
    return {"ignored": True, "reason": "L5 activities do not depend on L4"}


# ----------------------------------------------------------------- reads


def coverage(engine: Engine) -> dict:
    """Obligation -> activity coverage (for the scorecard)."""
    with engine.connect() as conn:
        acts = live_activities(conn)
        obs = _live_obligations(conn)
    covered = {(t["anchor"]["source_key"], r) for a in acts for t in a["triggers"]
               for r in (t.get("anchor") or {}).get("refs") or []}
    mapped = [o for o in obs if (o["source_key"], o["clause_ref"]) in covered]
    return {"obligations": len(obs), "mapped": len(mapped), "unmapped": len(obs) - len(mapped),
            "ratio": (len(mapped) / len(obs)) if obs else None,
            "activities": {"business": sum(1 for a in acts if a["side"] == "business"),
                           "compliance": sum(1 for a in acts if a["side"] == "compliance")}}
