# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Who a duty binds: a party (an organisation, or a person or function in one), never a thing
it is about, a heading or an introductory phrase; and the questions an organisation can answer."""
from __future__ import annotations

import os

import pytest


@pytest.mark.parametrize("subject, cleaned, conditions", [
    ("General Requirements Each member", "Each member", []),
    ("Disclosure of Member's Name All retail communications and correspondence",
     "All retail communications and correspondence", []),
    ("Notwithstanding any other provision of this Rule, an appropriately qualified principal",
     "an appropriately qualified principal", []),
    ("In particular, native advertising", "native advertising", []),
    ("For a period of one year beginning on the date that membership became effective, the member", "the member",
     ["For a period of one year beginning on the date that membership became effective"]),
    ("When sponsoring a seminar, forum, radio or television interview, persons associated with members",
     "persons associated with members", ["When sponsoring a seminar, forum, radio or television interview"]),
    ("Where a firm, acting as agent, holds money, it", "it", ["Where a firm, acting as agent, holds money"]),
    ("Paragraph (f)(2) does not apply to a recommendation; provided, however, that the associated person",
     "the associated person", []),
    ("7 Hyperlinks to Third-party Sites Regulatory Notice 11-39 states that a firm", "a firm", []),
    ("Any scripts or slides used in public appearances are considered communications, and members", "members", []),
    ("If a member has filed a draft version, then the member also", "the member", ["If a member has filed a draft version"]),
    ("Member States", "Member States", []),
    ("The controller", "The controller", []),
])
def test_what_precedes_the_subject_is_not_the_subject(subject, cleaned, conditions):
    from app.clhear.l2.registry import clean_subject

    assert clean_subject(subject) == (cleaned, conditions)


@pytest.mark.parametrize("phrase, party", [
    ("member", "organisation"), ("covered entity", "organisation"), ("Firms registered under the Act", "organisation"),
    ("Supervisory Analyst", "person"), ("appropriately qualified registered principal of the member", "person"),
    ("associated person", "person"), ("person making the testimonial", "universal"), ("any person", "universal"),
    ("Communications", "thing"), ("illustration", "thing"), ("Tax rates used in an illustration", "thing"),
    ("native advertising", "thing"), ("requirements of paragraph (b)(1)(A)", "thing"),
])
def test_a_party_or_a_thing(phrase, party):
    from app.clhear.l2.registry import party_of

    assert party_of(phrase) == party


def test_a_thing_is_not_asked_about_and_its_relative_clause_is_no_question():
    from app.clhear.l4.predicates import addressee_roles

    assert addressee_roles({"subject": "Retail communications that include a recommendation", "action": "disclose x"}) == ([], [])
    assert addressee_roles({"subject": "illustration", "action": "use actual federal income tax rates"}) == ([], [])


def test_a_person_is_a_role_the_organisation_has():
    from app.clhear.l4.predicates import addressee_roles, role_question

    roles, conditions = addressee_roles({"subject": "Supervisory Analyst", "action": "approve a retail communication"})
    assert [(r["role"], r["label"], r["party"]) for r in roles] == [("supervisory analyst", "Supervisory Analyst", "person")]
    assert conditions == []
    assert role_question(["Supervisory Analyst"], ["person"]) == "Does your organisation have a 'Supervisory Analyst'?"
    assert role_question(["member"], ["organisation"]) == "Are you a 'member'?"


def test_an_organisation_s_participle_narrows_who_is_bound():
    from app.clhear.l4.predicates import addressee_roles, condition_question

    roles, conditions = addressee_roles({"subject": "Firms registered under the Investment Advisers Act",
                                         "action": "be aware that ..."[3:]})
    assert [r["role"] for r in roles] == ["firm"]
    assert conditions[0]["fact"] == "registered under the Investment Advisers Act"
    assert condition_question(conditions[0]["fact"]) == "Is your organisation registered under the Investment Advisers Act?"


def test_a_condition_about_a_thing_or_a_person_times_the_duty():
    from app.clhear.l4.predicates import conditions_of

    asked, triggers = conditions_of({"subject": "illustration", "action": "reflect the impact of taxes",
                                     "condition": "If the illustration covers the payout period for an investment"})
    assert asked == [] and len(triggers) == 1
    asked, triggers = conditions_of({"subject": "associated person", "action": "have a reasonable basis",
                                     "condition": "If an associated person recommends a security in a public appearance"})
    assert asked == [] and len(triggers) == 1


@pytest.mark.parametrize("fact, ask", [
    ("processes personal data on a large scale", "Does your organisation process personal data on a large scale?"),
    ("holds client money and deals on its own account",
     "Does your organisation hold client money and deal on its own account?"),
    ("intends to communicate through a chat service", "Does your organisation intend to communicate through a chat service?"),
    ("has filed a draft version", "Has your organisation filed a draft version?"),
    ("has more than 250 employees", "Does your organisation have more than 250 employees?"),
])
def test_a_condition_is_asked_as_a_sentence(fact, ask):
    from app.clhear.l4.predicates import condition_question

    assert condition_question(fact) == ask


def test_variants_of_one_role_are_one_question():
    from app.clhear.l4.predicates import grounded_predicates, party_aliases

    rows = [{"subject": "appropriately qualified registered principal of the member", "action": "approve each communication",
             "source_key": "s"},
            {"subject": "appropriately qualified principal", "action": "approve a communication", "source_key": "s"}]
    aliases = party_aliases(rows)
    roles = [e["predicate"]["roles"] for ob in rows for e in grounded_predicates(ob, "", aliases)]
    assert roles == [["appropriately qualified principal"], ["appropriately qualified principal"]]


def _measure(text: str, modality: str = "shall"):
    from app.clhear.l2.registry import structured_fields
    from app.clhear.l3.decompose import propose_block

    fields = structured_fields(text, modality)
    clause = {"id": 1, "source_key": "s", "ref": "r", "text": text}
    return propose_block({**fields, "modality": modality, "evidence": {"duty": {"quote": text}}}, [clause])


@pytest.mark.parametrize("text, kind, name", [
    ("The supervisory procedures required by this paragraph shall include procedures for the review of correspondence.",
     "Document", "Supervisory procedures"),
    ("The supervisory procedures shall set forth the supervisory system established by the member.",
     "Document", "Supervisory procedures"),
    ("Members must ensure that statements are clear and not misleading.", "Process",
     "Ensure that statements are clear and not misleading"),
])
def test_what_a_content_duty_and_an_ensure_duty_name(text, kind, name):
    proposal = _measure(text)
    assert (proposal["kind"], proposal["name"]) == (kind, name)


def test_no_advice_prints_an_empty_placeholder():
    from app.clhear.advisor import summarise

    gaps = [{"kind": "role_undefined", "subject": f"role:r{i}", "missing": f"a definition of 'r{i}'", "detail": {}}
            for i in range(3)]
    gaps += [{"kind": "characteristic_unspecified", "subject": f"BLK-{i}:cadence", "missing": "cadence: How often",
              "detail": {"key": "cadence", "block": f"Block {i}"}} for i in range(2)]
    for advice in summarise(gaps):
        text = str(advice)
        assert "()" not in text and "''" not in text, text
    names = {a["gap"]: a["missing"] for a in summarise(gaps)}
    assert "'r0', 'r1', 'r2'" in names["role_undefined"]
    assert "cadence of 'Block 0'" in names["characteristic_unspecified"]


RULE_TEXT = """(a) Approval
(1) An appropriately qualified registered principal of the member must approve each retail communication before the earlier of its use or filing.
(2) Members must maintain all retail communications for the retention period required by SEA Rule 17a-4(b).
(3) Communications may not predict or project performance.
(4) The supervisory procedures required by this paragraph shall include procedures for the review of correspondence.
"""


def test_a_person_a_duty_is_addressed_to_is_a_role_component(install):
    import sqlalchemy as sa

    from app.clhear import hoststore, scope_build
    from app.clhear.derived_models import blocks
    from app.clhear.l1 import scopes
    from app.clhear.l4.predicates import applicability, questions
    from app.clhear.runtime import engine

    from .scripted_model import scripted_router

    hoststore.upsert_source(engine(), "rule", {"adapter": "local_text", "jurisdiction": "US",
                                               "locator": {"text": RULE_TEXT}})
    scopes.put("comms", ["rule"])
    os.environ[scopes.SCOPE_ENV] = "comms"
    try:
        scope_build.build(engine(), scripted_router(engine()), layers=("L1", "L2", "L3", "L4"))
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)
    with engine().connect() as conn:
        asked = questions(conn, ["rule"])
        roles = {r["role"]: r for r in asked["roles"]}
        kinds = {r.name: r.kind for r in conn.execute(sa.select(blocks.c.name, blocks.c.kind))}
        verdicts = applicability(conn, {"jurisdictions": ["US"], "roles": {"member": True}}, source_keys=["rule"])
    assert set(roles) == {"member", "appropriately qualified registered principal"}
    assert roles["appropriately qualified registered principal"]["party"] == "person"
    assert roles["appropriately qualified registered principal"]["ask"].startswith("Does your organisation have")
    assert kinds.get("Appropriately qualified registered principal") == "Role"
    states = {v["obligation"]["subject"]: v["state"] for v in verdicts.values()}
    assert states["Communications"] == "applies"  # a thing raises no question
