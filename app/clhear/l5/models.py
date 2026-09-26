# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L5 junction vocabulary (HLD v2 §4.5).

An activity is what an organisation *does*. It sits on one of two sides:

* ``business``   — onboarding, order handling, marketing, deposits and withdrawals,
  custody, advice, data processing, outsourcing ...; implied by the L4
  products / services the organisation offers;
* ``compliance`` — screen, monitor, investigate, report, train, attest, assess,
  record, control, test, notify ...; each one operates L3 blocks and governs
  business activities, lit by the obligations both share.

The metaphor people use for the two sides in conversation is a narrative
device only; it never appears in the schema, the vocabulary or the API
(:func:`schema_term_lint` is the guard).
"""
from __future__ import annotations

import re
from pathlib import Path

SIDES = ("business", "compliance")

ACTION_TYPES: dict[str, dict[str, str]] = {
    "business": {
        "onboarding": "Opening and classifying client relationships",
        "order_handling": "Receiving, routing and executing client orders",
        "marketing": "Promoting products and services to clients and prospects",
        "payments": "Taking deposits and paying out withdrawals, moving client funds",
        "custody": "Holding or controlling client assets, including crypto-assets",
        "advice": "Advising clients and managing their portfolios",
        "lending": "Extending margin or securities lending to clients",
        "data_processing": "Collecting, storing and using personal data",
        "outsourcing": "Contracting third parties for ICT and other services",
        "distribution": "Distributing through intermediaries and partners",
    },
    "compliance": {
        "screen": "Screening clients, counterparties and transactions against lists and rules",
        "monitor": "Ongoing monitoring of relationships, transactions and controls",
        "investigate": "Reviewing alerts, escalations and higher-risk relationships",
        "report": "Reporting to regulators, authorities and clients",
        "notify": "Notifying authorities and affected people of incidents and breaches",
        "train": "Training and testing staff on their duties",
        "attest": "Senior management approval, sign-off and governance attestation",
        "assess": "Risk assessments, impact assessments and reviews of the programme",
        "record": "Keeping and retaining the records the rules require",
        "control": "Implementing and maintaining technical and organisational controls",
        "test": "Testing resilience, controls and recovery arrangements",
    },
}

# Words that must never become schema, vocabulary or API terms for the two sides.
FORBIDDEN_SCHEMA_TERMS = ("offense", "offence", "defense", "defence")

_FORBIDDEN_RE = re.compile(r"\b(?:" + "|".join(FORBIDDEN_SCHEMA_TERMS) + r")(?:s|ive|ively)?\b", re.I)


def is_valid_side(side: str) -> bool:
    return side in SIDES


def action_types_for(side: str) -> list[str]:
    return sorted(ACTION_TYPES.get(side, {}))


def is_valid_action_type(side: str, action_type: str) -> bool:
    return action_type in ACTION_TYPES.get(side, {})


def vocabulary() -> dict:
    """The published L5 vocabulary (served at /l5/vocabulary)."""
    return {
        "sides": list(SIDES),
        "action_types": {side: [{"key": k, "description": v} for k, v in sorted(types.items())]
                         for side, types in ACTION_TYPES.items()},
        "edges": {
            "implies": "product / service (L4) -> business activity",
            "operates": "compliance activity -> block (L3), via the obligations it implements",
            "mitigates": "compliance activity -> business activity, lit by shared obligations",
        },
    }


def schema_term_lint(paths: list[Path]) -> list[dict]:
    """Return every line in ``paths`` that uses a forbidden side term. The L5
    schema files, vocabulary and curated activities must come back empty."""
    hits: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, start=1):
            if _FORBIDDEN_RE.search(line):
                hits.append({"path": str(path), "line": n, "text": line.strip()[:160]})
    return hits
