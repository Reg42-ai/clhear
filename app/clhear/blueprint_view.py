# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The public shape of one reference blueprint: what the API returns and what a snapshot stores."""
from __future__ import annotations


def public_blueprint(composition: dict, *, profile_id: str | None = None) -> dict:
    return {
        "blueprint_id": composition.get("blueprint_id"),
        "profile_id": profile_id or composition.get("profile_id"),
        # The organisation profile the blueprint is for, as the run read it.
        "profile_attributes": composition.get("profile_attributes") or {},
        "items": composition.get("items") or [],
        "coverage": composition.get("coverage") or [],
        "minimality": composition.get("minimality") or {},
        "coverage_summary": composition.get("coverage_summary") or {},
        "not_applicable": composition.get("not_applicable") or [],
        "undetermined": composition.get("undetermined") or [],
        "open_questions": composition.get("open_questions") or [],
        "evidence_gaps": composition.get("evidence_gaps") or [],
        "source_advice": composition.get("source_advice") or [],
        "source_inventory": composition.get("source_inventory") or [],
        "profile_warnings": composition.get("profile_warnings") or [],
        "scope": composition.get("scope"),
        **({"sample": True} if composition.get("sample") else {}),
        "engine_version": composition.get("engine_version"),
    }
