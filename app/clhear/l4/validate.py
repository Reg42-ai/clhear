# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 validators and profile store (HLD v2 §4.4).

``validate(engine, attributes)`` is the one place a profile is judged:

1. attribute keys must exist on the L4 attribute schema (types respected);
2. every value resolves to an ontology row (jurisdiction, licence, product,
   client type, channel) — names, aliases or ids; unknown values are errors;
3. an authorisation's jurisdiction must be one of the profile's jurisdictions;
4. every product must be permitted by a held licence (``permits`` edges)
   in one of the profile's jurisdictions;
5. every live ``validity_rules`` row whose ``if`` matches must have its
   ``requires`` satisfied (or ``forbids`` unsatisfied); ``error`` rows make the
   profile invalid, ``warning`` rows are surfaced.

Valid profiles can be stored as ``PRF-000001`` rows (fingerprinted so the same
attribute set is one profile); invalid permutations are never stored as valid
(the register-backed "no impossible permutation offered" guarantee).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import (
    attribute_schema,
    channels,
    client_types,
    licences,
    permits,
    products_services,
    profiles,
    validity_rules,
)
from app.clhear.l4.ontology import Lookup, matches, snapshot, snapshot_version
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l4.validate")

AGENT = "l4.validate"
LIST_KEYS_WITH_ONTOLOGY = {
    "authorisations": "licences",
    "products": "products_services",
    "customer_base": "client_types",
    "channels": "channels",
}


def _live(conn: Connection, table: sa.Table) -> list[dict]:
    return [dict(r) for r in conn.execute(sa.select(table).where(table.c.valid_to.is_(None))).mappings()]


class Ontology:
    """In-memory view of the live ontology for one validation pass."""

    def __init__(self, conn: Connection):
        self.schema = {r["key"]: dict(r) for r in conn.execute(sa.select(attribute_schema)).mappings()}
        self.licences = Lookup(_live(conn, licences))
        self.products = Lookup(_live(conn, products_services))
        self.clients = Lookup(_live(conn, client_types))
        self.channels = Lookup(_live(conn, channels))
        self.permits = _live(conn, permits)
        self.rules = _live(conn, validity_rules)
        snap = snapshot()
        self.jurisdictions = {j["code"].upper(): j for j in snap["jurisdictions"]}
        for code in _source_jurisdictions(conn):
            self.jurisdictions.setdefault(code, {"code": code, "name": code, "regulators": []})
        self.version = snapshot_version(snap)
        self.lookups = {"licences": self.licences, "products_services": self.products,
                        "client_types": self.clients, "channels": self.channels}

    def empty(self) -> bool:
        return not self.licences.rows


# How each profile fact changes which duties apply. Served by GET /v1/profile-schema.
FIELD_EFFECTS = {
    "jurisdictions": "A duty from a source with a declared jurisdiction applies only if that code is listed here. "
                     "Duties from sources with no jurisdiction apply everywhere.",
    "authorisations": "Duties addressed to 'a firm', 'an authorised person' or 'an obliged entity' apply only when at "
                      "least one authorisation is listed. Named licences narrow further when the ontology knows them.",
    "products": "Duties whose own condition names a product (for example client money or custody) apply only "
                "when that product is listed.",
    "customer_base": "Duties conditional on a client type (retail, professional ...) apply only when it is listed.",
    "channels": "Duties conditional on a channel (online, intermediaries ...) apply only when it is listed.",
    "data_footprint": "Duties addressed to a data controller or processor apply only when this is set "
                      "(any non-empty description of the personal data you handle).",
    "crypto_services": "Duties about crypto-assets apply only when true.",
    "financial_entity_dora": "Duties addressed to 'financial entities' apply only when true.",
}


def profile_schema(conn: Connection) -> list[dict]:
    """The profile fields, what each one does, and the values this install knows."""
    onto = Ontology(conn)
    fields = []
    for key, spec in sorted(onto.schema.items(), key=lambda kv: list(FIELD_EFFECTS).index(kv[0]) if kv[0] in FIELD_EFFECTS else 99):
        known: list[str] | None = None
        if key == "jurisdictions":
            known = sorted(onto.jurisdictions)
        elif key in LIST_KEYS_WITH_ONTOLOGY:
            known = onto.lookups[LIST_KEYS_WITH_ONTOLOGY[key]].names() or None
        fields.append({"key": key, "type": spec["type"], "description": spec.get("description") or "",
                       "effect": FIELD_EFFECTS.get(key, ""), "known_values": known,
                       "closed": bool(known) and key in LIST_KEYS_WITH_ONTOLOGY})
    return fields


def _source_jurisdictions(conn: Connection) -> set[str]:
    """Jurisdictions the registered sources declare (host sources and imported ones)."""
    from app.clhear.hoststore import host_sources
    from app.clhear.l1.models import sources

    found: set[str] = set()
    for table in (host_sources, sources):
        schema = None if conn.dialect.name == "sqlite" else table.schema
        if not sa.inspect(conn).has_table(table.name, schema=schema):
            continue
        for (value,) in conn.execute(sa.select(table.c.jurisdiction).distinct()):
            if value and value.strip():
                found.add(value.strip().upper())
    return found


