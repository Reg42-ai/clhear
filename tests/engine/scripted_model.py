# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""A stand-in model for engine tests: valid, conservative JSON for every task.

It answers from the prompt alone, the way a careful model would when the text
gives it nothing more: no extra duties, no narrowing predicates, one measure
per batch of obligations named after their first duty.
"""
from __future__ import annotations

import json
import re

from app.clhear.platform.gateway import FakeProvider

_OBLIGATION_LINE = re.compile(r"^- (OBL:[^\s]+) \[(?P<key>[^\s#]+) #(?P<ref>[^\]]+)\] (?P<text>.+)$", re.M)
_CHARACTERISTIC = re.compile(r"^- (?P<key>[a-z_]+): ", re.M)


_AFTER_MODAL = re.compile(r"\b(?:shall|must|should)\s+(?:not\s+)?(?P<rest>[^,;.]+)", re.I)


def _block(prompt: str) -> dict:
    """One measure per duty, named with the words after its modal (as a careful model would)."""
    measures = []
    for row in _OBLIGATION_LINE.finditer(prompt):
        found = _AFTER_MODAL.search(row.group("text"))
        words = (found.group("rest") if found else row.group("text")).split()[:6]
        measures.append({"name": " ".join(words).capitalize(), "kind": "", "kind_quote": "",
                         "satisfies": [{"source_key": row.group("key"), "refs": [row.group("ref")]}]})
    return {"measures": measures}


def respond(prompt: str, system: str | None = None, model: str | None = None) -> str:
    system = system or ""
    if system.startswith("You classify duties"):
        answer = {"is_duty": False, "evidence_span": "", "modality": None}
    elif system.startswith("You restructure legal text"):
        answer = {"subject": "", "action": "", "condition": "", "object": "", "obligation_type": "other"}
    elif system.startswith("You consolidate"):
        answer = {"name": "", "canonical_statement": "", "member_notes": {}}
    elif system.startswith("You judge derivations"):
        answer = {"verdict": "correct", "confidence": 0.9, "reason": "Same addressee and duty."}
    elif system.startswith("You design controls"):
        answer = _block(prompt)
    elif system.startswith("You fill fixed characteristic schemas"):
        answer = {m.group("key"): "not specified by source" for m in _CHARACTERISTIC.finditer(prompt)}
    elif system.startswith("Extractive only"):
        answer = {"license_types": []}
    elif system.startswith("Extractive, closed-world"):
        answer = {"predicates": []}
    elif system.startswith("Closed-world mapper"):
        answer = {"activity_id": None, "new": None, "quote": ""}
    elif system.startswith("Citing narrator"):
        answer = {"explanation": "", "rationale": ""}
    elif system.startswith("Closed-world enforcement linker"):
        answer = {"links": []}
    elif system.startswith("Number-echo"):
        answer = {"narrative": ""}
    else:
        answer = {"materiality": "minor"}
    return json.dumps(answer)


def scripted_router(engine):
    """A Router whose only provider is the scripted stand-in."""
    from app.clhear.platform.router import Router

    provider = FakeProvider(script=respond)
    provider.model = "scripted"
    return Router(engine, providers={"fake": provider})
