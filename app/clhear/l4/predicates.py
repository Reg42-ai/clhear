# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 applicability predicates (HLD v2 §4.4): obligation -> profile predicate.

Every live L2 obligation gets ``applies_to`` edges written in the shared
predicate language (:func:`app.clhear.l4.ontology.matches`). Edges are
conjunctive: an obligation applies to a profile when *all* of its live edges
match. Three deterministic bases, one optional grounded LLM basis:

* ``jurisdiction`` — the obligation's jurisdiction (always, when known);
* ``subject``      — who the duty addresses (investment firm, CASP, e-money
  institution, financial entity, controller ...) mapped to ontology
  authorisations / flags;
* ``condition``    — the duty's condition (retail clients, client money,
  custody, distance marketing ...) mapped to products / client types / channels;
* ``llm``          — for obligations whose subject/condition carry no cue, a
  closed-world extraction over the attribute schema and ontology names; the
  model must quote the span it read, and any value that does not resolve to an
  ontology row is discarded (grounding contract, no general knowledge).

Every edge carries its rationale (the cue text) and a why-trail (I3); stale
edges are invalidated, never deleted (I2); an L2 change re-stamps them (I1).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import applies_to, attribute_schema, obligations
from app.clhear.l4 import validate as l4_validate
from app.clhear.l4.ontology import Lookup, matches
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l4.predicates")

AGENT = "l4.predicates"
METHOD = "deterministic-v1"
LIVE_STATUS = ("derived", "validated")

# cue regex -> (attribute, value | list of licence ids | callable). Licence ids are
# resolved against the live ontology and narrowed to the obligation's jurisdiction.
_SUBJECT_CUES: list[tuple[str, str, object]] = [
    (r"\binvestment firms?\b|\bMiFID (?:investment )?firms?\b|\bauthorised firms?\b",
     "authorisations", ["LIC:UK:mifid-investment-firm", "LIC:EU:mifid-investment-firm", "LIC:US:sec-broker-dealer"]),
    (r"\bcrypto-?asset service providers?\b|\bCASPs?\b", "authorisations", ["LIC:EU:casp", "LIC:UK:cryptoasset-registration"]),
    (r"\bcrypto-?assets?\b", "crypto_services", True),
    (r"\belectronic money institutions?\b|\be-?money institutions?\b|\bEMIs?\b|\bissuers? of electronic money\b",
     "authorisations", ["LIC:UK:e-money-institution", "LIC:EU:e-money-institution"]),
    (r"\bpayment institutions?\b|\bpayment service providers?\b|\bPSPs?\b",
     "authorisations", ["LIC:UK:payment-institution", "LIC:EU:payment-institution", "LIC:UK:e-money-institution", "LIC:EU:e-money-institution"]),
    (r"\bcredit institutions?\b|\bbanks?\b", "authorisations", ["LIC:EU:credit-institution"]),
    (r"\bbroker-?dealers?\b|\bbrokers? or dealers?\b", "authorisations", ["LIC:US:sec-broker-dealer"]),
    (r"\bFINRA members?\b|\bmember firms?\b", "authorisations", ["LIC:US:finra-member"]),
    (r"\binvestment advisers?\b", "authorisations", ["LIC:US:sec-investment-adviser"]),
    (r"\bmoney services business(?:es)?\b|\bmoney transmitters?\b|\bMSBs?\b",
     "authorisations", ["LIC:US:fincen-msb", "LIC:US:state-money-transmitter"]),
    (r"\bfutures commission merchants?\b|\bFCMs?\b", "authorisations", ["LIC:US:cftc-fcm"]),
    (r"\bmembers?\b(?! states?)", "authorisations", ["LIC:US:finra-member"], ("US",)),
    (r"\bfinancial entit(?:y|ies)\b", "financial_entity_dora", True),
    (r"\b(?:data )?controllers?\b|\b(?:data )?processors?\b", "data_footprint", "*"),
    # "a firm" / "authorised person" / "obliged entity": any regulated organisation — some
    # authorisation is held, which one the text does not say (no invented licence).
    (r"\bfirms?\b|\bauthorised persons?\b|\bobliged entit(?:y|ies)\b|\brelevant persons?\b|\bregulated entit(?:y|ies)\b",
     "authorisations", "*"),
]