def _as_list(value) -> list:
    if value is None or value == "":
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def validate(engine: Engine, attributes: dict) -> dict:
    with engine.connect() as conn:
        return validate_with(Ontology(conn), attributes)


def validate_with(onto: Ontology, attributes: dict) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    normalized: dict = {}
    attributes = attributes or {}

    # 1. schema keys + types
    for key, value in attributes.items():
        spec = onto.schema.get(key)
        if spec is None:
            errors.append({"code": "unknown_attribute", "attribute": key, "message": f"'{key}' is not an L4 attribute"})
            continue
        if spec["type"] == "list" and value is not None and not isinstance(value, list):
            errors.append({"code": "type", "attribute": key, "message": f"'{key}' must be a list"})
        elif spec["type"] == "bool" and value is not None and not isinstance(value, bool):
            errors.append({"code": "type", "attribute": key, "message": f"'{key}' must be true or false"})
        else:
            normalized[key] = value

    # 2. jurisdictions
    jurs: list[str] = []
    for j in _as_list(normalized.get("jurisdictions")):
        code = str(j).strip().upper()
        if not code:
            continue
        if code not in jurs:
            jurs.append(code)
        if code not in onto.jurisdictions:
            # Where an organisation operates is a fact about it, not a claim to check.
            # No source in scope names this code, so no duty is selected by it.
            warnings.append({"code": "jurisdiction_not_in_sources", "attribute": "jurisdictions", "value": code,
                             "message": f"No registered source declares jurisdiction '{code}'",
                             "known": sorted(onto.jurisdictions)})
    normalized["jurisdictions"] = jurs

    # 2. ontology-backed lists (skip the closed-world check while the ontology is empty: honest, not inventive)
    resolved: dict[str, list[dict]] = {}
    for key, collection in LIST_KEYS_WITH_ONTOLOGY.items():
        rows: list[dict] = []
        lookup = onto.lookups[collection]
        for v in _as_list(normalized.get(key)):
            row = lookup.resolve(str(v))
            if row is None:
                if onto.empty():
                    rows.append({"id": None, "name": str(v)})
                    continue
                errors.append({"code": "unknown_value", "attribute": key, "value": v,
                               "message": f"'{v}' is not in the L4 {collection.replace('_', ' ')} ontology"})
                continue
            if row not in rows:
                rows.append(row)
        resolved[key] = rows
        if key in normalized or rows:
            normalized[key] = [r["name"] for r in rows]

    # 3. authorisation jurisdiction must be one of the profile's
    for lic in resolved.get("authorisations", []):
        if lic.get("id") and jurs and lic["jurisdiction"].upper() not in jurs:
            errors.append({"code": "authorisation_jurisdiction", "attribute": "authorisations", "value": lic["name"],
                           "message": f"'{lic['name']}' is a {lic['jurisdiction']} authorisation but the profile does not operate in {lic['jurisdiction']}"})

    # 4. every product permitted by a held licence
    held = {lic["id"] for lic in resolved.get("authorisations", []) if lic.get("id")}
    permitted_products = {p["product_id"] for p in onto.permits if p["licence_id"] in held}
    for prod in resolved.get("products", []):
        if prod.get("id") and prod["id"] not in permitted_products:
            needed = sorted({onto.licences.rows[p["licence_id"]]["name"] for p in onto.permits
                             if p["product_id"] == prod["id"] and p["licence_id"] in onto.licences.rows
                             and (not jurs or onto.licences.rows[p["licence_id"]]["jurisdiction"].upper() in jurs)})
            errors.append({"code": "product_not_permitted", "attribute": "products", "value": prod["name"],
                           "message": f"no held authorisation permits '{prod['name']}'", "allowed": needed})

    # 5. validity rules
    for rule in onto.rules:
        if rule["jurisdiction"] not in ("*", "") and rule["jurisdiction"].upper() not in jurs:
            continue
        body = rule["rule"] or {}
        if not matches(body.get("if", {}), normalized):
            continue
        ok = True
        if "requires" in body and not matches(body["requires"], normalized):
            ok = False
        if "forbids" in body and matches(body["forbids"], normalized):
            ok = False
        if "requires" not in body and "forbids" not in body:
            ok = False  # informational rule: surfaces whenever its condition holds
        if not ok:
            item = {"code": "validity_rule", "rule_id": rule["id"], "rule": rule["name"], "severity": rule["severity"],
                    "message": rule["basis"], "requires": body.get("requires"), "forbids": body.get("forbids")}
            (errors if rule["severity"] == "error" else warnings).append(item)

    valid = not errors
    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized,
        "resolved": {k: [r.get("id") for r in v] for k, v in resolved.items()},
        "ontology_version": onto.version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ----------------------------------------------------------------- profile store


