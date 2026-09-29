# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Stated penalties: what the binding texts in scope say a breach costs.

L7 reads the penalty clauses of the scope's binding sources (kind ``law`` or
``regulation``): clauses that make a breach an offence, or liable to
imprisonment, a fine or penalty, suspension, revocation or disqualification.
Each penalty is kept with its type and the maximum the clause states, both
quoted with offsets, and linked to the obligations whose provisions the clause
refers to:

* a provision it cites ("fails to comply with section 1"), resolved like any
  cross-reference (``l1.references``), with every clause inside that provision;
* a sibling provision ("subsection (1)") in the same unit;
* the whole text ("this Act", "these Rules"), or its enclosing division
  ("this Part").

A clause that refers to nothing is kept, unlinked. A stated penalty is a risk
input even with no enforcement source in scope; enforcement events stay a
separate, stronger input. The grammar is generic legal drafting; it names no
regime, amount or authority.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import date

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

BINDING_KINDS = ("law", "regulation")

# A clause that states a consequence for a breach.
LIABILITY = re.compile(
    r"\b(?:liable|commits? an offen[cs]e|guilty of an offen[cs]e|is an offen[cs]e|punishable|"
    r"shall be (?:fined|imprisoned|punished)|may (?:impose|suspend|revoke|cancel|disqualify)|"
    r"subject to (?:an? )?(?:\w+ )?(?:penalty|penalties|fine|sanction))", re.I)
# The types, most severe first; a later type inside an earlier one's words is the same penalty.
TYPES: tuple[tuple[str, re.Pattern], ...] = (
    ("imprisonment", re.compile(r"\bimprison(?:ment|ed)\b|\bcustodial sentence\b", re.I)),
    ("disqualification", re.compile(r"\bdisqualif(?:y|ied|ication)\b", re.I)),
    ("revocation", re.compile(r"\brevo(?:ke|ked|cation)\b|\bcancel(?:led|lation)\b", re.I)),
    ("suspension", re.compile(r"\bsuspen(?:d|ded|sion)\b", re.I)),
    ("fine", re.compile(r"\bfine[sd]?\b|\b(?:monetary|pecuniary|financial|administrative) penalt(?:y|ies)\b", re.I)),
    ("penalty", re.compile(r"\b(?:civil )?penalt(?:y|ies)\b", re.I)),
)
RANK = {"imprisonment": 1.0, "disqualification": 0.8, "revocation": 0.8, "suspension": 0.7, "fine": 0.6,
        "penalty": 0.6}
_LIMIT = r"(?:not exceeding|not more than|no more than|of up to|up to|a maximum of|maximum of|shall not exceed|" \
         r"does not exceed|of not more than)"
# A stated maximum runs to the end of its phrase; "2,000" and "2.5" keep their separators.
_PHRASE = r"(?P<max>(?:[^,;.()]|(?<=\d)[,.](?=\d))+?)(?=\s*(?:(?<!\d)[,.]|[,.](?!\d)|[;()]|\bor\b|\band\b|$))"
_MAXIMUM = re.compile(rf"\b{_LIMIT}\s+{_PHRASE}", re.I)
_OF_MAXIMUM = re.compile(rf"^\s+of\s+{_PHRASE}", re.I)
_AMOUNT = re.compile(
    r"(?P<cur>[$£€¥]|\b[A-Z]{3}\b)?\s*(?P<num>\d{1,3}(?:[,  ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>million|billion|thousand|bn|m|k)?\b\s*(?P<unit>(?:penalty\s+)?units?|[A-Z]{3}\b|years?|months?|"
    r"weeks?|days?|per ?cent|%)?")
_DURATION = re.compile(r"\b(years?|months?|weeks?|days?)\b", re.I)
_RELATIVE = re.compile(r"\b(?:sub-?section|paragraph|sub-?paragraph)s?\s+\((?P<n>[0-9a-z]{1,4})\)", re.I)
_WHOLE = re.compile(r"\bth(?:is|ese) (?:Act|Regulations?|Rules?|Code|Law|Order|Directive|Standard|Ordinance|Decree)\b",
                    re.I)