_CONDITION_CUES: list[tuple[str, str, object]] = [
    (r"\bretail (?:clients?|customers?|investors?)\b|\bconsumers?\b", "customer_base", ["retail"]),
    (r"\bprofessional clients?\b", "customer_base", ["professional"]),
    (r"\beligible counterpart(?:y|ies)\b", "customer_base", ["eligible counterparty"]),
    (r"\baccredited investors?\b", "customer_base", ["accredited investor"]),
    (r"\bclient money\b|\bclient funds\b|\bcustomer funds\b", "products", ["client money holding"]),
    (r"\bcustod(?:y|ian)\b|\bclient assets\b|\bsafekeeping\b", "products", ["custody of client assets", "crypto custody"]),
    (r"\bcontracts? for differences?\b|\bCFDs?\b|\bleveraged\b", "products", ["CFDs and leveraged derivatives"]),
    (r"\belectronic money\b|\be-?money\b", "products", ["e-money issuance"]),
    (r"\bpayment accounts?\b|\bpayment services?\b", "products", ["payment accounts"]),
    (r"\bportfolio management\b|\bmanag(?:es|ing) investments\b", "products", ["portfolio management"]),
    (r"\binvestment advice\b|\badvis(?:es|ing) on investments\b", "products", ["investment advice"]),
    (r"\bcrypto-?assets?\b", "crypto_services", True),
    (r"\bdistance\b|\bonline\b|\bwebsite\b|\bmobile app(?:lication)?s?\b|\belectronic means\b|\bdigital channels?\b",
     "channels", ["online and mobile app"]),
    (r"\bintroduc(?:ers?|ing brokers?)\b|\bintermediar(?:y|ies)\b|\bappointed representatives?\b|\btied agents?\b",
     "channels", ["introducing brokers and intermediaries"]),
    (r"\bsocial media\b|\bpromoters?\b", "channels", ["social promotion"]),
]


def _why(subject_ref: str, summary: str, evidence: list[str], *, method: str = METHOD,
         confidence: float | None = 1.0, manifest: dict | None = None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L4", reasoning_summary=summary, evidence_refs=evidence, inputs=(subject_ref, method, *evidence),
        model_manifest=manifest or {"model": "deterministic", "method": method}, skill_version=AGENT,
        confidence=confidence, agent_id=AGENT, subject_ref=subject_ref, input_layers=("L1", "L2"),
    )


class _Onto:
    def __init__(self, conn: Connection):
        self.view = l4_validate.Ontology(conn)
        self.schema = self.view.schema

    def licence_names(self, ids: list[str], jurisdiction: str) -> list[str]:
        rows = [self.view.licences.rows[i] for i in ids if i in self.view.licences.rows]
        narrowed = [r for r in rows if not jurisdiction or r["jurisdiction"].upper() == jurisdiction.upper()]
        return sorted(r["name"] for r in (narrowed or rows))

    def licences_naming(self, addressee: str, jurisdiction: str) -> list[str]:
        """Licences of the obligation's jurisdiction whose name carries the addressee the
        cue read: in a derived ontology "investment advisers" is "Investment Adviser
        Registration", whatever id the licence extraction gave it."""
        words = re.findall(r"[a-z0-9]+", addressee.lower())
        if not words or not jurisdiction:
            return []
        words[-1] = words[-1][:-1] if words[-1].endswith("s") and len(words[-1]) > 3 else words[-1]
        phrase = " ".join(words)
        return sorted(r["name"] for r in self.view.licences.rows.values()
                      if r["jurisdiction"].upper() == jurisdiction.upper()
                      and phrase in " ".join(re.findall(r"[a-z0-9]+", r["name"].lower())))

    def resolve_values(self, attribute: str, values: list) -> list[str]:
        collection = l4_validate.LIST_KEYS_WITH_ONTOLOGY.get(attribute)
        if collection is None:
            return [str(v) for v in values]
        lookup: Lookup = self.view.lookups[collection]
        out = []
        for v in values:
            row = lookup.resolve(str(v))
            if row is not None and row["name"] not in out:
                out.append(row["name"])
        return out


