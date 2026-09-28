# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 profiles: an organisation's answers to the questions its texts raise.

A profile is::

    {"jurisdictions": ["EU"],                       # where it operates
     "roles": ["controller"],                        # addressees it is (list = true;
                                                     #   {"processor": false} says no)
     "conditions": {"processes personal data": true, # facts the duties depend on,
                    "COND-1a2b3c4d5e": false},        #   by their words or their id
     "licences": ["..."]}                            # licence types it holds

The questions themselves (which roles, conditions and licences exist) come from
the texts in scope: ``GET /v1/profile-schema?scope=...`` lists them with their
quotes once the scope has been built. :func:`validate` checks a profile's shape;
:func:`check_answers` compares its answers with a scope's questions and warns
about answers no text in scope asks for. A question left unanswered makes the
duties that raise it *undetermined*, never silently applicable or not.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import license_types, profiles
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l4.validate")

AGENT = "l4.validate"
PROFILE_VERSION = "questions-v1"

FIELDS = {
    "jurisdictions": {
        "type": "list of strings",
        "effect": "A duty from a source with a declared jurisdiction applies only if that code is listed here. "
                  "Duties from sources with no jurisdiction apply everywhere. Leaving this out makes "
                  "jurisdiction-bound duties undetermined.",
    },
    "roles": {
        "type": "list of strings, or an object of role -> true/false",
        "effect": "A duty addressed to a named role (for example 'the operator' or 'a licensee') applies when "
                  "you are that role. A list says which roles you are; an object can also "
                  "say which you are not. A role you do not answer leaves its duties undetermined.",
    },
    "conditions": {
        "type": "object of condition -> true/false",
        "effect": "A duty with its own 'where / if / unless' clause about the addressee applies only when that "
                  "fact holds ('unless' when it does not). Answer by the fact's words or its COND- id. An "
                  "unanswered condition leaves its duties undetermined.",
    },
    "licences": {
        "type": "list of strings",
        "effect": "The licence, registration or authorisation types you hold, from those the texts in scope "
                  "establish. Listed for your record; a duty addressed to a licence holder is asked as a role.",
    },
}
RETIRED_FIELDS = frozenset({"authorisations", "products", "customer_base", "channels", "data_footprint",
                            "crypto_services", "financial_entity_dora"})


