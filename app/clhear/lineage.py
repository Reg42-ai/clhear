# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Lineage: every derived row of a scope, checked back to the clause text.

After L5, a build walks every live row it derived for the scope's sources and
checks each quote it carries against the clause it names (same text at the
same offsets, clause still in force):

* obligations (their assert edge and every quoted field),
* requires edges and the measures (blocks) they point at,
* characteristics, applicability edges, activities and operates edges,
* licence types quoted from the scope's licensing clauses.

A row that fails is *withheld*: the blueprint neither shows it nor counts on
it, and the release lists it under ``lineage.unanchored`` with the reason.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear import evidence as ev
from app.clhear.derived_models import (
    activities,
    applies_to,
    asserts,
    blocks,
    characteristics,
    license_types,
    obligations,
    operates,
    requires,
)

LIVE = ("derived", "validated")


def _json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _quotes(found) -> list[dict]:
    """Every quote inside an evidence value (dicts and lists nest)."""
    if ev.is_quote(found):
        return [found]
    if isinstance(found, dict):
        return [q for v in found.values() for q in _quotes(v)]
    if isinstance(found, list):
        return [q for v in found for q in _quotes(v)]
    return []


class _Checker:
    def __init__(self, conn):
        self.conn = conn
        self.clauses: dict[int, dict] = {}
        self.rows = 0
        self.unanchored: list[dict] = []

    def load(self, quotes: list[dict]) -> None:
        missing = {q.get("clause_id") for q in quotes} - set(self.clauses)
        self.clauses.update(ev.clause_rows(self.conn, [i for i in missing if isinstance(i, int)]))

    def quotes_hold(self, quotes: list[dict]) -> str | None:
        self.load(quotes)
        for q in quotes:
            reason = ev.check(q, self.clauses)
            if reason:
                return f"{q.get('source_key')} {q.get('clause_ref')}: {reason}"
        return None

    def judge(self, table: str, row_id, reason: str | None) -> bool:
        self.rows += 1
        if reason:
            self.unanchored.append({"table": table, "id": row_id, "reason": reason})
            return False
        return True