def _scan(text: str, cues: list, onto: _Onto, jurisdiction: str, basis: str) -> list[dict]:
    """Deterministic predicates from one text field. One predicate per attribute
    (values union when several cues hit the same attribute)."""
    found: dict[str, dict] = {}
    for cue in cues:
        pattern, attribute, target = cue[0], cue[1], cue[2]
        only = cue[3] if len(cue) > 3 else None
        if only and jurisdiction.upper() not in only:
            continue
        m = re.search(pattern, text or "", flags=re.IGNORECASE)
        if not m or attribute not in onto.schema:
            continue
        if isinstance(target, list) and target and str(target[0]).startswith("LIC:"):
            values = onto.licence_names(target, jurisdiction) or onto.licences_naming(m.group(0), jurisdiction)
        elif isinstance(target, list):
            values = onto.resolve_values(attribute, target)
        else:
            values = target
        if isinstance(values, list) and not values:
            continue  # nothing in the ontology backs this cue: honest gap, no edge
        slot = found.get(attribute)
        if slot is None:
            slot = found[attribute] = {"attribute": attribute, "values": values, "cues": [], "basis": basis}
        elif isinstance(values, list) and isinstance(slot["values"], list):
            slot["values"] = sorted(set(slot["values"]) | set(values))
        elif values == "*" and slot["values"] != "*":
            continue  # a specific reading already covers the generic cue
        else:
            slot["values"] = values
        slot["cues"].append(m.group(0))
    out = []
    for slot in found.values():
        value = slot["values"]
        if isinstance(value, list):
            value = sorted(set(value))
            if len(value) == 1:
                value = value[0]
        out.append({"predicate": {slot["attribute"]: value}, "basis": basis,
                    "rationale": f"{basis}: '{'; '.join(dict.fromkeys(slot['cues']))}'"})
    return out


def deterministic_predicates(ob: dict, onto: _Onto) -> list[dict]:
    """The deterministic edges an obligation should have (no DB writes)."""
    out: list[dict] = []
    jur = (ob.get("jurisdiction") or "").strip()
    if jur and jur.upper() in onto.view.jurisdictions:
        out.append({"predicate": {"jurisdictions": jur.upper()}, "basis": "jurisdiction",
                    "rationale": f"jurisdiction: obligation derived from a {jur.upper()} source"})
    subject_text = " ".join(filter(None, [ob.get("subject"), ob.get("addressee")]))
    subject = _scan(subject_text, _SUBJECT_CUES, onto, jur, "subject") if subject_text.strip() else []
    if not subject:
        # The extracted addressee can be the wrong noun ("advertisement" for "any
        # investment adviser ... to disseminate any advertisement"); the duty's own
        # words then name who bears it.
        # The structured duty sentence can drop the bearer ("unless you adopt ..."); the
        # statement it came from keeps it ("If you are an investment adviser ...").
        for text in (ob.get("determination"), ob.get("statement")):
            subject = _scan((text or "")[:300], _SUBJECT_CUES, onto, jur, "subject") if text else []
            if subject:
                break
    out += subject
    # Structured `condition` when L2 has split the duty; otherwise the duty sentence itself.
    condition_text = ob.get("condition") or ob.get("determination") or ob.get("statement") or ""
    out += _scan(condition_text, _CONDITION_CUES, onto, jur, "condition")
    # A subject edge already asserting crypto/DORA/... shadows the same condition edge.
    seen: set[str] = set()
    deduped = []
    for e in out:
        key = json.dumps(e["predicate"], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def _live_edges(conn: Connection, obligation_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sa.select(applies_to).where(applies_to.c.obligation_id == obligation_id)
                                          .where(applies_to.c.valid_to.is_(None))).mappings()]


def _pkey(predicate: dict) -> str:
    return json.dumps(predicate, sort_keys=True)


def _write_edge(conn: Connection, ob: dict, edge: dict, trail: str, *, method: str = METHOD, confidence: float | None = 1.0) -> str:
    eid = next_id(conn, "APL")
    record.write(conn, applies_to, {
        "id": eid, "obligation_id": ob["id"], "predicate": edge["predicate"], "basis": edge["basis"],
        "rationale": edge["rationale"], "method": method, "obligation_text_hash": ob["text_hash"],
    }, why=trail, valid_from=datetime.now(timezone.utc).date(),
        jurisdictions=[ob["jurisdiction"]] if ob.get("jurisdiction") else None)
    return eid