_DIVISION = re.compile(r"\bthis (?P<division>Part|Subpart|Chapter|Schedule|Title)\b", re.I)
_DIVISION_SLUG = {"part": "part-", "subpart": "subpart-", "chapter": "ch-", "schedule": "sch-", "title": "title-"}


def _parse_amount(maximum: str) -> tuple[float | None, str]:
    """(amount, unit) of a stated maximum: "2,000 units" -> (2000.0, "units"), "two years" -> (None, "years")."""
    m = _AMOUNT.search(maximum)
    if m:
        num = float(re.sub(r"[,  ]", "", m.group("num")))
        scale = (m.group("scale") or "").lower()
        num *= {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "thousand": 1e3, "k": 1e3}.get(scale, 1)
        unit = (m.group("cur") or m.group("unit") or "").strip()
        unit = "units" if unit.lower().endswith(("unit", "units")) else unit
        return num, unit
    duration = _DURATION.search(maximum)
    return None, (duration.group(1).lower().rstrip("s") + "s") if duration else ""


def monetary(unit: str) -> bool:
    return unit == "units" or unit in ("$", "£", "€", "¥") or bool(re.fullmatch(r"[A-Z]{3}", unit or ""))


def severity(penalty: dict) -> float:
    """How severe a stated penalty is: its type, and its monetary maximum on a log scale."""
    amount = penalty.get("amount")
    bonus = 0.1 * math.log10(1 + amount) if amount and monetary(penalty.get("unit") or "") else 0.0
    return round(RANK.get(penalty["penalty_type"], 0.5) + bonus, 4)


def _quote(clause: dict, start: int, end: int) -> dict:
    return {"layer": "L1", "clause_id": clause["id"], "source_key": clause["source_key"], "clause_ref": clause["ref"],
            "start": start, "end": end, "quote": clause["text"][start:end]}


def read(clause: dict) -> list[dict]:
    """The penalties one clause states: type and maximum, each quoted."""
    text = clause["text"] or ""
    if not LIABILITY.search(text):
        return []
    mentions: list[tuple[int, int, str]] = []
    for kind, pattern in TYPES:
        for m in pattern.finditer(text):
            if not any(s < m.end() and m.start() < e for s, e, _ in mentions):
                mentions.append((m.start(), m.end(), kind))
    mentions.sort()
    out, seen = [], set()
    for i, (start, end, kind) in enumerate(mentions):
        if kind in seen:
            continue
        seen.add(kind)
        # The maximum is stated after the penalty, before the next one: "a fine not exceeding 2,000 units
        # or imprisonment for a term not exceeding two years".
        stop = mentions[i + 1][0] if i + 1 < len(mentions) else len(text)
        window = text[end:stop]
        found = _MAXIMUM.search(window)
        if found is None and re.search(r"maximum\s+(?:\w+\s+)?$", text[max(0, start - 30):start], re.I):
            found = _OF_MAXIMUM.search(window)  # "a maximum fine of 5,000 units"
        maximum = None
        if found is not None and found.group("max").strip():
            maximum = _quote(clause, end + found.start("max"), end + found.end("max"))
        amount, unit = _parse_amount(maximum["quote"]) if maximum else (None, "")
        out.append({"penalty_type": kind, "maximum": maximum["quote"] if maximum else "", "amount": amount,
                    "unit": unit, "evidence": {"penalty": _quote(clause, start, end), "maximum": maximum}})
    return out


def _clauses(conn: Connection, keys) -> dict[str, list[dict]]:
    from app.clhear.l1.references import in_force_clauses

    return {key: [{**c, "source_key": key, "text": c["text"] or ""} for c in rows]
            for key, rows in in_force_clauses(conn, keys).items()}