def verify(engine: Engine, source_keys) -> dict:
    keys = sorted(source_keys or [])
    withheld: set[str] = set()
    with engine.connect() as conn:
        c = _Checker(conn)
        obs = [dict(r) for r in conn.execute(sa.select(obligations).where(obligations.c.source_key.in_(keys))
                                             .where(obligations.c.status.in_(LIVE))).mappings()]
        ob_ids = {o["id"] for o in obs}
        refs = {o.get("stable_id") or o["id"]: o["id"] for o in obs} | {o["id"]: o["id"] for o in obs}
        live_asserts = {}
        for a in conn.execute(sa.select(asserts).where(asserts.c.obligation_id.in_(ob_ids))
                              .where(asserts.c.valid_to.is_(None))).mappings():
            live_asserts.setdefault(a["obligation_id"], []).append(dict(a))
        for o in obs:
            found = _json(o.get("evidence"), {})
            reason = None
            edges = live_asserts.get(o["id"]) or []
            if not edges:
                reason = "no live assert edge to a clause"
            elif not found or not found.get("duty"):
                reason = "no quoted duty"
            else:
                c.load([{"clause_id": e["clause_id"]} for e in edges])
                for e in edges:
                    clause = c.clauses.get(e["clause_id"])
                    if clause is None or not clause["in_force"]:
                        reason = f"asserted clause {e['clause_ref']} is not in force"
                    elif clause["text_hash"] != e["text_hash"]:
                        reason = f"asserted clause {e['clause_ref']} changed since derivation"
                reason = reason or c.quotes_hold(_quotes(found))
            if not c.judge("obligations", o["id"], reason):
                withheld.add(o["id"])

        texts_of: dict[str, list[str]] = {}
        block_ids: set[str] = set()
        for r in conn.execute(sa.select(requires).where(requires.c.obligation_id.in_(ob_ids))
                              .where(requires.c.valid_to.is_(None))).mappings():
            found = _json(r.get("evidence"), {})
            quotes = _quotes(found)
            reason = "no quoted duty" if not quotes else c.quotes_hold(quotes)
            if not c.judge("requires", r["id"], reason):
                withheld.add(f"requires:{r['id']}")  # this link only: other duties may rightly need the block
            block_ids.add(r["block_id"])
        for b in conn.execute(sa.select(blocks).where(blocks.c.id.in_(block_ids))).mappings():
            found = _json(b.get("evidence"), {})
            reason = None
            if not found:
                reason = "no evidence for the measure's name"
            elif found.get("name"):
                reason = c.quotes_hold(_quotes(found))
            elif found.get("name_words_in"):
                reason = c.quotes_hold(_quotes(found))
                texts = [q["quote"] for q in found["name_words_in"]]
                texts_of[b["id"]] = texts
                missing = ev.missing_words(b["name"], texts)
                if not reason and missing:
                    reason = f"name words not in the cited clauses: {', '.join(missing)}"
            else:
                reason = "no evidence for the measure's name"
            if not reason and b["kind"] not in ("Unspecified",) and not found.get("kind"):
                reason = f"kind {b['kind']} is not quoted"
            if not c.judge("blocks", b["id"], reason):
                withheld.add(b["id"])
        for ch in conn.execute(sa.select(characteristics).where(characteristics.c.block_id.in_(block_ids))
                               .where(characteristics.c.valid_to.is_(None))).mappings():
            quotes = _quotes(_json(ch.get("evidence"), {}))
            reason = "value is not quoted" if not quotes else c.quotes_hold(quotes)
            if not c.judge("characteristics", ch["id"], reason):
                withheld.add(f"characteristic:{ch['id']}")

        from app.clhear.l1.models import sources

        declared = {r.key: (r.jurisdiction or "").strip().upper()
                    for r in conn.execute(sa.select(sources.c.key, sources.c.jurisdiction).where(sources.c.key.in_(keys)))}
        by_ob = {o["id"]: o for o in obs}
        for e in conn.execute(sa.select(applies_to).where(applies_to.c.obligation_id.in_(ob_ids))
                              .where(applies_to.c.valid_to.is_(None))).mappings():
            found = _json(e.get("evidence"), {})
            pred = _json(e.get("predicate"), {})
            if "jurisdictions" in pred:
                source = (found or {}).get("source") or {}
                owner = by_ob[e["obligation_id"]]["source_key"]
                reason = None if source.get("value") == declared.get(owner) == pred["jurisdictions"] else \
                    "jurisdiction differs from the source's declaration"
            else:
                quotes = _quotes(found)
                reason = "not quoted" if not quotes else c.quotes_hold(quotes)
            if not c.judge("applies_to", e["id"], reason):
                withheld.add(e["obligation_id"])

        acts = [dict(a) for a in conn.execute(sa.select(activities).where(activities.c.valid_to.is_(None))).mappings()]
        mine = set()
        for a in acts:
            triggers = _json(a.get("triggers"), [])
            if not any(refs.get(t.get("obligation")) in ob_ids for t in triggers):
                continue
            mine.add(a["id"])
            found = _json(a.get("evidence"), {})
            reason = "not quoted" if not (found or {}).get("name") else c.quotes_hold(_quotes(found))
            if not reason and a.get("business_owner") and not found.get("operator"):
                reason = "operator is not quoted"
            if not c.judge("activities", a["id"], reason):
                withheld.add(a["id"])
        for o in conn.execute(sa.select(operates).where(operates.c.activity_id.in_(mine))
                              .where(operates.c.valid_to.is_(None))).mappings():
            quotes = _quotes(_json(o.get("evidence"), {}))
            reason = "not quoted" if not quotes else c.quotes_hold(quotes)
            if not c.judge("operates", o["id"], reason):
                withheld.add(o["activity_id"])

        for lic in conn.execute(sa.select(license_types).where(license_types.c.status != "retired")).mappings():
            anchors = _json(lic.get("clause_anchors"), [])
            if not any(isinstance(x, dict) and x.get("source_key") in keys for x in anchors):
                continue
            found = _json(lic.get("evidence"), {})
            quotes = _quotes(found)
            reason = "not quoted" if not quotes else c.quotes_hold(quotes)
            if not reason and ev.missing_words(lic["name"], [q["quote"] for q in quotes]):
                reason = "name words not in the cited clause"
            c.judge("license_types", lic["id"], reason)
    return {"checked": True, "rows": c.rows, "anchored": c.rows - len(c.unanchored),
            "unanchored": c.unanchored[:200], "withheld": sorted(withheld)}
