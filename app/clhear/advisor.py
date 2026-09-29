# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Source advisor: which official sources to add to L1 so a layer can produce records.

Every layer derives its records from the texts in scope. When a layer cannot,
the build records an evidence gap (``app.clhear.evidence``). The advisor turns
those gaps into advice a user can act on: the kinds of official source to add,
why each one lets the layer derive records, and the source ``kind`` to register
it as. A text the clauses in scope cite but the scope does not hold is named as
the clauses word it (``unresolved_reference``), and the source inventory lists
every source in scope and every cited text with its status.

The advice is the same for every sector. It never names a regulation or an
authority from a list: when the sources in scope declare a publisher (the
``issuer`` of the source registration), the advice says "published by <publisher>".
"""
from __future__ import annotations

from collections import OrderedDict

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from app.clhear.l1.models import SOURCE_KINDS  # noqa: F401  (the kinds a user can register)


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
    "unresolved_reference": {
        "layer": "L1",
        "missing": "Clauses in scope cite '{cited}', which is not among the sources in scope.",
        "summary": "Register '{cited}' as a source of kind '{register_as}' and add it to the scope.",
        "add": [
            _add("{cited}", "{register_as}",
                 "The clauses that cite it depend on its text: with it in scope the reference resolves, and the "
                 "obligations, definitions and penalties it holds are read."),
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
        "missing": "No licensing, registration, certification or authorisation regime, and no register of licensed "
                   "entities, is in scope.",
        "summary": "If your activity needs a licence, registration or certification, add the text that establishes it "
                   "or the official register that lists it.",
        "add": [
            _add("The licensing, registration or certification rules for your activity", "regulation",
                 "Licence types are read from these clauses and become profile attribute values and candidate organisation profiles."),
            _add("The scope-of-practice or authorisation provisions of the act", "law",
                 "They say who may carry on an activity, which separates one organisation profile from another."),
            _add("The official register of licensed or authorised entities, or its list of licence categories",
                 "register",
                 "Each licence type a register entry names is read with its quote and becomes a permitted value of "
                 "the profile's licences and a candidate organisation profile."),
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
        "missing": "No enforcement source is in scope, so no obligation has enforcement events: risk rests only on "
                   "the penalties the texts in scope state, if any.",
        "summary": "Add the regulator's published enforcement record for these texts, and the penalty provisions of "
                   "the act if they are not in scope.",
        "add": [
            _add("Enforcement actions, consent orders and settlements", "enforcement",
                 "Each enforcement event ties a breached obligation to a consequence; risk scores are built from these links."),
            _add("Civil money penalty notices and warning letters", "enforcement",
                 "They show which obligations are enforced and how severely."),
            _add("Published breach reports, resolution agreements or corrective action plans", "enforcement",
                 "They show recurring failures and the remediation the regulator ordered."),
            _add("The act's penalty provisions (its offences, penalties and sanctions), in full", "law",
                 "Each penalty they state is quoted with its type and maximum and linked to the obligations it "
                 "refers to, so L7 scores risk from it even without enforcement records."),
        ],
    },
    "no_reference_sources": {
        "layer": "L8",
        "missing": "No guidance or enforcement source is in scope, so no component has a practice.",
        "summary": "Add the regulator's interpretive material, decisions and enforcement actions on these texts.",
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
            _add("Enforcement actions or resolution agreements that order remediation", "enforcement",
                 "The remediation a regulator orders is a practice for the component it concerns; each order is "
                 "quoted against that component."),
        ],
        "note": "News coverage may point you to one of these official sources, but it is never used as evidence.",
    },
    "undetermined": {
        "layer": "L4",
        "missing": "Some obligations depend on questions the organisation profile has not answered.",
        "summary": "Not a missing source: answer the open questions in the profile and run again.",
        "add": [],
    },
}


# What a placeholder reads as when the gap does not say (advice about several records at once).
_DEFAULTS = {"field": "this characteristic", "role": "this role", "cited": "the cited text", "register_as": "law"}


class _Filled(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fill(value, detail: dict):
    if isinstance(value, str):
        return value.format_map(_Filled({**_DEFAULTS, **{k: v for k, v in detail.items() if v not in (None, "")}}))
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
            if item["register_as"] in ("law", "regulation", "guidance", "enforcement", "register"):
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
    cited: list[dict] = []
    for gap in gaps:
        if gap["kind"] == "unresolved_reference":
            cited.append(_cited_advice(gap))
            continue
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
    return sorted(out + cited, key=lambda a: (order.get(a["layer"], 9), a["gap"], a.get("cited", "")))


def _cited_advice(gap: dict) -> dict:
    """One piece of advice per text the clauses cite but the scope does not hold,
    naming it as the clauses word it, with every clause that cites it."""
    detail = gap.get("detail") or {}
    cited = detail.get("cited") or gap["subject"]
    cited_by = list(detail.get("cited_by") or [])
    advice = advice_for("unresolved_reference", cited=cited, register_as=detail.get("register_as") or "law")
    item = advice["add"][0]
    item["cited_by"] = cited_by
    if detail.get("registered_as"):
        item["registered_as"] = detail["registered_as"]
        advice["summary"] = f"'{cited}' is registered as '{detail['registered_as']}' but is not in this scope: add it to the scope."
    return {**advice, "cited": cited, "count": len(cited_by) or 1,
            "examples": [{"subject": gap["subject"], "source_key": q.get("source_key"), "clause_ref": q.get("clause_ref"),
                          "missing": gap.get("missing")} for q in cited_by[:5]]}


STATUS_ORDER = {"derived": 0, "pending": 1, "unresolved": 2}
MAX_CITING = 20


def source_inventory(conn: Connection, scope: str | None, source_keys) -> list[dict]:
    """Every source in scope, and every text their clauses cite that the scope does not hold.

    ``status`` is ``derived`` (its text is stored and the layers read it),
    ``pending`` (registered but not built: not read yet or unreadable, or cited
    and registered but not in this scope) or ``unresolved`` (cited, not
    registered). A missing source is thereby told apart from one that does not
    apply."""
    from app.clhear import evidence
    from app.clhear.l1 import references

    keys = sorted(set(source_keys or []))
    registry = references.registered(conn)
    stored = references.stored_clauses(conn, keys)
    unread = {g["subject"]: g["missing"] for g in (evidence.gaps_for(conn, scope) if scope else [])
              if g["kind"] == "no_text"}
    resolved = references.resolve(conn, keys)
    citing = references.cited_in_scope(resolved)
    out = []
    for key in keys:
        known = registry.get(key) or {}
        entry = {"status": "derived" if stored.get(key) else "pending", "source_key": key,
                 "name": known.get("name") or key, "kind": known.get("kind") or "",
                 "reference": known.get("reference") or "", "clauses": stored.get(key, 0),
                 "cited_by": citing.get(key, [])[:MAX_CITING]}
        if not stored.get(key):
            entry["reason"] = unread.get(key) or "not read yet: run the scope"
        out.append(entry)
    for item in references.missing(conn, keys, resolved):
        entry = {"status": "pending" if item["registered_as"] else "unresolved", "source_key": item["registered_as"],
                 "cited_as": item["cited_as"], "register_as": item["register_as"],
                 "cited_by": item["cited_by"][:MAX_CITING]}
        if item["registered_as"]:
            entry["reason"] = "registered but not in this scope"
        out.append(entry)
    return sorted(out, key=lambda e: (STATUS_ORDER[e["status"]], e.get("source_key") or "", e.get("cited_as") or ""))


def advise(conn: Connection, scope: str, source_keys=None) -> list[dict]:
    """All advice for a built scope."""
    from app.clhear import evidence

    return summarise(evidence.gaps_for(conn, scope), issuers=_issuers(conn, source_keys))


def for_blueprint(conn: Connection, scope: str | None, gaps: list[dict], undetermined: bool, source_keys=None) -> list[dict]:
    """Advice for the gaps a blueprint shows (and a pointer to its open questions)."""
    return summarise(gaps, issuers=_issuers(conn, source_keys) if source_keys else [],
                     extra=["undetermined"] if undetermined else [])
