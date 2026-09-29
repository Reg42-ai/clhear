# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L8 practices from the remediation an enforcement source orders.

The rule and the notice are written for these tests; neither is real. The
remediation the notice orders is a practice for the component it concerns, so
L8 builds with no guidance source in scope. The notice's findings are not
practices, and an enforcement source is never read for obligations.
"""
from __future__ import annotations

import os

import sqlalchemy as sa

from .test_live_run import _run, live  # noqa: F401  (fixture)

RULE = """Harbour lantern rule

This text is written for CLHEAR's tests. It is not a law.

Section 1. A keeper shall light the harbour lantern every evening at sunset.

Section 2. A keeper shall record each lighting in a lantern log.
"""

NOTICE = """Notice to North Quay Lights

This notice is written for CLHEAR's tests. It is not a real enforcement action.

Finding 1. The inspector found that North Quay Lights did not record lightings for three months.

Order 1. North Quay Lights is ordered to record each lighting in a lantern log, and to show the log to the inspector within 30 days.
"""


def test_remediation_in_an_enforcement_source_is_a_practice(live):  # noqa: F811
    from app.clhear.derived_models import blocks, obligations, requires
    from app.clhear.evidence import check, clause_rows
    from app.clhear.l1 import scopes
    from app.clhear.l8.reference import derived_reference_rows
    from app.clhear.runtime import engine

    client, _ = live
    client.post("/v1/sources", json={"key": "lantern", "adapter": "local_text", "kind": "regulation",
                                     "locator": {"text": RULE}})
    client.post("/v1/sources", json={"key": "notice", "adapter": "local_text", "kind": "enforcement",
                                     "locator": {"text": NOTICE}})
    client.post("/v1/scopes", json={"name": "harbour", "sources": ["lantern", "notice"]})
    client.put("/v1/profiles/keeper", json={"attributes": {"roles": ["keeper"]}})
    release = _run(client, "harbour", ["keeper"])

    released = client.get(f"/v1/releases/{release}").json()
    assert released["layers"]["L8"].get("built", True) is True  # no guidance source is in scope
    blueprint = client.get(f"/v1/releases/{release}/blueprints/keeper").json()
    assert "no_reference_sources" not in {g["kind"] for g in blueprint["evidence_gaps"]}

    os.environ[scopes.SCOPE_ENV] = "harbour"
    try:
        with engine().connect() as conn:
            rows = derived_reference_rows(conn)
            held = clause_rows(conn, [r["evidence"]["clause_id"] for r in rows])
            from_notice = conn.execute(sa.select(obligations.c.id).where(obligations.c.source_key == "notice")).all()
            record_block = conn.execute(sa.select(requires.c.block_id).where(
                requires.c.obligation_id == "OBL:lantern#sec-2", requires.c.valid_to.is_(None))).scalar()
            block_name = conn.execute(sa.select(blocks.c.name).where(blocks.c.id == record_block)).scalar()
    finally:
        os.environ.pop(scopes.SCOPE_ENV, None)
    assert from_notice == []  # an enforcement source states no obligation
    assert [(r["kind"], r["source"]["clause_ref"]) for r in rows] == [("remediation", "p4")]  # the order, not the finding
    row = rows[0]
    assert row["practice"].startswith("Order 1. North Quay Lights is ordered to record each lighting")
    assert row["ordered_in"] == {"source_key": "notice", "clause_ref": "p4"}
    assert row["block_id"] == record_block and row["block_name"] == block_name  # the component it concerns
    assert check(row["evidence"], held) is None


def test_the_advice_for_practices_names_enforcement_sources():
    from app.clhear.advisor import advice_for

    advice = advice_for("no_reference_sources")
    assert "enforcement" in advice["missing"]
    assert any(item["register_as"] == "enforcement" and "remediation" in item["source"] for item in advice["add"])
