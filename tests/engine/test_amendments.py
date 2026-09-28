# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Texts change. After an amendment, a rebuild keeps every record anchored to the text in force."""
from __future__ import annotations

import os

import sqlalchemy as sa

from .scripted_model import scripted_router
from .test_l2_l3 import PRIVACY


def _build(text: str, profiles=None):
    from app.clhear import hoststore, scope_build
    from app.clhear.l1 import scopes
    from app.clhear.runtime import engine

    hoststore.upsert_source(engine(), "privacy", {"adapter": "local_text", "jurisdiction": "EU", "locator": {"text": text}})
    scopes.put("privacy-scope", ["privacy"])
    os.environ[scopes.SCOPE_ENV] = "privacy-scope"
    try:
        return scope_build.build(engine(), scripted_router(engine()),
                                 profiles=profiles or [{"name": "eu", "attributes": {"jurisdictions": ["EU"],
                                                                                     "roles": ["controller"]}}])
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)


def _coverage(report) -> dict:
    composed = next(iter(report["compositions"].values()))
    return {c["clause_ref"]: c for c in composed["coverage"]}


def _obligations():
    from app.clhear.derived_models import obligations
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        return {r.clause_ref: r for r in conn.execute(sa.select(obligations).where(obligations.c.status == "derived"))}


def test_an_amendment_keeps_unchanged_duties_anchored(install):
    first = _build(PRIVACY)
    assert first["lineage"]["unanchored"] == []
    before = _coverage(first)
    amended = _build(PRIVACY.replace("without undue delay", "within one month"))
    assert amended["lineage"]["unanchored"] == [], amended["lineage"]["unanchored"]
    after = _coverage(amended)
    assert set(after) == set(before)  # nothing withheld after the amendment
    assert all(c["state"] == "covered" for c in after.values())
    assert "within one month" in after["art-17/1"]["duty"]
    from app.clhear.l1.models import clauses
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        text = dict(conn.execute(sa.select(clauses.c.id, clauses.c.text)).all())
    for row in after.values():
        q = row["evidence"]
        assert text[q["clause_id"]][q["start"]:q["end"]] == q["quote"]


def test_a_changed_lead_in_re_derives_its_list_items(install):
    _build(PRIVACY)
    _build(PRIVACY.replace("1. Personal data shall be:", "1. Personal data shall not be:"))
    found = _obligations()
    assert found["art-5/1/a"].modality == "must-not"
    assert "shall not be" in found["art-5/1/a"].determination
    from app.clhear.derived_models import l2_change_events
    from app.clhear.runtime import engine

    with engine().connect() as conn:
        kinds = {(r.obligation_id, r.kind) for r in conn.execute(sa.select(l2_change_events))}
    assert ("OBL:privacy#art-5/1/a", "updated") in kinds


def test_rows_derived_before_quotes_are_derived_again_after_the_upgrade(install):
    """A 0.1 database's measures carry no quotes: migration 0042 closes them and the next build re-derives."""
    import importlib

    from app.clhear.derived_models import activities, blocks, operates, requires
    from app.clhear.runtime import engine

    _build(PRIVACY)
    with engine().begin() as conn:  # what a pre-0.2 build left behind
        for table in (requires, blocks, activities, operates):
            conn.execute(table.update().values(evidence=sa.null()))  # SQL NULL, as ALTER TABLE ADD COLUMN leaves it
        importlib.import_module("migrations.m0042_grounded_evidence")._close_unquoted(conn)
    again = _build(PRIVACY)
    assert again["lineage"]["unanchored"] == [], again["lineage"]["unanchored"]
    assert all(c["state"] == "covered" for c in _coverage(again).values())