def _kinds(conn: Connection, keys) -> dict[str, str]:
    from app.clhear.l1.models import sources

    return dict(conn.execute(sa.select(sources.c.key, sources.c.kind).where(sources.c.key.in_(sorted(keys)))).all())


def _leaves(rows: list[dict]) -> list[dict]:
    """A parent clause repeats its children's text: keep the most specific ones."""
    return [c for c in rows if not any(o is not c and o["text"] and o["text"] in c["text"] for o in rows)]


def penalty_id(clause: dict, kind: str) -> str:
    digest = hashlib.sha1(f"{clause['source_key']}|{clause['ref']}|{kind}|{clause['text']}".encode()).hexdigest()[:12]
    return f"PEN-{digest}"


def find(conn: Connection, source_keys) -> list[dict]:
    """Every penalty the binding texts in scope state, not yet linked or stored."""
    keys = sorted(set(source_keys or []))
    kinds = _kinds(conn, keys)
    binding = [k for k in keys if kinds.get(k) in BINDING_KINDS]
    out = []
    for key, rows in _clauses(conn, binding).items():
        for clause in _leaves(rows):
            for penalty in read(clause):
                out.append({**penalty, "id": penalty_id(clause, penalty["penalty_type"]), "source_key": key,
                            "clause_ref": clause["ref"], "clause_id": clause["id"], "clause": clause})
    return out


def _obligations(conn: Connection, keys) -> dict[str, dict[str, list[str]]]:
    """source key -> clause ref -> the live obligations read from that clause."""
    from app.clhear.derived_models import obligations

    out: dict[str, dict[str, list[str]]] = {}
    for r in conn.execute(sa.select(obligations.c.id, obligations.c.source_key, obligations.c.clause_ref).where(
            obligations.c.source_key.in_(sorted(keys)), obligations.c.status.in_(("derived", "validated")))):
        out.setdefault(r.source_key, {}).setdefault(r.clause_ref, []).append(r.id)
    return out


def _links(conn: Connection, penalty: dict, clauses: dict[str, list[dict]], resolved: list[dict],
           obs: dict[str, dict[str, list[str]]]) -> dict[str, tuple[str, dict | None]]:
    """obligation id -> (method, the words in the penalty clause that refer to it)."""
    from app.clhear.l1.references import descendants

    clause = penalty["clause"]
    key = penalty["source_key"]
    own = clauses.get(key, [])
    found: dict[str, tuple[str, dict | None]] = {}

    def link(source_key: str, targets: list[dict], method: str, via: dict | None) -> None:
        for target in targets:
            for oid in obs.get(source_key, {}).get(target["ref"], []):
                found.setdefault(oid, (method, via))

    for entry in resolved:
        if entry["from"]["clause_id"] != clause["id"] or entry["status"] != "resolved" or not entry["target_ref"]:
            continue
        rows = clauses.get(entry["target_source"], [])
        target = next((c for c in rows if c["ref"] == entry["target_ref"]), None)
        if target is not None:
            link(entry["target_source"], descendants(rows, target), "reference", entry["from"])
    for m in _RELATIVE.finditer(clause["text"]):
        unit = clause["ref"].split("/", 1)[0]
        target = next((c for c in own if c["ref"] == f"{unit}/{m.group('n').lower()}"), None)
        if target is not None and target["id"] != clause["id"]:
            link(key, descendants(own, target), "relative", _quote(clause, m.start(), m.end()))
    for m in _DIVISION.finditer(clause["text"]):
        prefix = _DIVISION_SLUG[m.group("division").lower()]
        around = [c for c in own if c["ref"].startswith(prefix) and c.get("span_start") is not None
                  and clause.get("span_start") is not None
                  and c["span_start"] <= clause["span_start"] and clause["span_end"] <= c["span_end"]]
        if around:
            division = min(around, key=lambda c: c["span_end"] - c["span_start"])
            link(key, descendants(own, division), "division", _quote(clause, m.start(), m.end()))
    for m in _WHOLE.finditer(clause["text"]):
        link(key, own, "whole_text", _quote(clause, m.start(), m.end()))
    return found


