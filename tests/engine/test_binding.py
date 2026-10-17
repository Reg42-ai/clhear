# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""A registered text says whether it binds.

A text that does not bind, such as voluntary guidance, gives no obligations even
when it uses the words of duty; L8 reads it for practices instead. Unset keeps
the rule by kind. The texts below are written for these tests.
"""
from __future__ import annotations

import sqlalchemy as sa

from .test_live_run import _run, live  # noqa: F401  (fixture)

RULE = """Supervisory rule on model records

This text is written for CLHEAR's tests. It is not a law.

Section 1. A firm shall keep a record of each model it uses to decide on credit.

Section 2. A firm shall review each model record at least once a year.
"""

GUIDE = """Guidance on model records

This text is written for CLHEAR's tests. It is not guidance of any authority.

1. A firm should name an owner for each model record and must date each review.

2. A firm should keep the records in one register that its second line can read.
"""


def test_a_text_that_does_not_bind_gives_practices_not_obligations(live):  # noqa: F811
    from app.clhear.derived_models import obligations
    from app.clhear.runtime import engine

    client, _ = live
    client.post("/v1/sources", json={"key": "rule", "adapter": "local_text", "kind": "regulation",
                                     "jurisdiction": "EU", "locator": {"text": RULE}})
    made = client.post("/v1/sources", json={"key": "guide", "adapter": "local_text", "kind": "guidance",
                                            "jurisdiction": "EU", "binding": False, "locator": {"text": GUIDE}})
    assert made.status_code == 201, made.text
    assert made.json()["binding"] is False
    assert client.get("/v1/sources/rule").json()["binding"] is None  # unset: by kind
    client.post("/v1/scopes", json={"name": "models", "sources": ["rule", "guide"]})
    client.put("/v1/profiles/firm", json={"attributes": {"jurisdictions": ["EU"]}})
    release = _run(client, "models", ["firm"])

    with engine().connect() as conn:
        read = {(r.source_key, r.clause_ref) for r in conn.execute(sa.select(obligations.c.source_key, obligations.c.clause_ref))}
    assert read == {("rule", "sec-1"), ("rule", "sec-2")}  # "must date each review" is guidance here
    layers = client.get(f"/v1/releases/{release}").json()["layers"]
    assert layers["L8"]["counts"]["reference_rows"] > 0  # the guidance is read for practices


def test_binding_is_true_false_or_unset(live):  # noqa: F811
    client, _ = live
    bad = client.post("/v1/sources", json={"key": "x", "adapter": "local_text", "binding": "sometimes",
                                           "locator": {"text": "Section 1. A firm shall act."}})
    assert bad.status_code == 422
