# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 applicability: which duties apply to an organisation, read from the duties' own words.

Every live L2 obligation gets ``applies_to`` edges. Each edge is a question the
text itself raises, with the quote it came from:

* ``jurisdiction`` — the source declares a jurisdiction: ``{"jurisdictions": "EU"}``;
* ``subject``      — the duty names who it binds, and that is not "every
  organisation" / "any person": ``{"roles": ["controller", "processor"]}``
  (any of them). A party is an organisation ("Are you a 'member'?") or a person
  or function inside one ("Does your organisation have a 'Supervisory
  Analyst'?"). A thing the duty is about ("Communications may not ...", "The
  illustration must ...") is not a party and raises no question;
* ``condition``    — the duty's own "where / if / unless" clause is about the
  addressee ("where an organisation processes personal data"):
  ``{"condition": "COND-…", "fact": "processes personal data", "expect": true}``.
  "unless" expects false. A clause about an event instead ("where an incident
  is likely to harm them", "when they join") is when the duty is performed,
  not whether it applies: it stays on the duty as a trigger.

No role, condition or value comes from a list in the code: they are the words
of the texts in scope, so a hospital's, a bank's and a factory's sources raise
their own questions.

An organisation's profile answers these questions. A duty **applies** when every
edge is answered and matches, is **not applicable** when an answer fails (the
failed edge is the reason), and is **undetermined** while a question it raises
has no answer: the blueprint then lists the question instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import applies_to, obligations
from app.clhear.l2.registry import PRONOUNS, binds_subject, head_phrase, is_universal, party_of, singular
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l4.predicates")

AGENT = "l4.predicates"
METHOD = "grounded-v1"
LIVE_STATUS = ("derived", "validated")

# Connectives that make a duty conditional on a fact. "when", "whenever",
# "before", "upon" time a duty; "subject to" and "except where" refer elsewhere.
_CONNECTIVE = re.compile(
    r"^\s*(?P<conn>where|if|unless|in the event that|to the extent that|provided that|in cases where|"
    r"in so far as|insofar as)\s+(?P<rest>.+?)[\s,;:.]*$", re.I | re.S)
_DETERMINERS = re.compile(r"^(?:(?:a|an|the|each|every|any|all|such|its|their)\s+)+", re.I)
_SPLIT_SUBJECT = re.compile(r"\s*(?:,|\band/or\b|\band\b|\bor\b)\s*", re.I)
_RELATIVE = re.compile(r"\s+(?P<rel>(?:who|that|which)\s+.+)$", re.I)
_RELATIVE_WORD = re.compile(r"^(?:which|who|whom|whose|that)$", re.I)
_QUALIFIER = re.compile(r"^(?:applicable|appropriate|possible|necessary|relevant|practicable|required|any)\b", re.I)


def _why(subject_ref: str, summary: str, evidence: list, *, method: str = METHOD,
         confidence: float | None = 1.0) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L4", reasoning_summary=summary, evidence_refs=evidence,
        inputs=(subject_ref, method, *[json.dumps(e, sort_keys=True) for e in evidence]),
        model_manifest={"model": "deterministic", "method": method}, skill_version=AGENT,
        confidence=confidence, agent_id=AGENT, subject_ref=subject_ref, input_layers=("L1", "L2"),
    )


# ----------------------------------------------------------------- reading the text


def norm(text: str) -> str:
    """Folded form used to match a profile's answer to a question."""
    words = re.findall(r"[a-z0-9]+", _DETERMINERS.sub("", (text or "").strip()).lower())
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def condition_id(fact: str) -> str:
    return "COND-" + hashlib.sha1(norm(fact).encode()).hexdigest()[:10]


def _quote_in(quotes: list[dict], part: str) -> list[dict]:
    """Narrow field quotes to the words of ``part`` (offsets stay into the clause)."""
    from app.clhear.evidence import locate

    out = []
    for q in quotes or []:
        span = locate(q["quote"], part)  # tolerant of line breaks inside the clause
        if span is not None:
            out.append({**q, "start": q["start"] + span[0], "end": q["start"] + span[1],
                        "quote": q["quote"][span[0]:span[1]]})
            break
    return out


def _label(head: str, first: bool) -> str:
    """The party's name as the text writes it, in the singular: "Members" -> "member",
    "Supervisory Analysts" -> "Supervisory Analyst"."""
    words = head.split()
    if not words:
        return head
    last = words[-1]
    one = singular(last)
    words[-1] = (one[:1].upper() + one[1:]) if last[:1].isupper() else one
    if first and len(words) == 1 and words[0][:1].isupper() and words[0][1:].islower():
        words[0] = words[0].lower()  # capitalised only because it opens the sentence
    return " ".join(words)


def addressee_roles(ob: dict) -> tuple[list[dict], list[dict]]:
    """(roles, relative-clause conditions) the duty's subject names.

    "The controller and the processor" -> two roles, either binds. "Every
    organisation" and pronouns name no role. A passive duty's subject is what
    the duty is about ("personal data shall be kept ..."), not who it binds; so
    is a thing ("Communications may not predict performance"). A person or
    function ("the Supervisory Analyst") is a role the organisation has."""
    subject = " ".join((ob.get("subject") or "").split())
    if not subject or not binds_subject(ob) or subject.lower() in PRONOUNS:
        return [], []
    quotes = ((ob.get("evidence") or {}).get("subject") or []) if isinstance(ob.get("evidence"), dict) else []
    roles, extra, parties = [], [], []
    # The relative clause first ("a firm that holds client money and deals on its own account"):
    # its "and" / "or" belong to the condition, not to a list of roles.
    heads, rel = subject, ""
    found = _RELATIVE.search(subject)
    if found:
        heads, rel = subject[:found.start()], found.group("rel")
    for i, part in enumerate(p for p in _SPLIT_SUBJECT.split(heads) if p.strip()):
        head = _DETERMINERS.sub("", part).strip()
        if not head or _CONNECTIVE.match(head) or _QUALIFIER.match(head) or _RELATIVE_WORD.match(head):
            continue  # a parenthetical ("where applicable") or a relative pronoun, not a noun phrase
        party = party_of(head)
        parties.append(party)
        if party not in ("organisation", "person"):
            continue  # anyone, or a thing the duty is about
        words, post = head_phrase(head)
        label = _label(words, first=subject.startswith(part.strip()))
        roles.append({"role": norm(label), "label": label, "party": party, "quotes": _quote_in(quotes, words)})
        if party == "organisation" and re.match(r"^\w+ed\b", post):
            # "Firms registered under the Investment Advisers Act": which organisations it binds.
            extra.append({"fact": post, "text": post, "expect": True, "quotes": _quote_in(quotes, post)})
    # A relative clause narrows who is bound when it is about an organisation or anyone
    # ("a firm that holds client money"); about a thing or a person it does not.
    if rel and any(p in ("organisation", "universal") for p in parties):
        fact = re.sub(r"^(?:who|that|which)\s+", "", rel, flags=re.I)
        extra.append({"fact": fact, "text": rel, "expect": True, "quotes": _quote_in(quotes, rel)})
    return roles, extra


_AUX = frozenset({"is", "are", "has", "have", "does", "do", "was", "were", "will", "can", "may", "uses", "holds"})


def _split_np(body: str) -> tuple[str, str]:
    """("covered entity", "maintains electronic ...") from "covered entity maintains
    electronic ...": the noun phrase ends before its verb (a third-person verb
    ending in -s, or an auxiliary). ("", body) when no verb is found early."""
    words = body.split()
    for i, word in enumerate(words[:5]):
        low = word.lower()
        if i and (low in _AUX or (low.endswith("s") and not low.endswith("ss") and len(low) > 3)):
            return " ".join(words[:i]), " ".join(words[i:])
    return "", body


def _about_addressee(rest: str, ob: dict) -> tuple[bool, str, str]:
    """(is the clause about the addressee?, the fact it states, the addressee it names).

    It is when it opens with a pronoun, with the duty's own subject, or with a
    word that stands for anyone ("an organisation", "a person"), and the duty
    binds an organisation. A clause about a thing ("if the illustration covers
    ...") or about a person inside the organisation ("if an associated person
    recommends ...") says when the duty is performed, not whether it applies.
    When the duty's subject is itself a pronoun ("where a covered entity
    maintains ..., it must ..."), the clause's noun phrase is the addressee."""
    words = rest.split()
    if not words:
        return False, "", ""
    subject = _DETERMINERS.sub("", " ".join((ob.get("subject") or "").split()))
    pronoun = not subject or subject.lower() in PRONOUNS
    party = "pronoun" if pronoun else party_of(subject)
    if words[0].lower() in PRONOUNS:
        return party in ("pronoun", "organisation", "universal"), " ".join(words[1:]), ""
    body = _DETERMINERS.sub("", rest)
    if not pronoun and body.lower().startswith(subject.lower() + " "):
        return party in ("organisation", "universal"), body[len(subject):].strip(), ""
    noun, fact = _split_np(body)
    if noun and is_universal(noun):
        return party in ("pronoun", "organisation", "universal"), fact, ""
    if noun and pronoun and party_of(noun) in ("organisation", "person"):
        return True, fact, noun
    return False, "", ""


def conditions_of(ob: dict) -> tuple[list[dict], list[dict]]:
    """(questions, triggers) from the duty's own condition clause(s)."""
    quotes = ((ob.get("evidence") or {}).get("condition") or []) if isinstance(ob.get("evidence"), dict) else []
    questions, triggers = [], []
    for part in [p.strip() for p in (ob.get("condition") or "").split("; ") if p.strip()]:
        quote = _quote_in(quotes, part)
        found = _CONNECTIVE.match(part)
        if found is None or _QUALIFIER.match(found.group("rest")):
            triggers.append({"text": part, "quotes": quote})
            continue
        about, fact, noun = _about_addressee(found.group("rest"), ob)
        if not about or not fact:
            triggers.append({"text": part, "quotes": quote})
            continue
        questions.append({"fact": fact, "text": part, "expect": found.group("conn").lower() != "unless",
                          "quotes": quote, "addressee": noun})
    return questions, triggers


def party_aliases(rows: list[dict]) -> dict[str, tuple[str, str]]:
    """{role key: (key, label) it is merged into} for variants of one person or function in
    these duties: "appropriately qualified registered principal" reads as "appropriately
    qualified principal" when both occur. Same head noun, and one's words within the other's."""
    labels: dict[str, str] = {}
    for ob in rows:
        for r in addressee_roles(ob)[0]:
            if r["party"] == "person":
                labels.setdefault(r["role"], r["label"])
    out = {}
    for key in labels:
        words, best = set(key.split()), key
        for other in labels:
            if (other != key and other.split()[-1] == key.split()[-1] and set(other.split()) < words
                    and len(other) < len(best)):
                best = other
        if best != key:
            out[key] = (best, labels[best])
    return out


def grounded_predicates(ob: dict, jurisdiction: str, aliases: dict | None = None) -> list[dict]:
    """The edges an obligation should have (no DB writes)."""
    out: list[dict] = []
    if jurisdiction:
        out.append({"predicate": {"jurisdictions": jurisdiction.upper()}, "basis": "jurisdiction",
                     "rationale": f"the source is declared for {jurisdiction.upper()}",
                     "evidence": {"source": {"layer": "L1", "source_key": ob["source_key"], "field": "jurisdiction",
                                             "value": jurisdiction.upper()}}})
    roles, relative = addressee_roles(ob)
    questions, _ = conditions_of(ob)
    for q in questions:
        if q.get("addressee"):  # "where a covered entity ..., it must": the clause names who is bound
            roles.append({"role": norm(q["addressee"]), "label": q["addressee"], "party": party_of(q["addressee"]),
                          "quotes": _quote_in(q["quotes"], q["addressee"])})
    for r in roles:
        if r["role"] in (aliases or {}):
            r["role"], r["label"] = aliases[r["role"]]
    if roles:
        out.append({"predicate": {"roles": sorted({r["role"] for r in roles})}, "basis": "subject",
                    "rationale": ob.get("subject") or "",
                    "evidence": {"subject": [q for r in roles for q in r["quotes"]],
                                 "labels": {r["role"]: r["label"] for r in roles},
                                 "parties": {r["role"]: r.get("party") or "organisation" for r in roles}}})
    for q in questions + relative:
        out.append({"predicate": {"condition": condition_id(q["fact"]), "fact": norm(q["fact"]), "expect": q["expect"]},
                    "basis": "condition", "rationale": q["text"],
                    "evidence": {"condition": q["quotes"], "fact": q["fact"]}})
    return out


# ----------------------------------------------------------------- writing edges


def _live_edges(conn: Connection, obligation_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sa.select(applies_to).where(applies_to.c.obligation_id == obligation_id)
                                          .where(applies_to.c.valid_to.is_(None))).mappings()]


