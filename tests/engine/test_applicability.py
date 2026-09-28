# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4: the questions a duty raises come from its own words; a profile answers them."""
from __future__ import annotations

import os

from .scripted_model import scripted_router

EU_TEXT = """Article 1
1. The controller shall keep a record of processing activities.
2. Every provider shall publish its terms on its website and keep them available online.
3. Where an organisation processes personal data on a large scale, it shall designate a data protection officer.
4. The notice shall include the identity of the controller.
"""
GLOBAL_TEXT = """Section 1. Every organisation shall appoint a person responsible for information security.
"""


def _build(install):
    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.runtime import engine

    hoststore.upsert_source(engine(), "eu-rule", {"adapter": "local_text", "jurisdiction": "EU",
                                                  "locator": {"text": EU_TEXT}})
    hoststore.upsert_source(engine(), "standard", {"adapter": "local_text", "locator": {"text": GLOBAL_TEXT}})
    scopes.put("mixed", ["eu-rule", "standard"])
    os.environ[scopes.SCOPE_ENV] = "mixed"
    try:
        scope_build.build(engine(), scripted_router(engine()), layers=("L1", "L2", "L3", "L4"))
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)


def _states(attributes):
    from app.clhear.l4.predicates import applicability
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        verdicts = applicability(conn, attributes, source_keys=["eu-rule", "standard"])
    return {v["obligation"]["clause_ref"]: v for v in verdicts.values()}


def test_a_source_without_jurisdiction_applies_everywhere(install):
    _build(install)
    us = _states({"jurisdictions": ["US"]})
    assert us["sec-1"]["state"] == "applies"
    assert us["art-1/2"]["state"] == "not_applicable"
    assert us["art-1/2"]["failed"][0]["predicate"] == {"jurisdictions": "EU"}


def test_incidental_words_do_not_narrow_a_duty(install):
    _build(install)
    # "website" / "online" describe the duty; they are not its condition, and "every provider" is a role.
    eu = _states({"jurisdictions": ["EU"], "roles": ["provider"]})
    assert eu["art-1/2"]["state"] == "applies"


def test_a_role_duty_is_undetermined_until_the_role_is_answered(install):
    _build(install)
    assert _states({"jurisdictions": ["EU"]})["art-1/1"]["state"] == "undetermined"
    assert _states({"jurisdictions": ["EU"], "roles": ["controller"]})["art-1/1"]["state"] == "applies"
    assert _states({"jurisdictions": ["EU"], "roles": {"controller": False}})["art-1/1"]["state"] == "not_applicable"


def test_a_condition_about_the_addressee_is_a_question_in_the_texts_words(install):
    from app.clhear.l4.predicates import questions
    from app.clhear.runtime import engine

    _build(install)
    with engine().connect() as conn:
        asked = questions(conn, ["eu-rule", "standard"])
    facts = {c["fact"]: c for c in asked["conditions"]}
    assert set(facts) == {"processes personal data on a large scale"}
    assert facts["processes personal data on a large scale"]["quotes"][0]["quote"] == (
        "Where an organisation processes personal data on a large scale")
    assert {r["role"] for r in asked["roles"]} == {"controller", "provider"}  # not "notice": it sets content
    assert _states({"jurisdictions": ["EU"]})["art-1/3"]["state"] == "undetermined"
    answered = _states({"jurisdictions": ["EU"], "conditions": {"processes personal data on a large scale": False}})
    assert answered["art-1/3"]["state"] == "not_applicable"
    assert _states({"jurisdictions": ["EU"], "conditions": {facts["processes personal data on a large scale"]["id"]: True}})[
        "art-1/3"]["state"] == "applies"


def test_profile_schema_lists_the_questions_a_scope_raises(install):
    from fastapi.testclient import TestClient

    from app.clhear.api import app

    _build(install)
    with TestClient(app) as client:
        body = client.get("/v1/profile-schema", params={"scope": "mixed"}).json()
        assert body["questions"]["jurisdictions"] == ["EU"]
        assert {r["role"] for r in body["questions"]["roles"]} == {"controller", "provider"}
        assert "apply everywhere" in {f["key"]: f for f in body["fields"]}["jurisdictions"]["effect"]
        stored = client.put("/v1/profiles/acme", json={"attributes": {"jurisdictions": ["EU", "CA"],
                                                                      "roles": ["controller"]}}).json()
        assert stored["validation"]["valid"] is True
        retired = client.put("/v1/profiles/old", json={"attributes": {"data_footprint": "customer data"}})
    assert retired.status_code == 422 and "profile-schema" in retired.json()["detail"]
