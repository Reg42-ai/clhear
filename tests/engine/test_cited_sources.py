# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Cross-references: the texts in scope name the sources they depend on.

The texts are written for these tests; none is a real law. A clause that cites
another text ("section 2 of the Harbour Lighting Act 2019") or a provision the
scope does not hold ("Part 7") is an evidence gap that names the cited text as
the clause words it, and the blueprint's source inventory tells a missing source
apart from one that is in scope.
"""
from __future__ import annotations

import os

import sqlalchemy as sa

from .scripted_model import scripted_router
from .test_live_run import _assert_quotes_hold, _run, live  # noqa: F401  (fixture)

LIGHTING = """Harbour lighting rule

This text is written for CLHEAR's tests. It is not a law.

Rule 1. In this rule, "keeper" has the meaning given in section 2 of the Harbour Lighting Act 2019.

Rule 2. A keeper shall light the harbour lantern every evening at sunset.

Rule 3. A keeper shall record each lighting in the lantern log required by rule 2.

Rule 4. A keeper shall report a failed lantern to the harbour master as required under Part 7.
"""

ACT = """Lantern statute

This text is written for CLHEAR's tests. It is not a law.

Section 1. This Act applies to every harbour with a public lantern.

Section 2. In this Act, "keeper" means a person appointed to light a harbour lantern.
"""


def test_the_grammar_finds_references_in_any_text():
    from app.clhear.l1.references import extract

    def found(text):
        mentions = extract(text)
        assert all(text[m["start"]:m["end"]] == m["quote"] for m in mentions)  # quotes hold at their offsets
        return [(m["path_ref"], m["instrument"]) for m in mentions]

    assert found("as defined in section 3(1)(a) of the Harbour Lighting Act 2019") == [
        ("sec-3/1/a", "the Harbour Lighting Act 2019")]
    assert found("A keeper shall comply with the duties under Part 7.") == [("part-7", "")]
    assert found("Records are kept pursuant to Regulation (AB) 2030/17.") == [("", "Regulation (AB) 2030/17")]
    assert found("Under the Harbour Code, a keeper shall register.") == [("", "the Harbour Code")]
    assert found("Lantern Standard, article 4 applies.") == [("art-4", "Lantern Standard")]
    assert found("sections 3 and 4 and Article II of the Lamp Rules") == [
        ("sec-3", ""), ("sec-4", ""), ("art-ii", "the Lamp Rules")]
    # Not references: a provision's own label, the text itself, a defined term, relative
    # provisions, the name a text gives itself, a title line, and words that only look like numerals.
    assert found("Section 4. A keeper who fails to comply with this rule commits an offence.") == []
    assert found("This Act applies. The Act applies. Subject to subsection (2), the keeper acts.") == []
    assert found("This rule may be cited as the Harbour Lighting Rule 2020.") == []
    assert found("Harbour Lighting Act 2019") == []
    assert found("a regulation civil in tone, part mild, rule did") == []


def test_a_provision_written_inline_holds_the_paragraphs_after_it():
    from app.clhear.l1.references import descendants

    clauses = [{"id": i, "ref": ref, "ordering": i, "span_start": None, "span_end": None}
               for i, ref in enumerate(["sec-3", "1", "2", "sec-4", "1-2"])]
    assert [c["ref"] for c in descendants(clauses, clauses[0])] == ["sec-3", "1", "2"]


def test_a_reference_is_stored_once_on_the_clause_that_makes_it(install):
    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.l1.models import clause_references, clauses
    from app.clhear.runtime import engine

    text = ("Lantern rules\n\nArticle 5\nDuties of keepers\n1. A keeper shall keep the log described in "
            "Article 9 of the Lamp Rules.\n2. A keeper shall light the lantern at sunset.\n")
    hoststore.upsert_source(engine(), "lamps", {"adapter": "local_text", "kind": "regulation",
                                                "locator": {"text": text}})
    scopes.put("lamps-scope", ["lamps"])
    os.environ[scopes.SCOPE_ENV] = "lamps-scope"
    try:
        scope_build.build(engine(), scripted_router(engine()), layers=("L1",))
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)
    with engine().connect() as conn:
        rows = conn.execute(sa.select(clause_references)).mappings().all()
        texts = dict(conn.execute(sa.select(clauses.c.id, clauses.c.text)).all())
    assert [(r["clause_ref"], r["path_ref"], r["cited_instrument"]) for r in rows] == [
        ("art-5/1", "art-9", "the Lamp Rules")]  # not again on art-5, whose text repeats it
    row = rows[0]
    assert texts[row["from_clause_id"]][row["start_offset"]:row["end_offset"]] == row["quote"]


def test_the_migration_records_the_references_of_texts_already_stored(install):
    import importlib

    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.l1.models import clause_references
    from app.clhear.runtime import engine

    hoststore.upsert_source(engine(), "lighting", {"adapter": "local_text", "kind": "regulation",
                                                   "locator": {"text": LIGHTING}})
    scopes.put("harbour", ["lighting"])
    os.environ[scopes.SCOPE_ENV] = "harbour"
    try:
        scope_build.build(engine(), scripted_router(engine()), layers=("L1",))
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)
    with engine().begin() as conn:
        stored = conn.execute(sa.select(clause_references.c.path_ref)).scalars().all()
        conn.execute(clause_references.delete())  # as a database from before 0043 holds them
        importlib.import_module("migrations.m0043_clause_references").upgrade(conn)
        assert sorted(conn.execute(sa.select(clause_references.c.path_ref)).scalars().all()) == sorted(stored)
    assert sorted(stored) == ["part-7", "rule-2", "sec-2"]


def _quotes_hold(quotes):
    from app.clhear.evidence import check, clause_rows
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        held = clause_rows(conn, [q["clause_id"] for q in quotes])
    assert quotes and all(check(q, held) is None for q in quotes)


def test_a_cited_text_missing_from_the_scope_is_named_and_listed(live):  # noqa: F811
    from app.clhear.first_run import blueprint_text

    client, _ = live
    client.post("/v1/sources", json={"key": "lighting", "adapter": "local_text", "kind": "regulation",
                                     "name": "Harbour lighting rule", "locator": {"text": LIGHTING}})
    client.post("/v1/scopes", json={"name": "harbour", "sources": ["lighting"]})
    client.put("/v1/profiles/keeper", json={"attributes": {"roles": ["keeper"]}})
    release = _run(client, "harbour", ["keeper"])
    blueprint = client.get(f"/v1/releases/{release}/blueprints/keeper").json()
    _assert_quotes_hold(blueprint)

    gaps = {g["subject"]: g for g in blueprint["evidence_gaps"] if g["kind"] == "unresolved_reference"}
    assert set(gaps) == {"ref:harbour lighting act", "ref:lighting:part-7"}  # "rule 2" resolves in the rule itself
    act = next(a for a in blueprint["source_advice"] if a.get("cited") == "the Harbour Lighting Act 2019")
    assert act["layer"] == "L1" and act["gap"] == "unresolved_reference"
    assert act["add"][0]["source"] == "the Harbour Lighting Act 2019" and act["add"][0]["register_as"] == "law"
    assert [(q["source_key"], q["clause_ref"]) for q in act["add"][0]["cited_by"]] == [("lighting", "rule-1")]
    _quotes_hold(act["add"][0]["cited_by"])
    part = next(a for a in blueprint["source_advice"] if a.get("cited") == "Part 7")
    assert part["add"][0]["register_as"] == "regulation"  # a bare provision: the kind of the text citing it

    inventory = {(e["status"], e.get("source_key"), e.get("cited_as")) for e in blueprint["source_inventory"]}
    assert inventory == {("derived", "lighting", None), ("unresolved", None, "the Harbour Lighting Act 2019"),
                         ("unresolved", None, "Part 7")}
    shown = blueprint_text(blueprint)
    assert "unresolved" in shown and '"the Harbour Lighting Act 2019" cited by lighting rule-1' in shown

    # Registered under its publisher's reference, but not yet in the scope: pending, and the advice says so.
    client.post("/v1/sources", json={"key": "act", "adapter": "local_text", "kind": "law", "name": "Lantern statute",
                                     "reference": "Harbour Lighting Act 2019", "locator": {"text": ACT}})
    advice = client.get("/v1/scopes/harbour/advice").json()
    pending = next(e for e in advice["source_inventory"] if e.get("cited_as") == "the Harbour Lighting Act 2019")
    assert pending["status"] == "pending" and pending["source_key"] == "act"

    # In scope: the reference resolves, the gap is gone, and the act is derived and cited by the rule.
    client.post("/v1/scopes", json={"name": "harbour", "sources": ["lighting", "act"]})
    release = _run(client, "harbour", ["keeper"])
    blueprint = client.get(f"/v1/releases/{release}/blueprints/keeper").json()
    subjects = {g["subject"] for g in blueprint["evidence_gaps"] if g["kind"] == "unresolved_reference"}
    assert subjects == {"ref:lighting:part-7"}
    derived = {e["source_key"]: e for e in blueprint["source_inventory"] if e["status"] == "derived"}
    assert set(derived) == {"act", "lighting"}
    assert derived["act"]["reference"] == "Harbour Lighting Act 2019"
    assert [(q["source_key"], q["clause_ref"]) for q in derived["act"]["cited_by"]] == [("lighting", "rule-1")]
    lineage = client.get(f"/v1/releases/{release}").json()["lineage"]
    assert lineage["unanchored"] == [] and lineage["rows"] == lineage["anchored"] > 0
