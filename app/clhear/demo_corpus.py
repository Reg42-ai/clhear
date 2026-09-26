# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Promoter demo sources in the production corpus: one fixed import request.

The import is one ``AdapterRunRequested`` per lane with ``source_keys`` set and
``discover`` false. It is not a FINRA cycle and it is not an all-publisher
cycle, and it derives nothing: the demo corpus is built from L1 up, in its own
database, by ``app.clhear.scope_build`` for the active scope.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.l2.extract import obligation_id
from app.clhear.models import events

DEMO_SOURCE_KEYS = (
    "usc/15/ftc-act-45",
    "cfr/16/255",
    "cfr/17/ia-marketing",
    "nist/csf-2.0",
)
LEGAL_SOURCE_KEYS = DEMO_SOURCE_KEYS[:3]
# Evidence for L7 (SEC Marketing Rule sweeps) and L8 (the examination risk alert).
ENFORCEMENT_SOURCE_KEYS = ("sec/enforcement/2023-173", "sec/enforcement/2024-46", "sec/enforcement/2024-121")
REFERENCE_SOURCE_KEYS = ("sec/exams/risk-alert-041724",)
DEMO_IMPORTS = (
    ("govinfo_us", DEMO_SOURCE_KEYS),
    ("sec_enforcement", ENFORCEMENT_SOURCE_KEYS),
    ("sec_edgar", REFERENCE_SOURCE_KEYS),
)
DERIVE_KIND = "DemoDeriveRequested"

def request_demo_import(engine, verification_id: str) -> dict:
    """L0 receipt: one fixed run per demo lane (GovInfo sources, SEC sweeps, SEC
    risk alert). Repeat calls keep the same events."""
    from app.clhear.l1 import workflow

    if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
        raise ValueError("Only the L0 worker may request the demo import")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", verification_id or ""):
        raise ValueError("A safe, unique verification ID is required")
    runs = []
    with engine.begin() as conn:
        for adapter, keys in DEMO_IMPORTS:
            # The GovInfo lane keeps its original event identity.
            seed = verification_id if adapter == "govinfo_us" else f"{verification_id}:{adapter}"
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "clhear-demo-import:" + seed))
            payload = {"adapter": adapter, "source_keys": list(keys), "discover": False,
                       "job_id": workflow.job_id_for(event_id, adapter)}
            workflow._insert_once(conn, events, dict(
                event_id=event_id, layer="l1", kind="AdapterRunRequested",
                subject_ref=adapter, payload=payload, producer="l0.demo", schema_version=1,
            ))
            original = conn.execute(sa.select(events.c.payload).where(events.c.event_id == event_id)).scalar_one()
            if list(original.get("source_keys") or []) != list(keys) or original.get("discover") is not False:
                raise ValueError("Verification ID is already bound to another import")
            runs.append({"event_id": event_id, "adapter": adapter, "source_keys": list(keys), "job_id": payload["job_id"]})
    first = runs[0]
    return {**first, "status": "requested", "discover": False, "runs": runs}