def fingerprint(attributes: dict) -> str:
    canon = {k: (sorted(str(x).lower() for x in v) if isinstance(v, list) else v) for k, v in sorted((attributes or {}).items())}
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def _why(subject_ref: str, summary: str, evidence: list[str], confidence: float | None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L4", reasoning_summary=summary, evidence_refs=evidence, inputs=(subject_ref, *evidence),
        model_manifest={"model": "deterministic", "method": "l4.validate"}, skill_version=AGENT,
        confidence=confidence, agent_id=AGENT, subject_ref=subject_ref, input_layers=("L1", "L2"),
    )


def create_profile(engine: Engine, attributes: dict, *, name: str = "", source: str = "builder",
                   allow_invalid: bool = False) -> dict:
    """Validate and store. Returns the profile row (existing one when the same
    attribute set is already stored). Invalid permutations raise ValueError
    unless ``allow_invalid`` (then stored with status 'invalid' for the
    approval console, never offered as valid)."""
    with engine.begin() as conn:
        return create_profile_in(conn, attributes, name=name, source=source, allow_invalid=allow_invalid)


def create_profile_in(conn: Connection, attributes: dict, *, name: str = "", source: str = "builder",
                      allow_invalid: bool = False, onto: Ontology | None = None) -> dict:
    result = validate_with(onto or Ontology(conn), attributes)
    if not result["valid"] and not allow_invalid:
        raise ValueError({"errors": result["errors"]})
    fp = fingerprint(result["normalized"])
    existing = conn.execute(sa.select(profiles).where(profiles.c.fingerprint == fp).where(profiles.c.valid_to.is_(None))).mappings().first()
    if existing is not None:
        return dict(existing)
    pid = next_id(conn, "PRF")
    summary = (f"profile validated against ontology {result['ontology_version']}: "
               f"{len(result['errors'])} error(s), {len(result['warnings'])} warning(s)")
    row = record.write(conn, profiles, {
        "id": pid, "name": name or _default_name(result["normalized"]), "attributes": result["normalized"],
        "fingerprint": fp, "validity": {k: result[k] for k in ("valid", "errors", "warnings", "ontology_version", "checked_at")},
        "source": source, "status": "valid" if result["valid"] else "invalid",
    }, why=_why(pid, summary, [f"l4.ontology@{result['ontology_version']}"], 1.0 if result["valid"] else 0.0),
        valid_from=datetime.now(timezone.utc).date(), jurisdictions=result["normalized"].get("jurisdictions"))
    return dict(row) if row else {"id": pid}


def store_sample_profiles_in(conn: Connection) -> dict:
    """The curated sample profiles become validated ``profiles`` rows (source =
    sample). An invalid sample is stored as invalid — visible, never offered."""
    from app.clhear import curated

    onto = Ontology(conn)
    out = {"stored": 0, "valid": 0, "invalid": 0}
    for item in curated.load("l4_sample_profiles"):
        row = create_profile_in(conn, item.get("attributes", {}), name=item["name"], source="sample",
                                allow_invalid=True, onto=onto)
        out["stored"] += 1
        out["valid" if row.get("status") == "valid" else "invalid"] += 1
    return out


def _default_name(attrs: dict) -> str:
    jurs = "/".join(attrs.get("jurisdictions") or []) or "unspecified"
    auth = (attrs.get("authorisations") or ["unauthorised"])[0]
    return f"{jurs} · {auth}"[:120]


def get_profile(conn: Connection, profile_id: str) -> dict | None:
    row = conn.execute(sa.select(profiles).where(profiles.c.id == profile_id)).mappings().first()
    return dict(row) if row else None


def revalidate_profiles(engine: Engine) -> dict:
    """Propagator: after an ontology change, re-judge every stored profile.
    Status flips are recorded on the row (I2: the profile stays)."""
    flipped = checked = 0
    with engine.connect() as conn:
        onto = Ontology(conn)
        rows = _live(conn, profiles)
    results = [(r, validate_with(onto, r["attributes"])) for r in rows]
    with engine.begin() as conn:
        for r, res in results:
            checked += 1
            status = "valid" if res["valid"] else "invalid"
            if status != r["status"] or res["ontology_version"] != (r["validity"] or {}).get("ontology_version"):
                trail = _why(r["id"], f"re-validated against ontology {res['ontology_version']}: now {status}",
                             [f"l4.ontology@{res['ontology_version']}"], 1.0 if res["valid"] else 0.0).write(conn)
                review = list(r.get("review") or []) + [{"event": "revalidated", "at": res["checked_at"], "from": r["status"], "to": status, "why_trail_id": trail}]
                conn.execute(profiles.update().where(profiles.c.id == r["id"]).values(
                    status=status, validity={k: res[k] for k in ("valid", "errors", "warnings", "ontology_version", "checked_at")},
                    review=review, why_trail_id=trail, version=r["version"] + 1))
                flipped += int(status != r["status"])
    return {"checked": checked, "changed": flipped}