def _pkey(predicate: dict) -> str:
    return json.dumps(predicate, sort_keys=True)


def _write_edge(conn: Connection, ob: dict, edge: dict, trail: str) -> str:
    eid = next_id(conn, "APL")
    record.write(conn, applies_to, {
        "id": eid, "obligation_id": ob["id"], "predicate": edge["predicate"], "basis": edge["basis"],
        "rationale": edge["rationale"], "method": METHOD, "obligation_text_hash": ob["text_hash"],
        "evidence": edge["evidence"],
    }, why=trail, valid_from=datetime.now(timezone.utc).date(),
        jurisdictions=[ob["jurisdiction"]] if ob.get("jurisdiction") else None)
    return eid


def sync_obligation(conn: Connection, ob: dict, jurisdiction: str, *, reason: str = "l4.predicates",
                    aliases: dict | None = None) -> dict:
    """Bring one obligation's edges in line with its words: add missing, re-stamp
    those whose text moved, close those the reading no longer produces."""
    if aliases is None:
        aliases = party_aliases(_live_obligations(conn))
    wanted = {_pkey(e["predicate"]): e for e in grounded_predicates(ob, jurisdiction, aliases)}
    ref = ob.get("stable_id") or ob["id"]
    counts = {"added": 0, "unchanged": 0, "restamped": 0, "invalidated": 0}
    trail: str | None = None

    def _trail() -> str:
        nonlocal trail
        if trail is None:
            quotes = [q for e in wanted.values() for v in e["evidence"].values() if isinstance(v, list) for q in v]
            trail = _why(ref, f"L4 applicability for {ref} ({reason}): {len(wanted)} edge(s) read from the text",
                         [{"layer": "L2", "table": "obligations", "id": ob["id"]}, *quotes]).write(conn)
        return trail

    for e in _live_edges(conn, ob["id"]):
        key = _pkey(e["predicate"])
        if key not in wanted or e["method"] != METHOD or (e.get("evidence") or {}) != wanted[key]["evidence"]:
            record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=_trail(),
                              reason="no longer read from the obligation")
            counts["invalidated"] += 1
        elif e["obligation_text_hash"] != ob["text_hash"]:
            record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=_trail(), reason="obligation text changed")
            _write_edge(conn, ob, wanted.pop(key), _trail())
            counts["restamped"] += 1
        else:
            counts["unchanged"] += 1
            wanted.pop(key)
    for edge in wanted.values():
        _write_edge(conn, ob, edge, _trail())
        counts["added"] += 1
    return counts


