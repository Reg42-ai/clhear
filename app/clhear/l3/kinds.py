# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 block kinds and their fixed characteristic schemas (HLD v2 §4.3).

A block is a type-agnostic real-life deliverable — what an organisation must
*have*. Each kind has a fixed characteristic schema; the characterizer fills
every required key with a value backed by obligation text or an explicit
"not specified by source". The registry is served at ``GET /l3/kinds``.
"""
from __future__ import annotations

import re

KINDS: tuple[str, ...] = ("System", "Document", "Role", "Configuration", "Process", "Workflow", "Asset", "Body")

# kind -> ordered (key, description) pairs. Every key is required: a missing
# characteristic is a gap, an unknowable one is recorded as `not_specified`.
KIND_SCHEMAS: dict[str, dict] = {
    "Process": {
        "description": "A repeatable activity the organisation performs (review, report, screen, reconcile).",
        "fields": (
            ("trigger", "What starts the process (event, cadence, request)."),
            ("performing_role", "Role accountable for performing it."),
            ("system_or_tool", "System or tool used, if any."),
            ("cadence", "How often it runs (continuous, daily, annually, on event)."),
            ("output", "What the process produces (report, decision, notification)."),
            ("record", "Record retained as evidence, and for how long."),
        ),
    },
    "Document": {
        "description": "A written artefact the organisation must hold (policy, procedure, register, plan).",
        "fields": (
            ("owner", "Role that owns the document."),
            ("approver", "Role or body that approves it."),
            ("review_cadence", "How often it is reviewed."),
            ("mandatory_sections", "Content the source requires it to cover."),
        ),
    },
    "Role": {
        "description": "A named function or officer (MLRO, compliance officer, data protection officer).",
        "fields": (
            ("seniority", "Required seniority (senior management, board-level, any)."),
            ("independence", "Independence requirements from business lines."),
            ("competence", "Knowledge, experience or fit-and-proper requirements."),
            ("reporting_line", "To whom the role reports."),
        ),
    },
    "Body": {
        "description": "A committee, board or forum with a mandate.",
        "fields": (
            ("mandate", "What the body decides or oversees."),
            ("quorum", "Composition or quorum requirements."),
            ("cadence", "How often it meets."),
        ),
    },
    "System": {
        "description": "A technical capability (transaction monitoring, screening, record-keeping system).",
        "fields": (
            ("capability", "What the system must be able to do."),
            ("data_inputs", "Data it consumes."),
            ("retention", "How long outputs / records are retained."),
        ),
    },
    "Asset": {
        "description": "Something held or maintained at a level (capital, client money, insurance cover).",
        "fields": (
            ("quantity_or_threshold", "Amount, ratio or threshold required."),
            ("custody", "Where or how it is held / segregated."),
        ),
    },
    "Configuration": {
        "description": "A parameter or setting the source constrains (limits, thresholds, time windows).",
        "fields": (
            ("parameter", "The parameter being set."),
            ("allowed_range", "Permitted value or range."),
        ),
    },
    "Workflow": {
        "description": "A composition of processes with hand-offs and service levels.",
        "fields": (
            ("composed_processes", "Processes it chains together."),
            ("sla", "Deadlines or service levels across the workflow."),
        ),
    },
}

NOT_SPECIFIED = "not specified by source"

# Deterministic kind cues, checked in order (first hit wins). Legal text is
# formulaic enough that these carry most of the decomposition; the LLM
# decomposer only handles clauses none of them match.
_KIND_CUES: tuple[tuple[str, re.Pattern], ...] = (
    ("Body", re.compile(r"\b(committee|board of directors|management body|supervisory board|audit committee|risk committee|forum)\b", re.I)),
    ("Role", re.compile(r"\b(appoint|designat\w+|officer|MLRO|nominated officer|compliance function|senior manager\w*|data protection officer|responsible (?:person|individual)|head of)\b", re.I)),
    ("Document", re.compile(r"\b(polic(?:y|ies)|written procedures?|procedures? (?:document|manual)|register|charter|terms of business|plan|statement|manual|contract|agreement|disclosure document|prospectus|report(?:s)? in writing)\b", re.I)),
    ("System", re.compile(r"\b(system|systems and controls|monitor\w*|surveillance|screen\w*|automated|software|information technology|ICT|record-keeping system|database)\b", re.I)),
    ("Asset", re.compile(r"\b(own funds|capital|client money|client assets|safeguarding (?:of )?(?:client|customer) (?:money|funds|assets)|insurance|indemnity|reserve|liquidity|collateral|segregat\w+)\b", re.I)),
    ("Configuration", re.compile(r"\b(threshold|limit|no (?:more|less) than|not exceed\w*|at least \d|maximum|minimum|within \d+ (?:business |working )?days|parameter)\b", re.I)),
    ("Workflow", re.compile(r"\b(escalat\w+|hand[- ]?off|end[- ]to[- ]end|workflow|approval chain|sign[- ]off)\b", re.I)),
    ("Process", re.compile(r"\b(review|assess\w*|report|notify|submit|file|verify|identify|record|retain|train\w*|test\w*|reconcil\w+|conduct|carry out|perform|disclose|inform)\b", re.I)),
)


def infer_kind(text: str) -> str:
    """Kind of the block an obligation most directly requires."""
    for kind, pattern in _KIND_CUES:
        if pattern.search(text or ""):
            return kind
    return "Process"


def required_fields(kind: str) -> tuple[str, ...]:
    return tuple(k for k, _ in KIND_SCHEMAS[kind]["fields"])


def kinds_catalog() -> list[dict]:
    return [
        {
            "kind": kind,
            "description": KIND_SCHEMAS[kind]["description"],
            "fields": [{"key": k, "description": d} for k, d in KIND_SCHEMAS[kind]["fields"]],
        }
        for kind in KINDS
    ]
