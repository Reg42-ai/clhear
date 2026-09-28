# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 finds the duties a text imposes; L3 gives every duty a measure."""
from __future__ import annotations

import os

import sqlalchemy as sa

from app.clhear.l2.extract import detect_duty

from .scripted_model import scripted_router

PRIVACY = """Data protection rules

Article 4
Definitions
1. The term personal data shall include any information relating to an identified or identifiable person.

Article 5
Principles relating to processing of personal data
1. Personal data shall be:
(a) processed lawfully, fairly and in a transparent manner in relation to the data subject;
(b) kept in a form which permits identification of data subjects for no longer than is necessary.

Article 13
Information to be provided
1. The notice shall include the identity and contact details of the controller and the purposes of processing.

Article 17
Right to erasure
1. The controller shall, to the extent possible, erase the personal data without undue delay where the data subject withdraws consent.

Article 32
Security of processing
1. The controller and the processor shall implement appropriate technical and organisational measures to ensure a level of security appropriate to the risk, taking into account the scope of processing.

Article 58
Powers
1. Each supervisory authority shall have all of the following investigative powers to order the controller to provide information.
"""


def test_duty_detection_reads_headings_not_body_words():
    assert detect_duty("The controller shall, to the extent possible, erase the personal data.", "", "Article 17")
    assert detect_duty("Each covered entity shall file an annual report under this title with the Commission.", "", "§ 5")
    assert detect_duty("The notice shall include the identity and contact details of the controller.", "", "Article 13")
    assert detect_duty("Each supervisory authority shall have all of the following investigative powers.", "", "") is None
    assert detect_duty("The term personal data shall include any information relating to a person.", "", "") is None
    assert detect_duty("The controller shall keep records of processing activities at all times.", "", "Definitions") is None


def _build(install, text: str, layers=("L1", "L2", "L3")):
    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.runtime import engine

    hoststore.upsert_source(engine(), "privacy", {"adapter": "local_text", "name": "Privacy", "jurisdiction": "EU",
                                                  "licence": "open", "locator": {"text": text}})
    scopes.put("privacy-scope", ["privacy"])
    os.environ[scopes.SCOPE_ENV] = "privacy-scope"
    try:
        return scope_build.build(engine(), scripted_router(engine()), layers=layers)
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)


def _obligations():
    from app.clhear.derived_models import obligations
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        return {r.clause_ref: r for r in conn.execute(
            sa.select(obligations).where(obligations.c.status.in_(("derived", "validated"))))}


def test_l2_finds_the_organisations_duties_and_only_those(install):
    report = _build(install, PRIVACY, layers=("L1", "L2"))
    assert report["failed_sources"] == []
    found = _obligations()
    assert {"art-5/1/a", "art-5/1/b", "art-13/1", "art-17/1", "art-32/1"} <= set(found)
    assert "art-58/1" not in found and "art-4/1" not in found
    item = found["art-5/1/a"]
    assert item.statement.startswith("1. Personal data shall be: (a) processed lawfully")
    assert item.title.startswith("(a) processed lawfully")
    assert found["art-32/1"].obligation_type == "implement"  # the duty's own verb, not a taxonomy

    assert report["layers"]["L2"]["model_calls"]["failed"] == 0


def test_l3_gives_every_duty_at_least_one_measure(install):
    report = _build(install, PRIVACY)
    from app.clhear.derived_models import requires
    from app.clhear.runtime import engine

    found = _obligations()
    with engine().connect() as conn:
        linked = {r[0] for r in conn.execute(sa.select(requires.c.obligation_id).where(requires.c.valid_to.is_(None)))}
    assert {o.id for o in found.values()} <= linked
    assert report["layers"]["L3"]["model_calls"]["ok"] >= 1


def test_triage_duties_survive_the_next_extraction(install):
    from app.clhear.derived_models import obligations
    from app.clhear.l2.extract import run_extraction
    from app.clhear.runtime import engine

    _build(install, PRIVACY, layers=("L1", "L2"))
    with engine().begin() as conn:
        conn.execute(obligations.update().where(obligations.c.clause_ref == "art-32/1").values(method="duty-triage-v1"))
    os.environ["CLHEAR_SOURCE_SCOPE"] = "privacy-scope"
    try:
        run_extraction(engine(), source_key="privacy")
    finally:
        os.environ.pop("CLHEAR_SOURCE_SCOPE", None)
    assert _obligations()["art-32/1"].status != "stale"