def sync_obligation(conn: Connection, ob: dict, onto: _Onto, *, reason: str = "l4.predicates") -> dict:
    """Bring one obligation's deterministic edges in line: add missing, re-stamp
    stale (text hash moved), invalidate those the reading no longer produces."""
    wanted = {_pkey(e["predicate"]): e for e in deterministic_predicates(ob, onto)}
    live = _live_edges(conn, ob["id"])
    ref = ob.get("stable_id") or ob["id"]
    counts = {"added": 0, "unchanged": 0, "restamped": 0, "invalidated": 0}
    trail: str | None = None

    def _trail():
        nonlocal trail
        if trail is None:
            trail = _why(ref, f"L4 applicability predicates for {ref} ({reason}): {len(wanted)} deterministic edge(s)",
                         [ob["id"], f"text_hash:{ob['text_hash']}"]).write(conn)
        return trail

    for e in live:
        key = _pkey(e["predicate"])
        if e["method"] != METHOD and e["basis"] == "llm":
            # LLM edges survive unless the obligation text moved under them.
            if e["obligation_text_hash"] != ob["text_hash"]:
                record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=_trail(), reason="obligation text changed")
                counts["invalidated"] += 1
            continue
        if key not in wanted:
            record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=_trail(), reason="no longer read from the obligation")
            counts["invalidated"] += 1
        elif e["obligation_text_hash"] != ob["text_hash"]:
            record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=_trail(), reason="obligation text changed")
            _write_edge(conn, ob, wanted[key], _trail())
            counts["restamped"] += 1
            wanted.pop(key)
        else:
            counts["unchanged"] += 1
            wanted.pop(key)
    for edge in wanted.values():
        _write_edge(conn, ob, edge, _trail())
        counts["added"] += 1
    return counts


def _live_obligations(conn: Connection, source_key: str | None = None, limit: int | None = None) -> list[dict]:
    from app.clhear.l1.scopes import limiting

    q = (sa.select(obligations).where(obligations.c.status.in_(LIVE_STATUS)).where(obligations.c.canonical_id.is_(None))
         .order_by(obligations.c.id))
    limit_to = limiting(obligations.c.source_key, source_key)
    if limit_to is not None:
        q = q.where(limit_to)
    if limit:
        q = q.limit(limit)
    return [dict(r) for r in conn.execute(q).mappings()]


def extract_predicates(engine: Engine, llm=None, *, source_key: str | None = None, limit: int | None = None,
                       llm_limit: int = 25) -> dict:
    """Nightly pass: deterministic edges for every live canonical obligation,
    then a grounded LLM pass over obligations that only got a jurisdiction edge."""
    totals = {"obligations": 0, "added": 0, "unchanged": 0, "restamped": 0, "invalidated": 0,
              "jurisdiction_only": 0, "llm_edges": 0, "llm_discarded": 0}
    thin: list[dict] = []
    with engine.begin() as conn:
        onto = _Onto(conn)
        for ob in _live_obligations(conn, source_key, limit):
            totals["obligations"] += 1
            c = sync_obligation(conn, ob, onto)
            for k in ("added", "unchanged", "restamped", "invalidated"):
                totals[k] += c[k]
            live = _live_edges(conn, ob["id"])
            if all(e["basis"] == "jurisdiction" for e in live):
                totals["jurisdiction_only"] += 1
                if llm is not None and not any(e["basis"] == "llm" for e in live):
                    thin.append(ob)
    if llm is not None and thin:
        for ob in thin[:llm_limit]:
            res = llm_predicates(engine, llm, ob)
            totals["llm_edges"] += res["written"]
            totals["llm_discarded"] += res["discarded"]
    log.info("L4 predicates: %s", totals)
    return totals


# ----------------------------------------------------------------- grounded LLM pass


def _allowed_values(onto: _Onto) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, collection in l4_validate.LIST_KEYS_WITH_ONTOLOGY.items():
        out[key] = onto.view.lookups[collection].names()
    out["jurisdictions"] = sorted(onto.view.jurisdictions)
    for key, spec in onto.schema.items():
        if spec["type"] == "bool":
            out[key] = ["true", "false"]
    return out