def validate(engine: Engine | None, attributes: dict) -> dict:
    """The profile's shape. Unknown fields and wrong types are errors."""
    errors: list[dict] = []
    normalized: dict = {}
    for key, value in (attributes or {}).items():
        if key not in FIELDS:
            hint = (" Roles and conditions now come from your texts: see GET /v1/profile-schema?scope=<scope>."
                    if key in RETIRED_FIELDS else "")
            errors.append({"code": "unknown_attribute", "attribute": key,
                           "message": f"'{key}' is not a profile field.{hint}"})
            continue
        if key in ("jurisdictions", "licences"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                errors.append({"code": "type", "attribute": key, "message": f"'{key}' must be a list of strings"})
                continue
            normalized[key] = sorted({v.strip().upper() if key == "jurisdictions" else v.strip()
                                      for v in value if v.strip()})
        elif key == "roles":
            ok = (isinstance(value, list) and all(isinstance(v, str) for v in value)) or (
                isinstance(value, dict) and all(isinstance(v, bool) for v in value.values()))
            if not ok:
                errors.append({"code": "type", "attribute": key,
                               "message": "'roles' must be a list of strings or an object of role -> true/false"})
                continue
            normalized[key] = sorted(v.strip() for v in value if v.strip()) if isinstance(value, list) else dict(value)
        elif key == "conditions":
            if not isinstance(value, dict) or not all(isinstance(v, bool) for v in value.values()):
                errors.append({"code": "type", "attribute": key,
                               "message": "'conditions' must be an object of condition -> true/false"})
                continue
            normalized[key] = dict(value)
    return {"valid": not errors, "errors": errors, "warnings": [], "normalized": normalized,
            "ontology_version": PROFILE_VERSION, "checked_at": datetime.now(timezone.utc).isoformat()}


def check_answers(asked: dict, attributes: dict) -> list[dict]:
    """Warnings for answers that no text in scope asks for."""
    from app.clhear.l4.predicates import _answers, norm

    warnings = []
    declared = set(asked.get("jurisdictions") or [])
    for code in attributes.get("jurisdictions") or []:
        if declared and code.upper() not in declared:
            warnings.append({"code": "jurisdiction_not_in_sources", "attribute": "jurisdictions", "value": code,
                             "message": f"No source in scope declares jurisdiction '{code}'", "known": sorted(declared)})
    roles = {r["role"] for r in asked.get("roles") or []}
    for key in _answers(attributes.get("roles")):
        if key not in roles:
            warnings.append({"code": "role_not_in_texts", "attribute": "roles", "value": key,
                             "message": f"No duty in scope is addressed to '{key}'", "known": sorted(roles)})
    facts = {c["id"].upper() for c in asked.get("conditions") or []} | {norm(c["fact"] or "") for c in asked.get("conditions") or []}
    for key in _answers(attributes.get("conditions")):
        if key not in facts:
            warnings.append({"code": "condition_not_in_texts", "attribute": "conditions", "value": key,
                             "message": f"No duty in scope depends on '{key}'"})
    known = {norm(n) for n in asked.get("licences_named") or []}
    for name in attributes.get("licences") or []:
        if norm(name) not in known:
            warnings.append({"code": "licence_not_in_texts", "attribute": "licences", "value": name,
                             "message": f"No text in scope establishes '{name}'"})
    return warnings


def licence_questions(conn: Connection, source_keys) -> list[dict]:
    """Licence types quoted from the licensing clauses of the texts in scope."""
    keys = set(source_keys or [])
    out = []
    for row in conn.execute(sa.select(license_types).where(license_types.c.status != "retired")
                            .order_by(license_types.c.name)).mappings():
        anchors = row["clause_anchors"] or []
        if keys and not any(isinstance(a, dict) and a.get("source_key") in keys for a in anchors):
            continue
        out.append({"id": row["id"], "name": row["name"], "jurisdiction": row["jurisdiction"],
                    "quotes": [a for a in anchors if isinstance(a, dict)]})
    return out


def profile_schema(conn: Connection, scope: str | None = None) -> dict:
    """The profile fields and, for a built scope, the questions its texts raise."""
    fields = [{"key": key, **spec} for key, spec in FIELDS.items()]
    if not scope:
        return {"fields": fields, "scope": None,
                "note": "Pass ?scope=<name> to list the roles, conditions and licences that scope's texts raise."}
    from app.clhear.l1 import scopes
    from app.clhear.l4.predicates import questions

    keys = scopes.get(scope)["sources"]
    asked = questions(conn, keys)
    asked["licences"] = licence_questions(conn, keys)
    built = bool(asked["roles"] or asked["conditions"] or asked["licences"]) or _has_obligations(conn, keys)
    return {"fields": fields, "scope": scope, "questions": asked,
            **({} if built else {"note": "This scope has not been built yet: run it once to derive its questions."})}


def _has_obligations(conn: Connection, keys) -> bool:
    from app.clhear.derived_models import obligations

    return conn.execute(sa.select(obligations.c.id).where(obligations.c.source_key.in_(list(keys))).limit(1)).first() is not None


# ----------------------------------------------------------------- profile store


def fingerprint(attributes: dict) -> str:
    return hashlib.sha256(json.dumps(attributes or {}, sort_keys=True, default=str).encode()).hexdigest()


def _why(subject_ref: str, summary: str, confidence: float | None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L4", reasoning_summary=summary, evidence_refs=[], inputs=(subject_ref, PROFILE_VERSION),
        model_manifest={"model": "deterministic", "method": AGENT}, skill_version=AGENT,
        confidence=confidence, agent_id=AGENT, subject_ref=subject_ref, input_layers=(),
    )


def create_profile(engine: Engine, attributes: dict, *, name: str = "", source: str = "builder",
                   allow_invalid: bool = False) -> dict:
    """Check the shape and store. Returns the profile row (the existing one when
    the same answers are already stored). A malformed profile raises ValueError
    unless ``allow_invalid`` (then it is stored with status 'invalid')."""
    with engine.begin() as conn:
        return create_profile_in(conn, attributes, name=name, source=source, allow_invalid=allow_invalid)


def create_profile_in(conn: Connection, attributes: dict, *, name: str = "", source: str = "builder",
                      allow_invalid: bool = False) -> dict:
    result = validate(None, attributes)
    if not result["valid"] and not allow_invalid:
        raise ValueError({"errors": result["errors"]})
    fp = fingerprint(result["normalized"])
    existing = conn.execute(sa.select(profiles).where(profiles.c.fingerprint == fp).where(profiles.c.valid_to.is_(None))).mappings().first()
    if existing is not None:
        return dict(existing)
    pid = next_id(conn, "PRF")
    summary = f"profile shape checked ({PROFILE_VERSION}): {len(result['errors'])} error(s)"
    row = record.write(conn, profiles, {
        "id": pid, "name": name or pid, "attributes": result["normalized"],
        "fingerprint": fp, "validity": {k: result[k] for k in ("valid", "errors", "warnings", "ontology_version", "checked_at")},
        "source": source, "status": "valid" if result["valid"] else "invalid",
    }, why=_why(pid, summary, 1.0 if result["valid"] else 0.0),
        valid_from=datetime.now(timezone.utc).date(), jurisdictions=result["normalized"].get("jurisdictions"))
    return dict(row) if row else {"id": pid}


def get_profile(conn: Connection, profile_id: str) -> dict | None:
    row = conn.execute(sa.select(profiles).where(profiles.c.id == profile_id)).mappings().first()
    return dict(row) if row else None


def revalidate_profiles(engine: Engine) -> dict:
    """Re-check every stored profile's shape (status flips are recorded on the row)."""
    flipped = checked = 0
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(profiles).where(profiles.c.valid_to.is_(None))).mappings()]
    with engine.begin() as conn:
        for r in rows:
            checked += 1
            res = validate(None, r["attributes"] or {})
            status = "valid" if res["valid"] else "invalid"
            if status != r["status"]:
                trail = _why(r["id"], f"re-checked ({PROFILE_VERSION}): now {status}", 1.0 if res["valid"] else 0.0).write(conn)
                review = list(r.get("review") or []) + [{"event": "revalidated", "at": res["checked_at"], "from": r["status"],
                                                         "to": status, "why_trail_id": trail}]
                conn.execute(profiles.update().where(profiles.c.id == r["id"]).values(
                    status=status, validity={k: res[k] for k in ("valid", "errors", "warnings", "ontology_version", "checked_at")},
                    review=review, why_trail_id=trail, version=r["version"] + 1))
                flipped += 1
    return {"checked": checked, "changed": flipped}
