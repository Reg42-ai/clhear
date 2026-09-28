# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The grammar CLHEAR reads duties with, one rule at a time."""
from __future__ import annotations

import pytest

from app.clhear.l2.extract import detect_duty


@pytest.mark.parametrize("text", [
    "Where the term of the credit agreement exceeds one year, the creditor shall provide the consumer with a statement.",
    "A firm that makes references to its authorised status in a communication must state the name of its regulator.",
    "The finance department shall reconcile the client accounts every business day.",
    "The company secretary shall keep the register of members at the registered office.",
])
def test_these_are_duties(text):
    assert detect_duty(text, "", "")


@pytest.mark.parametrize("text", [
    "The term personal data shall include any information relating to an identified person.",
    "The Department shall publish guidance on the implementation of this rule.",
    "The Health Department shall publish guidance on the implementation of this rule.",
    "Each supervisory authority shall have all of the following investigative powers.",
    "The Commission, by order, shall censure any person who violates this section.",
])
def test_these_are_not_the_addressees_duties(text):
    assert detect_duty(text, "", "") is None


def test_an_aside_after_the_modal_is_not_the_action():
    from app.clhear.l2.registry import parse_structure

    found = parse_structure("The controller shall, to the extent possible, erase the personal data where the data "
                            "subject withdraws consent.")
    assert found["action"] == "erase the personal data"
    assert found["condition"] == "to the extent possible; where the data subject withdraws consent"


def _measure(text: str, modality: str = "shall"):
    from app.clhear.l2.registry import structured_fields
    from app.clhear.l3.decompose import propose_block

    fields = structured_fields(text, modality)
    clause = {"id": 1, "source_key": "s", "ref": "r", "text": text}
    return propose_block({**fields, "modality": modality, "evidence": {"duty": {"quote": text}}}, [clause])


@pytest.mark.parametrize("text, modality, kind, name", [
    ("The controller shall establish and maintain a register of processing activities.", "shall",
     "Document", "Register of processing activities"),
    ("An employer shall appoint a person responsible for safety and record the appointment.", "shall",
     "Role", "Person responsible for safety"),
    ("A firm shall notify the management body of any breach within one day.", "shall",
     "Process", "Notify the management body of any breach"),
    ("The provider must not disclose personal data to third parties.", "must-not",
     "Process", "Must not disclose personal data to third parties"),
])
def test_a_measure_is_named_in_the_duty_s_words(text, modality, kind, name):
    proposal = _measure(text, modality)
    assert (proposal["kind"], proposal["name"]) == (kind, name)
    for q in proposal["evidence"]["name"]:
        assert text[q["start"]:q["end"]] == q["quote"]


def test_a_quoted_word_is_a_whole_word():
    from app.clhear.evidence import locate

    text = "Personal data about a person."
    assert text[slice(*locate(text, "person"))] == "person" and locate(text, "person")[0] == 22


def test_singular_and_plural_are_the_same_word():
    from app.clhear.evidence import grounded

    assert grounded("Access review procedures", ["review the procedure for access"])
    assert grounded("processes", ["the process"]) and grounded("policy", ["the policies"])


def test_a_relative_clause_is_a_condition_not_more_roles():
    from app.clhear.l4.predicates import addressee_roles

    roles, conditions = addressee_roles({"subject": "firm that holds client money and deals on its own account",
                                         "action": "segregate it"})
    assert [r["role"] for r in roles] == ["firm"]
    assert conditions[0]["fact"] == "holds client money and deals on its own account"


def test_a_quote_narrows_across_a_line_break():
    from app.clhear.l4.predicates import _quote_in

    clause = "1. Every covered\nentity must keep records."
    quote = {"clause_id": 1, "start": 3, "end": 23, "quote": clause[3:23]}
    narrowed = _quote_in([quote], "covered entity")[0]
    assert clause[narrowed["start"]:narrowed["end"]] == narrowed["quote"] == "covered\nentity"


def test_an_answer_by_condition_id_is_not_warned_about():
    from app.clhear.l4.validate import check_answers

    asked = {"conditions": [{"id": "COND-341af61df3", "fact": "processes personal data"}], "roles": []}
    assert check_answers(asked, {"conditions": {"COND-341af61df3": True}}) == []
    assert check_answers(asked, {"conditions": {"processes personal data": False}}) == []


def test_a_duplicate_stays_when_its_canonical_is_in_another_scope():
    from app.clhear.l4.predicates import canonical_in

    rows = [{"id": "OBL:a#1", "stable_id": "OBL-000001", "canonical_id": None},
            {"id": "OBL:a#2", "stable_id": "OBL-000002", "canonical_id": "OBL-000001"},
            {"id": "OBL:a#3", "stable_id": "OBL-000003", "canonical_id": "OBL-000099"}]
    assert [r["id"] for r in canonical_in(rows)] == ["OBL:a#1", "OBL:a#3"]


def test_the_stored_blueprint_changes_when_its_questions_change():
    from app.clhear.l6.models import composition_hash

    base = {"items": [], "coverage": [], "undetermined": [], "open_questions": []}
    asked = {**base, "undetermined": [{"clause_ref": "sec-2"}], "open_questions": [{"ask": "Are you 'x'?"}]}
    assert composition_hash(base) != composition_hash(asked)