def _source_jurisdictions(conn: Connection) -> dict[str, str]:
    from app.clhear.l1.models import sources

    return {r.key: (r.jurisdiction or "").strip().upper() for r in conn.execute(sa.select(sources.c.key, sources.c.jurisdiction))}


def canonical_in(rows: list[dict]) -> list[dict]:
    """Rows that stand for themselves in this set. A near-duplicate is folded into its
    canonical only when that canonical is in the same set: a scope never loses a duty because
    its twin lives in another scope."""
    present = {r.get("stable_id") for r in rows} | {r["id"] for r in rows}
    return [r for r in rows if not r.get("canonical_id") or r["canonical_id"] not in present]


def _live_obligations(conn: Connection, source_key: str | None = None, limit: int | None = None) -> list[dict]:
    from app.clhear.l1.scopes import limiting

    q = sa.select(obligations).where(obligations.c.status.in_(LIVE_STATUS)).order_by(obligations.c.id)
    limit_to = limiting(obligations.c.source_key, source_key)
    if limit_to is not None:
        q = q.where(limit_to)
    rows = canonical_in([dict(r) for r in conn.execute(q).mappings()])
    return rows[:limit] if limit else rows


def extract_predicates(engine: Engine, llm=None, *, source_key: str | None = None, limit: int | None = None) -> dict:
    """Edges for every live canonical obligation in scope, from its own words."""
    totals = {"obligations": 0, "added": 0, "unchanged": 0, "restamped": 0, "invalidated": 0, "unconditional": 0}
    with engine.begin() as conn:
        jurisdiction_of = _source_jurisdictions(conn)
        aliases = party_aliases(_live_obligations(conn))
        for ob in _live_obligations(conn, source_key, limit):
            totals["obligations"] += 1
            counts = sync_obligation(conn, ob, jurisdiction_of.get(ob["source_key"], ""), aliases=aliases)
            for k in ("added", "unchanged", "restamped", "invalidated"):
                totals[k] += counts[k]
            if not _live_edges(conn, ob["id"]):
                totals["unconditional"] += 1
    log.info("L4 predicates: %s", totals)
    return totals


