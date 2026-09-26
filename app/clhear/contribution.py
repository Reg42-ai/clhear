# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The only payload a host may send upstream.

This module defines and validates that payload. It does not call a URL.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

FORBIDDEN_KEYS = {
    "tenant_id", "tenant", "org_id", "organisation_id", "organization_id",
    "profile", "profile_attributes", "attributes", "actuals", "actual_controls",
    "controls", "owner", "owners", "evidence", "evidence_refs", "evidence_locator",
    "evidence_locators", "database_url", "credentials", "password", "secret",
    "api_key", "token",
}


class ProposalRejected(ValueError):
    pass


class StructuralElement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    element_id: str
    obligation_clause_ref: str
    provenance_hash: str


class AdapterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: str
    locator: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class NormalizerRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    pattern: str = ""
    replacement: str = ""


class ContributionProposal(BaseModel):
    """Public parsing rules and clause mappings.

    List source keys, adapter definitions, normalizer rules, and structural
    elements (an element id, a clause reference, and a provenance hash).
    Organisation facts, live controls, owners, evidence locators, database
    URLs, and credentials are rejected. Validating this document sends it nowhere.
    """

    model_config = ConfigDict(extra="forbid")
    source_keys: list[str]
    adapter_definitions: list[AdapterDefinition] = Field(default_factory=list)
    normalizer_rules: list[NormalizerRule] = Field(default_factory=list)
    structural_elements: list[StructuralElement] = Field(default_factory=list)

    @field_validator("source_keys")
    @classmethod
    def _keys(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("source_keys is required")
        return value


def _walk(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise ProposalRejected(f"forbidden field {path}.{key}".strip("."))
            _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]")


def parse_proposal(payload: dict) -> ContributionProposal:
    _walk(payload)
    try:
        return ContributionProposal.model_validate(payload)
    except Exception as exc:
        raise ProposalRejected(str(exc)) from exc
