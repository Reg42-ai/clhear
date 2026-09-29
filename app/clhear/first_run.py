# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The steps a user takes on their own texts, shared by the CLI and the HTTP API.

Register a source, preview what was read, store a profile that answers the
scope's questions, and read a blueprint back as plain text.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

from app.clhear.l1.models import SOURCE_KINDS  # noqa: F401  (the CLI checks kinds here)


def preview(engine: Engine, key: str) -> dict:
    """Read a registered source with its adapter and preview its clauses; stores nothing."""
    from app.clhear import hoststore
    from app.clhear.l1.fleet import adapter_for
    from app.clhear.l1.models import CLAUSE_TYPES

    entry = hoststore.registry_entries(engine, [key])
    if not entry:
        raise KeyError(f"unknown source {key}")
    fetched = adapter_for(entry[0]).fetch()
    if fetched is None:
        return {"stored": False, "version": None, "nodes": 0, "clauses": 0, "bytes": 0, "preview": []}
    tree = getattr(fetched, "tree", None) or []
    walked = [node for root in tree for node in root.walk()]
    clause_nodes = [node for node in walked if node.node_type in CLAUSE_TYPES and node.ref]
    nbytes = sum(len(artifact.content or b"") for artifact in fetched.artifacts)
    shown = [{"clause_ref": node.ref, "text": " ".join(node.subtree_text().split())[:200]} for node in clause_nodes[:8]]
    return {"stored": False, "version": fetched.version_label, "nodes": len(walked), "clauses": len(clause_nodes),
            "bytes": nbytes, "preview": shown}


def put_profile(engine: Engine, profile_id: str, *, name: str, attributes: dict) -> dict:
    """Check a profile's shape and store it under ``profile_id``."""
    from app.clhear import hoststore
    from app.clhear.l4.validate import create_profile

    stored = create_profile(engine, attributes, name=name, source="api", allow_invalid=True)
    row = hoststore.put_profile(engine, profile_id, name=name or stored.get("name") or profile_id,
                                attributes=attributes, engine_id=stored.get("id"))
    validity = stored.get("validity") if isinstance(stored.get("validity"), dict) else {}
    return {"row": row, "status": stored.get("status"), "engine_id": stored.get("id"),
            "validation": {"valid": stored.get("status") == "valid", "errors": validity.get("errors") or [],
                           "warnings": validity.get("warnings") or []}}


def _quote(item: dict) -> str:
    quotes = item.get("quotes") or item.get("evidence") or []
    if isinstance(quotes, dict):
        quotes = [quotes]
    q = next((q for q in quotes if isinstance(q, dict) and q.get("quote")), None)
    return f'  "{" ".join(q["quote"].split())[:140]}" ({q.get("source_key")} {q.get("clause_ref")})' if q else ""


def questions_text(schema: dict) -> str:
    """The questions a scope's texts raise, and the candidate profiles, as plain text."""
    asked = schema.get("questions") or {}
    lines = [f"Scope: {schema.get('scope')}"]
    if schema.get("note"):
        lines.append(schema["note"])
    lines.append(f"\nJurisdictions the sources declare: {', '.join(asked.get('jurisdictions') or []) or 'none'}")
    lines.append("\nRoles the obligations are addressed to (answer with \"roles\"):")
    for r in asked.get("roles") or []:
        lines += [f"- {r['role']}  ({len(r['duties'])} obligations)", _quote(r)]
    lines.append("\nConditions the obligations depend on (answer with \"conditions\": {fact: true|false}):")
    for c in asked.get("conditions") or []:
        lines += [f"- {c['fact']}  [{c['id']}]  ({len(c['duties'])} obligations)", _quote(c)]
    lines.append("\nLicence types the texts establish (answer with \"licences\"):")
    for lic in asked.get("licences") or []:
        lines.append(f"- {lic['name']} ({lic['jurisdiction']})")
    if not asked.get("licences"):
        lines.append("- none in scope")
    candidates = schema.get("candidates") or []
    if candidates:
        lines.append("\nCandidate organisation profiles to start from:")
        for c in candidates:
            lines.append(f"- {c['name']}: {c['attributes']}")
            for q in c.get("to_answer") or []:
                lines.append(f"    still to answer: {q['fact']}")
    return "\n".join(line for line in lines if line)