def llm_predicates(engine: Engine, llm, ob: dict) -> dict:
    """Closed-world applicability read. The model sees the obligation text and
    the attribute schema with allowed values only; every predicate must quote
    a span of the text and every value must resolve in the ontology."""
    from app.clhear.platform.gateway import parse_json_object
    from app.clhear.platform.router import complete

    with engine.connect() as conn:
        onto = _Onto(conn)
    allowed = _allowed_values(onto)
    text = ob.get("determination") or ob.get("statement") or ob.get("title") or ""
    prompt = (
        "Which organisations does this obligation apply to? Answer ONLY with attributes from the schema and "
        "values from the allowed lists. Every predicate MUST quote the exact words of the obligation you read it from. "
        "If the text does not narrow applicability beyond jurisdiction, return an empty list.\n"
        'JSON: {"predicates": [{"attribute": "", "values": [""], "quote": ""}]}\n\n'
        f"Schema and allowed values: {json.dumps(allowed)}\n\n"
        f"Obligation ({ob.get('stable_id') or ob['id']}, {ob.get('jurisdiction')}):\n{text[:1800]}"
    )
    try:
        result = complete(llm, "l4.applicability", prompt=prompt,
                          system="Extractive, closed-world. No general knowledge. JSON only.",
                          required_keys=["predicates"], max_tokens=500)
        parsed = parse_json_object(result.text)
    except Exception:
        log.exception("l4.applicability failed for %s", ob["id"])
        return {"written": 0, "discarded": 1}
    written = discarded = 0
    folded_text = text.lower()
    edges: list[dict] = []
    for item in parsed.get("predicates") or []:
        if not isinstance(item, dict):
            discarded += 1
            continue
        attribute = str(item.get("attribute") or "")
        quote = str(item.get("quote") or "").strip()
        if attribute not in onto.schema or not quote or quote.lower() not in folded_text:
            discarded += 1
            continue
        raw = item.get("values") if isinstance(item.get("values"), list) else [item.get("values")]
        spec = onto.schema[attribute]
        if spec["type"] == "bool":
            value = str(raw[0]).lower() == "true" if raw else None
            if value is None:
                discarded += 1
                continue
        elif attribute == "jurisdictions":
            value = [str(v).upper() for v in raw if str(v).upper() in onto.view.jurisdictions]
        elif attribute in l4_validate.LIST_KEYS_WITH_ONTOLOGY:
            value = onto.resolve_values(attribute, [v for v in raw if v is not None])
        else:
            value = "*"
        if isinstance(value, list):
            if not value:
                discarded += 1
                continue
            if len(value) == 1:
                value = value[0]
        edges.append({"predicate": {attribute: value}, "basis": "llm", "rationale": f"llm: '{quote[:200]}'"})
    if not edges:
        return {"written": 0, "discarded": discarded}
    manifest = {"model": getattr(result, "model", ""), "task": "l4.applicability", "method": "grounded-llm"}
    with engine.begin() as conn:
        live_keys = {_pkey(e["predicate"]) for e in _live_edges(conn, ob["id"])}
        trail = _why(ob.get("stable_id") or ob["id"], f"grounded LLM applicability read: {len(edges)} predicate(s), {discarded} discarded",
                     [ob["id"], f"text_hash:{ob['text_hash']}"], method="grounded-llm",
                     confidence=getattr(result, "confidence", None) or 0.85, manifest=manifest).write(conn)
        for e in edges:
            if _pkey(e["predicate"]) in live_keys:
                continue
            _write_edge(conn, ob, e, trail, method=manifest["model"] or "grounded-llm", confidence=0.85)
            written += 1
    return {"written": written, "discarded": discarded}


# ----------------------------------------------------------------- propagation + reads


