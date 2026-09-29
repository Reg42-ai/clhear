# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Source advisor: which official sources to add to L1 so a layer can produce records.

Every layer derives its records from the texts in scope. When a layer cannot,
the build records an evidence gap (``app.clhear.evidence``). The advisor turns
those gaps into advice a user can act on: the kinds of official source to add,
why each one lets the layer derive records, and the source ``kind`` to register
it as.

The advice is the same for every sector. It never names a regulation or an
authority from a list: when the sources in scope declare a publisher (the
``issuer`` of the source registration), the advice says "published by <publisher>".
"""
from __future__ import annotations

from collections import OrderedDict

import sqlalchemy as sa
from sqlalchemy.engine import Connection

# Source kinds a user can register (see api._SOURCE_KINDS).
SOURCE_KINDS = ("law", "regulation", "standard", "guidance", "form", "agreement", "enforcement")


def _add(source: str, register_as: str, why: str) -> dict:
    return {"source": source, "register_as": register_as, "why": why}


# gap kind -> (layer, what is missing, one-line recommendation, sources to add)
ADVICE: dict[str, dict] = {
    "no_text": {
        "layer": "L1",
        "missing": "No text could be read from a source.",
        "summary": "Register the official text itself: a text, HTML or PDF file with a text layer, or its official URL.",
        "add": [
            _add("The official publication of the text (HTML, or a PDF with a text layer; run OCR on scans)",
                 "regulation", "L1 can only split and keep text it can read."),
        ],
    },
    "no_duties": {
        "layer": "L2",
        "missing": "The texts in scope were read, but no clause states an obligation (must, shall, is required to).",
        "summary": "Add the binding text itself (the statute or regulation), not a summary, index or table of contents.",
        "add": [
            _add("The statute or act that imposes the obligations", "law",
                 "Its clauses say who must do what; each becomes an obligation with its quote."),
            _add("The implementing regulation or rule, in full", "regulation",
                 "Implementing rules carry the detailed obligations a summary leaves out."),
        ],
    },
    "no_measure": {
        "layer": "L3",
        "missing": "The obligation says what must be achieved but names no component to put in place.",
        "summary": "Add implementing guidance, a recognised standard or a code of practice that says how the obligation is met.",
        "add": [
            _add("The regulator's implementing guidance for these provisions", "guidance",
                 "Guidance names the processes, documents and roles that meet an obligation; components are named from its words."),
            _add("A recognised standard or code of practice the texts refer to", "standard",
                 "Standards describe controls concretely, so a component can be named and its characteristics stated."),
        ],
    },
    "measure_name_rejected": {
        "layer": "L3",
        "missing": "A component name was proposed with words that are not in the text, so it was not kept.",
        "summary": "Add guidance that names the component in words, or review the obligation by hand.",
        "add": [
            _add("Implementing guidance that names how this obligation is met", "guidance",
                 "A component is kept only when its name is the text's own words."),
        ],
    },
    "characteristic_unspecified": {
        "layer": "L3",
        "missing": "The texts do not state this characteristic of the component ({field}).",
        "summary": "Add the guidance or standard that specifies {field} for this component.",
        "add": [
            _add("Guidance, a standard or a technical rule that specifies {field}", "guidance",
                 "A characteristic is recorded only when a clause states it."),
            _add("The standard the texts refer to for this component", "standard",
                 "Standards commonly state frequencies, owners, retention periods and thresholds."),
        ],
    },
    "no_licence_types": {
        "layer": "L4",
        "missing": "No licensing, registration, certification or authorisation regime is in scope.",
        "summary": "If your activity needs a licence, registration or certification, add the text that establishes it.",
        "add": [
            _add("The licensing, registration or certification rules for your activity", "regulation",
                 "Licence types are read from these clauses and become profile attribute values and candidate organization profiles."),
            _add("The scope-of-practice or authorisation provisions of the act", "law",
                 "They say who may carry on an activity, which separates one organization profile from another."),
        ],
    },
    "role_undefined": {
        "layer": "L4",
        "missing": "The texts use '{role}' but no clause in scope defines who counts as '{role}'.",
        "summary": "Add the definitions section, or the act that defines '{role}'.",
        "add": [
            _add("The definitions section of the act or regulation", "law",
                 "A definition tells you whether you are '{role}', so the obligations addressed to it can be decided."),
            _add("The interpretive rule or guidance on who is covered", "guidance",
                 "Coverage guidance settles borderline cases for '{role}'."),
        ],
    },
    "operator_not_stated": {
        "layer": "L5",
        "missing": "The text does not say who performs this compliance activity.",
        "summary": "Add the provision or guidance that assigns it (a designated officer, a governance rule), "
                   "or record your own internal allocation.",
        "add": [
            _add("Rules or guidance that designate a responsible officer or function", "guidance",
                 "A compliance activity's performing role is quoted from the text that assigns the obligation."),
            _add("Governance provisions of the act or regulation", "regulation",
                 "They say which body or role approves, oversees or performs the obligation."),
        ],
    },
    "no_enforcement_sources": {
        "layer": "L7",
        "missing": "No enforcement source is in scope, so no obligation has enforcement events and risk cannot be scored.",
        "summary": "Add the regulator's published enforcement record for these texts.",
        "add": [
            _add("Enforcement actions, consent orders and settlements", "enforcement",
                 "Each enforcement event ties a breached obligation to a consequence; risk scores are built from these links."),
            _add("Civil money penalty notices and warning letters", "enforcement",
                 "They show which obligations are enforced and how severely."),
            _add("Published breach reports, resolution agreements or corrective action plans", "enforcement",
                 "They show recurring failures and the remediation the regulator ordered."),
        ],
    },
    "no_reference_sources": {
        "layer": "L8",
        "missing": "No guidance source is in scope, so no component has a guidance-derived practice.",
        "summary": "Add the regulator's interpretive material and decisions on these texts.",
        "add": [
            _add("The regulator's FAQs and official Q&As", "guidance",
                 "They answer how an obligation is met in practice; each answer is quoted against the component it informs."),
            _add("Regulator guidance, bulletins and circulars", "guidance",
                 "They describe expected practice for the components in the reference blueprint."),
            _add("Examination or inspection findings the regulator publishes", "guidance",
                 "Findings show which components fall short, and why."),
            _add("Court and tribunal decisions on disputes under these texts", "guidance",
                 "Decisions settle how an obligation is read when it is contested."),
            _add("The official gazette or journal issues that publish amendments and notices", "guidance",
                 "They keep the texts current and announce new requirements."),
        ],
        "note": "News coverage may point you to one of these official sources, but it is never used as evidence.",
    },
    "undetermined": {
        "layer": "L4",
        "missing": "Some obligations depend on questions the organization profile has not answered.",
        "summary": "Not a missing source: answer the open questions in the profile and run again.",
        "add": [],
    },
}


def _fill(value, detail: dict):
    if isinstance(value, str):
        try:
            return value.format(**detail)
        except (KeyError, IndexError):
            return value.replace("{field}", "this characteristic").replace("{role}", "this role")
    if isinstance(value, list):
        return [_fill(v, detail) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, detail) for k, v in value.items()}
    return value


def recommendation(kind: str, **detail) -> str:
    """The one-line recommendation for a gap kind (stored on each evidence gap)."""
    entry = ADVICE.get(kind)
    return _fill(entry["summary"], detail) if entry else ""


def advice_for(kind: str, *, issuers: list[str] | None = None, **detail) -> dict | None:
    """Structured advice for one gap kind. When the sources in scope declare their
    publisher, the sources to add are asked for from those publishers."""
    entry = ADVICE.get(kind)
    if entry is None:
        return None
    out = _fill({k: v for k, v in entry.items()}, detail)
    out["gap"] = kind
    if issuers:
        named = ", ".join(issuers)
        for item in out["add"]:
            if item["register_as"] in ("guidance", "enforcement", "regulation"):
                item["published_by"] = named
    return out


def _issuers(conn: Connection, keys) -> list[str]:
    from app.clhear.l1.models import sources

    rows = conn.execute(sa.select(sources.c.issuer).where(sources.c.key.in_(list(keys or [])))).all()
    return sorted({(r.issuer or "").strip() for r in rows if (r.issuer or "").strip()})


def summarise(gaps: list[dict], *, issuers: list[str] | None = None, extra: list[str] | None = None) -> list[dict]:
    """Advice per gap kind, in layer order: the advice once, with how many records
    it concerns and a few examples."""
    grouped: OrderedDict[str, dict] = OrderedDict()
    for gap in gaps:
        slot = grouped.setdefault(gap["kind"], {"count": 0, "examples": [], "detail": gap.get("detail") or {}})
        slot["count"] += 1
        if len(slot["examples"]) < 5:
            slot["examples"].append({k: gap.get(k) for k in ("subject", "source_key", "clause_ref", "missing")})
    for kind in extra or []:
        grouped.setdefault(kind, {"count": 1, "examples": [], "detail": {}})
    out = []
    for kind, slot in grouped.items():
        detail = slot["detail"] if slot["count"] == 1 else {}
        detail = {**detail, "field": (detail.get("key") or "").replace("_", " "), "role": detail.get("role", "")}
        if kind == "role_undefined" and slot["count"] == 1 and slot["examples"]:
            detail["role"] = slot["examples"][0]["subject"].removeprefix("role:")
        advice = advice_for(kind, issuers=issuers, **detail)
        if advice is None:
            continue
        out.append({**advice, "count": slot["count"], "examples": slot["examples"]})
    order = {f"L{i}": i for i in range(1, 9)}
    return sorted(out, key=lambda a: (order.get(a["layer"], 9), a["gap"]))


def advise(conn: Connection, scope: str, source_keys=None) -> list[dict]:
    """All advice for a built scope."""
    from app.clhear import evidence

    return summarise(evidence.gaps_for(conn, scope), issuers=_issuers(conn, source_keys))


def for_blueprint(conn: Connection, scope: str | None, gaps: list[dict], undetermined: bool, source_keys=None) -> list[dict]:
    """Advice for the gaps a blueprint shows (and a pointer to its open questions)."""
    return summarise(gaps, issuers=_issuers(conn, source_keys) if source_keys else [],
                     extra=["undetermined"] if undetermined else [])