def advice_text(advice: list[dict]) -> str:
    """Source advice, by layer, as plain text."""
    if not advice:
        return "No missing sources: every layer could derive its records from the texts in scope."
    lines = []
    for a in advice:
        lines.append(f"{a['layer']} · {a['gap']} ({a.get('count', 1)}): {a['missing']}")
        lines.append(f"  {a['summary']}")
        for item in a.get("add") or []:
            by = f", published by {item['published_by']}" if item.get("published_by") else ""
            lines.append(f"  + {item['source']}  -> register as kind \"{item['register_as']}\"{by}")
            lines.append(f"      why: {item['why']}")
            for quote in (item.get("cited_by") or [])[:3]:
                lines.append(f"      cited by {quote['source_key']} {quote['clause_ref']}: \"{quote['quote']}\"")
        if a.get("note"):
            lines.append(f"  note: {a['note']}")
    return "\n".join(lines)


def inventory_text(inventory: list[dict]) -> str:
    """The sources in scope and the texts they cite, each with its status, as plain text."""
    if not inventory:
        return "No source in scope."
    lines = []
    for e in inventory:
        if e.get("cited_as"):
            citing = ", ".join(f"{q['source_key']} {q['clause_ref']}" for q in e.get("cited_by") or [])
            what = f"\"{e['cited_as']}\" cited by {citing}"
            if e["status"] == "unresolved":
                what += f"; register it as kind \"{e['register_as']}\""
            else:
                what += f"; registered as {e['source_key']}: {e.get('reason', '')}"
        else:
            what = f"{e['source_key']} ({e.get('kind') or '-'}) {e.get('name') or ''}".rstrip()
            what += f": {e['clauses']} clauses" if e["status"] == "derived" else f": {e.get('reason', '')}"
        lines.append(f"- {e['status']:<10} {what}")
    return "\n".join(lines)


def blueprint_text(bp: dict) -> str:
    """A reference blueprint as plain text: elements, obligations by state, open questions, gaps and advice."""
    summary = bp.get("coverage_summary") or {}
    lines = [f"Reference blueprint {bp.get('blueprint_id')} for {bp.get('profile_id')}"
             + (" (offline sample)" if bp.get("sample") else ""),
             f"Obligations: {summary.get('covered', 0)} covered, {summary.get('gaps', 0)} gaps, "
             f"{summary.get('not_applicable', 0)} not applicable, {summary.get('undetermined', 0)} undetermined",
             "\nComponents:"]
    for item in bp.get("items") or []:
        lines.append(f"- {item['name']} [{item.get('kind')}] ({item['basis']}) for {len(item['obligations_satisfied'])} obligations")
    lines.append("\nObligations that apply:")
    for c in bp.get("coverage") or []:
        lines.append(f"- {c['state']:<8} {c['source_key']} {c['clause_ref']}: {' '.join((c.get('duty') or '').split())[:110]}")
    if bp.get("not_applicable"):
        lines.append("\nNot applicable:")
        for n in bp["not_applicable"]:
            lines.append(f"- {n['source_key']} {n['clause_ref']}: {n['because'][0]['rationale'] if n['because'] else ''}")
    if bp.get("open_questions"):
        lines.append("\nOpen questions (answer them in the organisation profile, then run again):")
        for q in bp["open_questions"]:
            lines.append(f"- {q['ask']}  ({len(q['duties'])} obligations)")
    if bp.get("source_inventory"):
        lines.append("\nSources (derived: read and built; pending: registered, not built; unresolved: cited, not registered):")
        lines.append(inventory_text(bp["source_inventory"]))
    if bp.get("source_advice"):
        lines.append("\nSources to add:")
        lines.append(advice_text(bp["source_advice"]))
    return "\n".join(lines)
