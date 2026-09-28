# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L6 vocabulary — the closed terms a blueprint is described in (HLD v2 §4.6).

A blueprint is the leanest complete program for one profile: a set of
*items* (block instances) that together satisfy every applicable
obligation. Each item is either *required* (an L3 ``requires`` edge makes it
mandatory for an applicable obligation) or *selected* (chosen by the
set-cover step among blocks whose curated selectors satisfy an obligation).
"""
from __future__ import annotations

import hashlib
import json

ENGINE_VERSION = "composer-v2"

ITEM_BASES = ("required", "selected")
BLUEPRINT_STATUSES = ("current", "superseded")
COVERAGE_STATES = ("covered", "gap")

# What a per-item explanation must contain to pass the rubric (HLD v2 §4.6:
# "explanation quality ≥ 90 % on the rubric"). Every check is mechanical so
# the gate is reproducible.
EXPLANATION_RUBRIC = (
    ("names_block", "names the block it explains"),
    ("cites_obligation", "cites at least one obligation the item satisfies"),
    ("states_trigger", "states which profile facts / activities made those obligations apply"),
    ("states_role", "says whether the item is required by an obligation or selected as the leanest cover"),
    ("no_outside_ids", "cites no id outside the blueprint"),
)


def fingerprint(attributes: dict, activities: list | None, *, scope: list | None = None) -> str:
    """One profile in one scope => one fingerprint: normalised attributes + activity filter + scope sources."""
    payload = {"attributes": _normalise(attributes or {}), "activities": sorted(activities) if activities else None}
    if scope is not None:
        payload["scope"] = sorted(scope)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def _normalise(value):
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in sorted(value.items()) if v not in (None, "", [], {})}
    if isinstance(value, list):
        return sorted((_normalise(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, str):
        return value.strip().lower()
    return value


_NOT_HASHED = frozenset({"blueprint_id", "release", "composition_hash", "profile_id"})


def composition_hash(composition: dict) -> str:
    """Hash of the whole deterministic composition: items, coverage and its quotes, the duties
    not applicable or undetermined, open questions and evidence gaps. A stored blueprint is
    reused only when all of it is the same."""
    payload = {k: v for k, v in composition.items() if k not in _NOT_HASHED}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]


def vocabulary() -> dict:
    return {
        "engine_version": ENGINE_VERSION,
        "item_bases": list(ITEM_BASES),
        "blueprint_statuses": list(BLUEPRINT_STATUSES),
        "coverage_states": list(COVERAGE_STATES),
        "explanation_rubric": [{"check": c, "meaning": m} for c, m in EXPLANATION_RUBRIC],
    }
