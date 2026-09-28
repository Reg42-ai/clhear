# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4: one visible rule decides which duties apply to an organisation."""
from __future__ import annotations

import os

from .scripted_model import scripted_router

EU_TEXT = """Article 1
1. The controller shall keep a record of processing activities.
2. Every provider shall publish its terms on its website and keep them available online.
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


def _applies(attributes):
    from app.clhear.l4.predicates import applicability
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        verdicts = applicability(conn, attributes, source_keys=["eu-rule", "standard"])
    return {v["obligation"]["clause_ref"]: v for v in verdicts.values()}


def test_a_source_without_jurisdiction_applies_everywhere(install):
    _build(install)
    us = _applies({"jurisdictions": ["US"]})
    assert us["sec-1"]["applies"] is True
    assert us["art-1/2"]["applies"] is False
    assert us["art-1/2"]["failed"][0]["predicate"] == {"jurisdictions": "EU"}


def test_incidental_words_do_not_narrow_a_duty(install):
    _build(install)
    eu = _applies({"jurisdictions": ["EU"]})
    # "website" / "online" describe the duty; they are not its condition.
    assert eu["art-1/2"]["applies"] is True


def test_a_controller_duty_needs_a_data_footprint(install):
    _build(install)
    assert _applies({"jurisdictions": ["EU"]})["art-1/1"]["applies"] is False
    assert _applies({"jurisdictions": ["EU"], "data_footprint": "customer records"})["art-1/1"]["applies"] is True


def test_profile_schema_explains_each_fact_and_lists_known_jurisdictions(install):
    from fastapi.testclient import TestClient

    from app.clhear.api import app

    _build(install)
    with TestClient(app) as client:
        fields = {f["key"]: f for f in client.get("/v1/profile-schema").json()["fields"]}
        assert fields["jurisdictions"]["known_values"] == ["EU"]
        assert "apply everywhere" in fields["jurisdictions"]["effect"]
        stored = client.put("/v1/profiles/acme", json={"attributes": {"jurisdictions": ["EU", "CA"]}}).json()
    assert stored["validation"]["valid"] is True
    assert stored["validation"]["warnings"][0]["value"] == "CA"
