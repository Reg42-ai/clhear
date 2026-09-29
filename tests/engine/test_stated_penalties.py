# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Stated penalties: L7 has a risk input even without an enforcement source.

The rule below is written for these tests; it is not a law. Its penalty clauses
state a type and a maximum; each is quoted, linked to the obligations whose
provisions it refers to, and scored as the dimension ``stated_penalty``.
Enforcement events stay a separate, stronger input.
"""
from __future__ import annotations

import sqlalchemy as sa

from .test_live_run import _run, live  # noqa: F401  (fixture)

RULE = """Harbour lantern rule

This text is written for CLHEAR's tests. It is not a law.

Section 1. A keeper shall light the harbour lantern every evening at sunset.

Section 2. A keeper shall record each lighting in a lantern log.

Section 3. A keeper who fails to comply with section 1 commits an offence and is liable to a fine not exceeding 2,000 units.

Section 4. The harbour master may suspend the licence of a keeper who breaches this rule for a period of up to six months.

Section 5. A keeper shall:
(a) keep spare wicks in the lantern room; and
(b) keep the lantern room locked.

Section 6. A keeper who fails to comply with section 5 is liable to a fine not exceeding 500 units or to imprisonment for a term not exceeding one year.
"""


def test_the_grammar_reads_type_and_maximum():
    from app.clhear.l7.penalties import read

    def stated(text):
        found = read({"id": 1, "ref": "x", "source_key": "k", "text": text})
        for p in found:  # every quote is the clause's own words at its offsets
            for q in filter(None, p["evidence"].values()):
                assert text[q["start"]:q["end"]] == q["quote"]
        return [(p["penalty_type"], p["maximum"], p["amount"], p["unit"]) for p in found]

    assert stated("A person who breaches regulation 4 is liable to a maximum fine of EUR 50,000.") == [
        ("fine", "EUR 50,000", 50000.0, "EUR")]
    assert stated("A person who breaches this Act is liable to a penalty of up to $1.5 million.") == [
        ("penalty", "$1.5 million", 1500000.0, "$")]
    assert stated("is liable to a fine not exceeding 500 units or to imprisonment for a term not exceeding one year") == [
        ("fine", "500 units", 500.0, "units"), ("imprisonment", "one year", None, "years")]
    assert stated("A keeper shall keep the lantern glass free of fine dust.") == []  # no consequence is stated


def test_penalties_give_l7_a_risk_input_without_enforcement(live):  # noqa: F811
    from app.clhear.evidence import check, clause_rows
    from app.clhear.l7.models import penalty_links, risk_scores, stated_penalties
    from app.clhear.runtime import engine

    client, _ = live
    client.post("/v1/sources", json={"key": "lantern", "adapter": "local_text", "kind": "regulation",
                                     "issuer": "Example Harbour Authority", "locator": {"text": RULE}})
    client.post("/v1/scopes", json={"name": "harbour", "sources": ["lantern"]})
    client.put("/v1/profiles/keeper", json={"attributes": {"roles": ["keeper"]}})
    release = _run(client, "harbour", ["keeper"])

    released = client.get(f"/v1/releases/{release}").json()
    assert released["layers"]["L7"].get("built", True) is True  # built from the stated penalties alone
    with engine().connect() as conn:
        stated = {(r.clause_ref, r.penalty_type): r for r in conn.execute(
            sa.select(stated_penalties).where(stated_penalties.c.valid_to.is_(None)))}
        links = {(r.penalty_id, r.obligation_id, r.method) for r in conn.execute(
            sa.select(penalty_links).where(penalty_links.c.valid_to.is_(None)))}
        quotes = [q for r in stated.values() for q in r.evidence.values() if q]
        held = clause_rows(conn, [q["clause_id"] for q in quotes])
        scores = {r.subject_ref: r for r in conn.execute(sa.select(risk_scores).where(
            risk_scores.c.subject_kind == "obligation", risk_scores.c.status == "current"))}
    assert {k: (r.maximum, r.amount, r.unit) for k, r in stated.items()} == {
        ("sec-3", "fine"): ("2,000 units", 2000.0, "units"),
        ("sec-4", "suspension"): ("six months", None, "months"),
        ("sec-6", "fine"): ("500 units", 500.0, "units"),
        ("sec-6", "imprisonment"): ("one year", None, "years")}
    assert all(check(q, held) is None for q in quotes)

    def linked(ref, kind):
        pid = stated[(ref, kind)].id
        return {(oid.split("#")[1], method) for p, oid, method in links if p == pid}

    assert linked("sec-3", "fine") == {("sec-1", "reference")}  # "section 1"
    assert linked("sec-6", "fine") == {("sec-5/a", "reference"), ("sec-5/b", "reference")}  # inside section 5
    assert linked("sec-4", "suspension") == {("sec-1", "whole_text"), ("sec-2", "whole_text"),
                                             ("sec-5/a", "whole_text"), ("sec-5/b", "whole_text")}  # "this rule"

    stated_dim = {oid.split("#")[1]: s.dimensions["stated_penalty"] for oid, s in scores.items()}
    assert stated_dim["sec-5/a"] == 1.0  # imprisonment is the most severe penalty stated
    assert stated_dim["sec-1"] > stated_dim["sec-2"] > 0  # a fine and a suspension, against a suspension alone
    assert all(s.method_version == "risk-v3" and s.evidence["event_count"] == 0 for s in scores.values())
    assert scores["OBL:lantern#sec-1"].evidence["stated_penalties"]

    blueprint = client.get(f"/v1/releases/{release}/blueprints/keeper").json()
    gap = next(g for g in blueprint["evidence_gaps"] if g["kind"] == "no_enforcement_sources")
    assert "4 penalties the texts in scope state" in gap["missing"]  # enforcement is still the stronger input
    advice = next(a for a in blueprint["source_advice"] if a["gap"] == "no_enforcement_sources")
    provisions = [item for item in advice["add"] if "penalty provisions" in item["source"]]
    assert provisions and provisions[0]["register_as"] == "law"
    assert provisions[0]["published_by"] == "Example Harbour Authority"
    lineage = released["lineage"]
    assert lineage["unanchored"] == [] and lineage["rows"] == lineage["anchored"] > 0