# ----------------------------------------------------------------- answering


def _answers(value) -> dict[str, bool]:
    """A profile's roles or conditions as {folded key: answer}. A list states
    what is true; a mapping may also say what is not. Condition ids are kept."""
    pairs = value.items() if isinstance(value, dict) else ((v, True) for v in value or () if isinstance(value, (list, tuple)))
    out = {}
    for key, answer in pairs:
        key = str(key).strip()
        if key and isinstance(answer, bool):
            out[key.upper() if key.upper().startswith("COND-") else norm(key)] = answer
    return out


def judge(edges: list[dict], attributes: dict) -> dict:
    """Three-valued verdict of one duty's edges against a profile."""
    failed, open_, matched = [], [], []
    roles = _answers(attributes.get("roles"))
    conditions = _answers(attributes.get("conditions"))
    jurisdictions = attributes.get("jurisdictions")
    for e in edges:
        pred = e["predicate"] or {}
        if "jurisdictions" in pred:
            if not jurisdictions:
                open_.append(e)
            elif pred["jurisdictions"].upper() in {str(j).strip().upper() for j in jurisdictions}:
                matched.append(e)
            else:
                failed.append(e)
        elif "roles" in pred:
            said = [roles.get(r) for r in pred["roles"]]
            if any(a is True for a in said):
                matched.append(e)
            elif said and all(a is False for a in said):
                failed.append(e)
            else:
                open_.append(e)
        elif "condition" in pred:
            answer = conditions.get(pred["condition"].upper(), conditions.get(pred.get("fact") or ""))
            if answer is None:
                open_.append(e)
            elif answer == bool(pred.get("expect", True)):
                matched.append(e)
            else:
                failed.append(e)
        else:  # an edge in a retired vocabulary: never decides anything
            open_.append(e)
    state = "not_applicable" if failed else ("undetermined" if open_ else "applies")
    return {"state": state, "failed": failed, "open": open_, "matched": matched}