def derive(engine: Engine, source_keys) -> dict:
    """Store the penalties the binding texts in scope state and link them to obligations.

    Unchanged penalties keep their rows; a penalty no longer stated, and a link no
    longer made, is closed (``valid_to``), never deleted."""
    from app.clhear.l1.references import resolve
    from app.clhear.l7.models import penalty_links, stated_penalties

    keys = sorted(set(source_keys or []))
    today = date.today()
    with engine.begin() as conn:
        penalties = find(conn, keys)
        clauses = _clauses(conn, keys)
        resolved = resolve(conn, keys)
        obs = _obligations(conn, keys)
        live = {r.id: r for r in conn.execute(sa.select(stated_penalties).where(
            stated_penalties.c.source_key.in_(keys), stated_penalties.c.valid_to.is_(None)))}
        current = {p["id"] for p in penalties}
        for pid in sorted(set(live) - current):
            conn.execute(stated_penalties.update().where(stated_penalties.c.id == pid,
                                                         stated_penalties.c.valid_to.is_(None)).values(valid_to=today))
            conn.execute(penalty_links.update().where(penalty_links.c.penalty_id == pid,
                                                      penalty_links.c.valid_to.is_(None)).values(valid_to=today))
        linked: set[str] = set()
        for p in penalties:
            if p["id"] not in live:
                version = conn.execute(sa.select(sa.func.max(stated_penalties.c.version))
                                       .where(stated_penalties.c.id == p["id"])).scalar() or 0
                conn.execute(stated_penalties.insert().values(
                    id=p["id"], version=version + 1, source_key=p["source_key"], clause_ref=p["clause_ref"],
                    clause_id=p["clause_id"], penalty_type=p["penalty_type"], maximum=p["maximum"],
                    amount=p["amount"], unit=p["unit"], evidence=p["evidence"], text_hash=p["clause"]["text_hash"],
                    valid_from=today, derived_by="l7.penalties"))
            wanted = _links(conn, p, clauses, resolved, obs)
            have = {r.obligation_id: r for r in conn.execute(sa.select(penalty_links).where(
                penalty_links.c.penalty_id == p["id"], penalty_links.c.valid_to.is_(None)))}
            for oid in sorted(set(have) - set(wanted)):
                conn.execute(penalty_links.update().where(penalty_links.c.id == have[oid].id).values(valid_to=today))
            for oid in sorted(set(wanted) - set(have)):
                method, via = wanted[oid]
                conn.execute(penalty_links.insert().values(penalty_id=p["id"], obligation_id=oid, method=method,
                                                           via=via, valid_from=today, derived_by="l7.penalties"))
            linked |= set(wanted)
    return {"found": len(penalties), "linked_obligations": len(linked), "ids": sorted(current)}


def stated_for(conn: Connection) -> dict[str, list[dict]]:
    """obligation id -> the live penalties stated for breaching it."""
    from app.clhear.l7.models import penalty_links, stated_penalties

    q = (sa.select(penalty_links.c.obligation_id, stated_penalties.c.id, stated_penalties.c.penalty_type,
                   stated_penalties.c.maximum, stated_penalties.c.amount, stated_penalties.c.unit)
         .join(stated_penalties, sa.and_(stated_penalties.c.id == penalty_links.c.penalty_id,
                                         stated_penalties.c.valid_to.is_(None)))
         .where(penalty_links.c.valid_to.is_(None)))
    out: dict[str, list[dict]] = {}
    for r in conn.execute(q).mappings():
        out.setdefault(r["obligation_id"], []).append(dict(r))
    return {oid: sorted(rows, key=lambda p: p["id"]) for oid, rows in out.items()}
