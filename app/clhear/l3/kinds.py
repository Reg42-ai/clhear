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

KINDS: tuple[str, ...] = ("System", "Document", "Role", "Configuration", "Process", "Workflow", "Asset", "Body",
                          "Unspecified")

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
        "description": "A named function or officer the text requires someone to hold.",
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
        "description": "A technical capability (a monitoring, screening or record-keeping system).",
        "fields": (
            ("capability", "What the system must be able to do."),
            ("data_inputs", "Data it consumes."),
            ("retention", "How long outputs / records are retained."),
        ),
    },
    "Asset": {
        "description": "Something held or maintained at a level (equipment, insurance cover, reserves).",
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
    "Unspecified": {
        "description": "The text names the measure but not what kind of thing it is.",
        "fields": (),
    },
}

NOT_SPECIFIED = "not specified by source"

# How a measure's kind is read from the duty's own words. These are the
# engine's data model in plain English (what counts as a document, a system, a
# role), not the vocabulary of any sector. Only the object's head noun counts:
# the words before its first preposition ("an inventory of the systems" is a
# Document, "access to systems" is not a System).
KIND_NOUNS: dict[str, frozenset[str]] = {
    "Document": frozenset({"policy", "policies", "procedure", "procedures", "plan", "plans", "register", "registers",
                           "record", "records", "log", "logs", "inventory", "inventories", "notice", "notices",
                           "statement", "statements", "report", "reports", "manual", "charter", "agreement",
                           "agreements", "contract", "contracts", "documentation", "document", "documents", "list",
                           "catalogue", "catalog", "file", "files"}),
    "System": frozenset({"system", "systems", "software", "database", "databases", "tool", "tools", "platform",
                         "platforms"}),
    "Role": frozenset({"officer", "officers", "representative", "representatives", "coordinator", "coordinators",
                       "manager", "managers", "person", "persons", "individual", "individuals", "lead"}),
    "Body": frozenset({"committee", "committees", "board", "boards", "body", "bodies", "forum", "forums"}),
    "Asset": frozenset({"equipment", "insurance", "facility", "facilities", "premises", "stock", "stocks"}),
    "Configuration": frozenset({"threshold", "thresholds", "limit", "limits", "setting", "settings", "parameter",
                                "parameters"}),
    "Workflow": frozenset({"workflow", "workflows", "escalation"}),
}
ROLE_VERBS = frozenset({"appoint", "designate", "nominate", "employ", "name", "engage"})
_PREPOSITION = re.compile(r"\s(?:of|to|for|in|on|with|by|from|at|about|under|within|between|against|into|that|which|who)\s", re.I)


def head_phrase(obj: str) -> str:
    """The object's words before its first preposition or relative clause."""
    return _PREPOSITION.split(f" {obj} ", maxsplit=1)[0].strip()


def kind_from_words(verb: str, obj: str) -> tuple[str, str]:
    """(kind, the word it was read from). ``Unspecified`` with no word when the
    duty's words do not say what kind of thing the measure is."""
    if verb.lower() in ROLE_VERBS:
        return "Role", verb
    for word in re.findall(r"[A-Za-z]+", head_phrase(obj)):
        for kind, nouns in KIND_NOUNS.items():
            if word.lower() in nouns:
                return kind, word
    return "Unspecified", ""


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