def edges_by_obligation(conn: Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute(sa.select(applies_to).where(applies_to.c.valid_to.is_(None))).mappings():
        out.setdefault(r["obligation_id"], []).append(dict(r))
    return out


def applicability(conn: Connection, attributes: dict, *, source_keys=None) -> dict[str, dict]:
    """Every live obligation (optionally only those of ``source_keys``) and its
    verdict for an organisation with these attributes: ``state`` is applies,
    not_applicable (``failed`` says why) or undetermined (``open`` lists the
    unanswered questions). ``applies`` is kept as a boolean for callers."""
    edges = edges_by_obligation(conn)
    query = sa.select(obligations).where(obligations.c.status.in_(LIVE_STATUS))
    if source_keys is not None:
        query = query.where(obligations.c.source_key.in_(list(source_keys)))
    out: dict[str, dict] = {}
    for ob in canonical_in([dict(r) for r in conn.execute(query).mappings()]):
        es = edges.get(ob["id"], [])
        verdict = judge(es, attributes)
        out[ob["id"]] = {"obligation": dict(ob), "edges": es, "applies": verdict["state"] == "applies", **verdict}
    return out


def obligations_for_attributes(conn: Connection, attributes: dict, *, source_keys=None) -> list[dict]:
    """Obligations that apply to these attributes (every edge answered and matching)."""
    out = []
    for verdict in applicability(conn, attributes, source_keys=source_keys).values():
        if not verdict["applies"]:
            continue
        ob, es = verdict["obligation"], verdict["edges"]
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
    from app.clhear.l4 import validate as l4_validate

    with engine.connect() as conn:
        if isinstance(profile_or_attributes, str):
            row = l4_validate.get_profile(conn, profile_or_attributes)
            if row is None:
                raise KeyError(profile_or_attributes)
            attributes, pid = row["attributes"], row["id"]
        else:
            attributes, pid = profile_or_attributes, None
        items = obligations_for_attributes(conn, attributes)
    return {"profile_id": pid, "attributes": attributes, "count": len(items), "obligations": items}


# ----------------------------------------------------------------- asking in plain words


def _with_article(label: str) -> str:
    return f"{'an' if label[:1].lower() in 'aeiou' else 'a'} '{label}'"


def role_question(labels: list[str], parties: list[str]) -> str:
    """"Are you a 'member'?" for an organisation; "Does your organisation have a
    'Supervisory Analyst'?" for a person or function inside one."""
    names = " or ".join(_with_article(n) for n in labels)
    if parties and all(p == "person" for p in parties):
        return f"Does your organisation have {names}?"
    return f"Are you {names}?"


_MODALS = frozenset({"can", "will", "may", "must", "should", "shall", "could", "would"})


def condition_question(fact: str, text: str = "") -> str:
    """The clause's fact as a question about the organisation, in the clause's words:
    "processes personal data on a large scale" -> "Does your organisation process personal
    data on a large scale?". Falls back to the clause itself when its fact does not open
    with a verb."""
    words = " ".join((fact or "").split()).rstrip(" ,;:.?").split()
    fallback = f"Does this apply to your organisation: {' '.join((text or fact or '').split()).rstrip(' ,;:.?')}?"
    if not words:
        return fallback
    first, rest = words[0].lower(), " ".join(words[1:])
    if first in ("is", "are"):
        return f"Is your organisation {rest}?"
    if first in ("was", "were"):
        return f"Was your organisation {rest}?"
    if first in ("has", "have"):
        if len(words) > 1 and re.match(r"^\w+(?:ed|en|wn|ne|un)$", words[1], re.I):
            return f"Has your organisation {rest}?"  # "has filed a draft version"
        return f"Does your organisation have {rest}?"
    if first in ("does", "do"):
        return f"Does your organisation {rest}?"
    if first in _MODALS:
        return f"{first.capitalize()} your organisation {rest}?"
    if len(first) > 3 and first.endswith("ed"):
        return f"Is your organisation {' '.join(words)}?"
    if len(first) > 2 and first.endswith("s") and not first.endswith(("ss", "us", "is")):
        # "holds client money and deals on its own account" -> "hold ... and deal on ..."
        rest = re.sub(r"\b(and|or)\s+(\w+?s)\s+(?=(?:a|an|the|its|their|on|in|with|to|for|from|any|all)\b)",
                      lambda m: f"{m.group(1)} {singular(m.group(2))} ", rest)
        return f"Does your organisation {singular(first)} {rest}?".replace(" ?", "?")
    return fallback


# ----------------------------------------------------------------- the questions a scope raises


def questions(conn: Connection, source_keys) -> dict:
    """The profile questions the texts in scope raise, each with its quotes."""
    from app.clhear.l1.models import sources

    keys = sorted(source_keys or [])
    declared = sorted({(r.jurisdiction or "").strip().upper() for r in conn.execute(
        sa.select(sources.c.jurisdiction).where(sources.c.key.in_(keys))) if (r.jurisdiction or "").strip()})
    obs = {r["id"]: r for r in canonical_in([dict(r) for r in conn.execute(
        sa.select(obligations).where(obligations.c.source_key.in_(keys))
        .where(obligations.c.status.in_(LIVE_STATUS))).mappings()])}
    roles: dict[str, dict] = {}
    conditions: dict[str, dict] = {}
    for oid, es in edges_by_obligation(conn).items():
        ob = obs.get(oid)
        if ob is None:
            continue
        for e in es:
            pred, found = e["predicate"] or {}, e.get("evidence") or {}
            if "roles" in pred:
                labels = found.get("labels") or {}
                parties = found.get("parties") or {}
                for role in pred["roles"]:
                    party = parties.get(role) or "organisation"
                    slot = roles.setdefault(role, {"role": role, "label": labels.get(role, role), "party": party,
                                                   "ask": role_question([labels.get(role, role)], [party]),
                                                   "duties": [], "quotes": []})
                    slot["duties"].append(ob["stable_id"] or oid)
                    slot["quotes"].extend(q for q in found.get("subject") or [] if role == norm(q["quote"]))
            elif "condition" in pred:
                slot = conditions.setdefault(pred["condition"], {
                    "id": pred["condition"], "fact": found.get("fact") or pred.get("fact"), "text": e["rationale"],
                    "ask": condition_question(found.get("fact") or pred.get("fact") or "", e["rationale"]),
                    "duties": [], "quotes": []})
                slot["duties"].append(ob["stable_id"] or oid)
                slot["quotes"].extend(found.get("condition") or [])
    for slot in list(roles.values()) + list(conditions.values()):
        slot["duties"] = sorted(set(slot["duties"]))
        seen, unique = set(), []
        for q in slot["quotes"]:
            key = (q.get("clause_id"), q.get("start"))
            if key not in seen:
                seen.add(key)
                unique.append(q)
        slot["quotes"] = unique[:5]
    return {"jurisdictions": declared, "roles": sorted(roles.values(), key=lambda r: r["role"]),
            "conditions": sorted(conditions.values(), key=lambda c: c["fact"] or "")}


# ----------------------------------------------------------------- propagation + reads


def on_l2_changed(engine: Engine, payload: dict) -> dict:
    """I1: an L2 change re-reads / closes the obligation's applicability edges."""
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
            trail = _why(ref, f"L2 change '{change}' on {ref}: applicability edges withdrawn",
                         [{"layer": "L2", "table": "l2_change_events", "id": payload.get("change_event_id")}]).write(conn)
            n = 0
            for e in _live_edges(conn, ob["id"]):
                record.invalidate(conn, applies_to, applies_to.c.id == e["id"], why=trail, reason=f"obligation {change}")
                n += 1
            return {"obligation": ref, "change": change, "invalidated": n}
        counts = sync_obligation(conn, ob, _source_jurisdictions(conn).get(ob["source_key"], ""),
                                 reason=f"l2.changed:{change}")
    return {"obligation": ref, "change": change, **counts}


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