def on_l2_changed(engine: Engine, payload: dict) -> dict:
    """I1: an L2 change re-stamps / invalidates the obligation's applicability edges."""
    oid = payload.get("derivation_key") or payload.get("obligation_id")
    change = payload.get("change") or payload.get("kind")
    if not oid:
        return {"ignored": True, "reason": "no obligation in payload"}
    with engine.begin() as conn:
        ob = conn.execute(sa.select(obligations).where(sa.or_(obligations.c.id == oid, obligations.c.stable_id == oid))).mappings().first()
        if ob is None:
            return {"ignored": True, "reason": f"unknown obligation {oid}"}
        ob = dict(ob)
        ref = ob["stable_id"] or ob["id"]
        if change == "revoked" or ob["status"] not in LIVE_STATUS:
            trail = _why(ref, f"L2 change '{change}' on {ref}: applicability edges withdrawn", [str(payload.get("change_event_id") or "")]).write(conn)
            n = 0
            for e in _live_edges(conn, ob["id"]):
                record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=trail, reason=f"obligation {change}")
                n += 1
            return {"obligation": ref, "change": change, "invalidated": n}
        counts = sync_obligation(conn, ob, _Onto(conn), reason=f"l2.changed:{change}")
    return {"obligation": ref, "change": change, **counts}


def edges_by_obligation(conn: Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute(sa.select(applies_to).where(applies_to.c.valid_to.is_(None))).mappings():
        out.setdefault(r["obligation_id"], []).append(dict(r))
    return out


def obligations_for_attributes(conn: Connection, attributes: dict) -> list[dict]:
    """Obligations whose live applies_to edges all match the attributes."""
    edges = edges_by_obligation(conn)
    if not edges:
        return []
    rows = {r["id"]: dict(r) for r in conn.execute(
        sa.select(obligations).where(obligations.c.id.in_(list(edges))).where(obligations.c.status.in_(LIVE_STATUS))).mappings()}
    out = []
    for oid, es in edges.items():
        ob = rows.get(oid)
        # An obligation applies through L4 only when its jurisdiction is known
        # (a subject-only reading must not attach a foreign duty to every firm).
        if ob is None or not any(e["basis"] == "jurisdiction" for e in es):
            continue
        if not all(matches(e["predicate"], attributes) for e in es):
            continue
        out.append({
            "obligation_id": ob["stable_id"] or ob["id"], "derivation_key": ob["id"], "title": ob["title"],
            "jurisdiction": ob["jurisdiction"], "regulator": ob["regulator"], "source_key": ob["source_key"],
            "clause_ref": ob["clause_ref"], "status": ob["status"], "obligation_type": ob["obligation_type"],
            "confidence": float(ob["confidence"] or 0),
            "predicates": [{"id": e["id"], "predicate": e["predicate"], "basis": e["basis"], "rationale": e["rationale"],
                            "why_trail_id": e["why_trail_id"]} for e in es],
        })
    out.sort(key=lambda o: (o["jurisdiction"], o["source_key"], o["clause_ref"]))
    return out


def obligations_for_profile(engine: Engine, profile_or_attributes) -> dict:
    with engine.connect() as conn:
        if isinstance(profile_or_attributes, str):
            row = l4_validate.get_profile(conn, profile_or_attributes)
            if row is None:
                raise KeyError(profile_or_attributes)
            attributes = row["attributes"]
            pid = row["id"]
        else:
            attributes, pid = profile_or_attributes, None
        items = obligations_for_attributes(conn, attributes)
    return {"profile_id": pid, "attributes": attributes, "count": len(items), "obligations": items}


def coverage(engine: Engine) -> dict:
    """How much of the live registry carries applicability edges (scorecard)."""
    with engine.connect() as conn:
        live = _live_obligations(conn)
        edges = edges_by_obligation(conn)
    by_basis: dict[str, int] = {}
    for es in edges.values():
        for e in es:
            by_basis[e["basis"]] = by_basis.get(e["basis"], 0) + 1
    with_edge = sum(1 for ob in live if ob["id"] in edges)
    narrowed = sum(1 for ob in live if any(e["basis"] != "jurisdiction" for e in edges.get(ob["id"], [])))
    return {"obligations": len(live), "with_predicates": with_edge, "narrowed_beyond_jurisdiction": narrowed,
            "rate": round(with_edge / len(live), 4) if live else 0.0, "edges_by_basis": by_basis}


def schema_keys(conn: Connection) -> set[str]:
    return {r[0] for r in conn.execute(sa.select(attribute_schema.c.key))}
