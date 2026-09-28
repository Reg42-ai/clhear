# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L4 ontology builders (HLD v2 §4.4).

jurisdictions -> regulators -> licences / authorisations -> permitted products
and services -> client types -> channels, plus the validity rules that make
"no impossible permutation" checkable. Rows come from the reviewed register
snapshot (``curated/l4_ontology.json``) cross-checked against the live
registers when reachable; every write carries a why-trail whose evidence is
the register check (I3). Rows that disappear from the snapshot are
invalidated, never deleted (I2).

Also home of the predicate language shared with L5 triggers and L6:
:func:`matches` evaluates ``{attribute: requirement}`` against a profile's
attributes (``"*"`` = present, list = any-of, scalar = equality / containment).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import (
    attribute_schema,
    channels,
    client_types,
    licences,
    license_types,
    permits,
    products_services,
    validity_rules,
)
from app.clhear.l4 import registers as l4_registers
from app.clhear.platform import record
from app.clhear.platform.events import publish_layer_event

log = logging.getLogger("clhear.l4.ontology")

AGENT = "l4.ontology"
COLLECTIONS = ("licences", "products_services", "client_types", "channels", "permits", "validity_rules")
_TABLES = {
    "licences": licences,
    "products_services": products_services,
    "client_types": client_types,
    "channels": channels,
    "permits": permits,
    "validity_rules": validity_rules,
}


# ----------------------------------------------------------------- predicate language


def matches(predicate: dict, attributes: dict) -> bool:
    """``{attribute: requirement}`` against profile attributes.

    ``"*"`` -> attribute present and truthy; list requirement -> any of the
    listed values matches; scalar -> equality (or containment when the
    attribute is a list). Values are compared case-insensitively."""
    for key, requirement in (predicate or {}).items():
        value = attributes.get(key)
        if requirement == "*":
            if not value:
                return False
            continue
        wanted = requirement if isinstance(requirement, list) else [requirement]
        if isinstance(value, list):
            have = {_fold(v) for v in value}
            if not any(_fold(w) in have for w in wanted):
                return False
        elif isinstance(value, bool) or isinstance(requirement, bool):
            if not any(bool(value) == bool(w) for w in wanted):
                return False
        else:
            if value is None or not any(_fold(value) == _fold(w) for w in wanted):
                return False
    return True


def _fold(v) -> str:
    return str(v).strip().lower()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]


# ----------------------------------------------------------------- snapshot access


# A scoped corpus builds its ontology only from what its own L1 sources grant:
# the grounded licence registry (``license_types``), merged below. The reviewed
# register snapshot is the full corpus's starting set, not evidence in scope.
EMPTY_SNAPSHOT = {"version": "derived-only", "registers": [], "jurisdictions": [], "licences": [],
                  "products_services": [], "client_types": [], "channels": [], "permits": [], "validity_rules": []}


def snapshot() -> dict:
    from app.clhear.l1.scopes import active

    if not active():
        from app.clhear.settings import get_settings

        if not get_settings().clhear_curated_finance:
            return dict(EMPTY_SNAPSHOT)  # the host's sources define the ontology
        return l4_registers.ontology_snapshot()
    # Jurisdictions and their regulators are the ones the scope's own sources name.
    from app.clhear.l1.source_registry import S

    by_code: dict[str, set] = {}
    for entry in S:
        if entry.get("jurisdiction"):
            by_code.setdefault(entry["jurisdiction"].upper(), set()).add(entry.get("publisher") or entry["issuer"])
    jurisdictions = [{"code": code, "name": code, "regulators": sorted(regs)} for code, regs in sorted(by_code.items())]
    return {**EMPTY_SNAPSHOT, "jurisdictions": jurisdictions}


def snapshot_version(snap: dict | None = None) -> str:
    snap = snap or snapshot()
    digest = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()[:12]
    return f"{snap.get('version', '')}:{digest}"


def _why(subject_ref: str, *, summary: str, evidence: list[str], confidence: float | None,
         method: str = "register-snapshot", manifest: dict | None = None) -> record.WhyTrail:
    return record.WhyTrail(
        layer="L4",
        reasoning_summary=summary,
        evidence_refs=evidence,
        inputs=(subject_ref, method, *evidence),
        model_manifest=manifest or {"model": "deterministic", "method": method},
        skill_version=AGENT,
        confidence=confidence,
        agent_id=AGENT,
        subject_ref=subject_ref,
        input_layers=("L1",),
    )


def _live_rows(conn: Connection, table: sa.Table) -> dict:
    rows = conn.execute(sa.select(table).where(table.c.valid_to.is_(None))).mappings().all()
    if table is permits:
        return {(r["licence_id"], r["product_id"]): dict(r) for r in rows}
    return {r["id"]: dict(r) for r in rows}


def _same(existing: dict, values: dict) -> bool:
    return all(existing.get(k) == v for k, v in values.items())


def _upsert(conn: Connection, table: sa.Table, key, values: dict, live: dict, why, *, today) -> str:
    """Insert, or invalidate + re-insert when the row's content changed. Returns
    'added' | 'updated' | 'unchanged'."""
    existing = live.get(key)
    if existing is not None and _same(existing, values):
        return "unchanged"
    if existing is not None:
        # Same stable id, new content: re-version in place with the new why-trail
        # and a review note (the previous values stay in the why-trail chain).
        review = list(existing.get("review") or []) + [
            {"event": "re-versioned", "at": datetime.now(timezone.utc).isoformat(), "why_trail_id": why,
             "reason": "register snapshot changed", "previous_version": existing["version"]}
        ]
        conn.execute(table.update().where(table.c.id == existing["id"]).values(
            **values, version=existing["version"] + 1, why_trail_id=why, review=review))
        return "updated"
    if table is permits:
        record.write(conn, table, values, why=why, valid_from=today)
    else:
        record.write(conn, table, {"id": key, **values}, why=why, valid_from=today)
    return "added"


def build_ontology(engine: Engine, *, check_registers: bool = True) -> dict:
    """Materialise the ontology from the snapshot (+ live register cross-check).
    Idempotent: unchanged rows are untouched; changed rows re-versioned;
    rows gone from the snapshot invalidated."""
    with engine.begin() as conn:
        return build_ontology_in(conn, check_registers=check_registers)


def build_ontology_in(conn: Connection, *, check_registers: bool = True, publish: bool = True) -> dict:
    """Same as :func:`build_ontology` inside an open transaction. Migrations seed
    with ``publish=False``: nothing downstream exists yet to re-validate."""
    snap = snapshot()
    version = snapshot_version(snap)
    checks = l4_registers.check_registers(snap) if check_registers else {}
    today = datetime.now(timezone.utc).date()
    counts = {c: {"added": 0, "updated": 0, "unchanged": 0, "invalidated": 0} for c in COLLECTIONS}
    live = {c: _live_rows(conn, _TABLES[c]) for c in COLLECTIONS}
    evidence = [f"register:{k}:{v.freshness}" for k, v in checks.items()] or ["register-snapshot"]
    trail = _why(f"l4.ontology@{version}", summary=f"L4 ontology built from register snapshot {version}; "
                 + ", ".join(f"{k}={v.freshness}" for k, v in checks.items()),
                 evidence=evidence, confidence=1.0).write(conn)

    seen: dict[str, set] = {c: set() for c in COLLECTIONS}
    for l in snap["licences"]:
        chk = checks.get(l["register"])
        status = "validated" if chk and l["id"] in chk.confirmed else ("derived" if not chk or chk.freshness == "snapshot" else "derived")
        register_url = next((r["url"] for r in snap["registers"] if r["key"] == l["register"]), "")
        values = {"jurisdiction": l["jurisdiction"], "regulator": l.get("regulator", ""), "name": l["name"],
                  "regime": l.get("regime", ""), "register": l["register"], "register_url": register_url,
                  "register_ref": l.get("register_ref", ""), "aliases": l.get("aliases", []),
                  "clause_anchors": l.get("clause_anchors", []), "status": status, "canonical_id": None}
        counts["licences"][_upsert(conn, licences, l["id"], values, live["licences"], trail, today=today)] += 1
        seen["licences"].add(l["id"])
    for p in snap["products_services"]:
        values = {"name": p["name"], "category": p.get("category", "service"), "description": p.get("description", ""),
                  "aliases": p.get("aliases", []), "status": "derived"}
        counts["products_services"][_upsert(conn, products_services, p["id"], values, live["products_services"], trail, today=today)] += 1
        seen["products_services"].add(p["id"])
    for c in snap["client_types"]:
        values = {"name": c["name"], "description": c.get("description", ""), "aliases": c.get("aliases", []),
                  "clause_anchors": c.get("clause_anchors", []), "status": "derived"}
        counts["client_types"][_upsert(conn, client_types, c["id"], values, live["client_types"], trail, today=today)] += 1
        seen["client_types"].add(c["id"])
    for c in snap["channels"]:
        values = {"name": c["name"], "description": c.get("description", ""), "aliases": c.get("aliases", []), "status": "derived"}
        counts["channels"][_upsert(conn, channels, c["id"], values, live["channels"], trail, today=today)] += 1
        seen["channels"].add(c["id"])
    for e in snap["permits"]:
        key = (e["licence_id"], e["product_id"])
        values = {"licence_id": e["licence_id"], "product_id": e["product_id"], "basis": e.get("basis", ""),
                  "clause_anchors": e.get("clause_anchors", [])}
        counts["permits"][_upsert(conn, permits, key, values, live["permits"], trail, today=today)] += 1
        seen["permits"].add(key)
    for r in snap["validity_rules"]:
        values = {"name": r["name"], "jurisdiction": r.get("jurisdiction", "*"), "rule": r["rule"],
                  "severity": r.get("severity", "error"), "basis": r.get("basis", ""),
                  "clause_anchors": r.get("clause_anchors", []), "status": "derived"}
        counts["validity_rules"][_upsert(conn, validity_rules, r["id"], values, live["validity_rules"], trail, today=today)] += 1
        seen["validity_rules"].add(r["id"])

    # Snapshot entries that vanished: invalidate (I2). Licences grounded by the
    # licence registry were never in the snapshot; they stay while their type does.
    # A scoped build does not invalidate rows it did not derive: the database
    # may hold ontology from the rest of the corpus.
    from app.clhear.l1.scopes import keys as scope_keys

    grounded = {f"LIC:{r.jurisdiction}:{slug(r.name)}" for r in conn.execute(
        sa.select(license_types.c.jurisdiction, license_types.c.name).where(license_types.c.status != "retired"))}
    if scope_keys() is None:
        for c in COLLECTIONS:
            table = _TABLES[c]
            for key, row in live[c].items():
                if key not in seen[c] and not (c == "licences" and key in grounded):
                    record.invalidate(conn, table, table.c.id == row["id"], why=trail, reason="removed from register snapshot")
                    counts[c]["invalidated"] += 1

    # Legacy grounded license registry (l4.license_extract) joins the ontology as clause-anchored licences.
    merged = _merge_license_types(conn, live["licences"], trail, today)

    changed = any(v["added"] or v["updated"] or v["invalidated"] for v in counts.values()) or merged
    if changed and publish:
        publish_layer_event(conn, layer="L4", event="changed", subject_ref=f"l4.ontology@{version}",
                            payload={"version": version, "counts": counts, "registers": {k: v.freshness for k, v in checks.items()}},
                            producer=AGENT)
    out = {"version": version, "counts": counts, "registers": {k: v.as_dict() for k, v in checks.items()},
           "license_types_merged": merged}
    log.info("L4 ontology: %s", {c: counts[c] for c in COLLECTIONS})
    return out


def _merge_license_types(conn: Connection, live_licences: dict, trail: str, today) -> int:
    """Grounded licence types join the live licences, per jurisdiction: the same
    name in two jurisdictions is two licences."""
    merged = 0
    existing_names = {(r["jurisdiction"].upper(), _fold(r["name"])) for r in live_licences.values()}
    existing_names |= {(r["jurisdiction"].upper(), _fold(a)) for r in live_licences.values() for a in (r.get("aliases") or [])}
    for row in conn.execute(sa.select(license_types).where(license_types.c.status != "retired")).mappings():
        if (row["jurisdiction"].upper(), _fold(row["name"])) in existing_names:
            continue
        lid = f"LIC:{row['jurisdiction']}:{slug(row['name'])}"
        if conn.execute(sa.select(licences.c.id).where(licences.c.id == lid)).first():
            continue
        record.write(conn, licences, {
            "id": lid, "jurisdiction": row["jurisdiction"], "regulator": "", "name": row["name"],
            "regime": row["issuing_regime"], "register": "", "register_url": "", "register_ref": "",
            "aliases": [], "clause_anchors": row["clause_anchors"] or [], "status": row["status"], "canonical_id": None,
        }, why=trail, valid_from=today)
        existing_names.add((row["jurisdiction"].upper(), _fold(row["name"])))
        merged += 1
    return merged


# ----------------------------------------------------------------- reads


def ontology(engine: Engine) -> dict:
    """The whole live ontology, ready for the API / builder."""
    with engine.connect() as conn:
        out = {c: [dict(r) for r in conn.execute(sa.select(_TABLES[c]).where(_TABLES[c].c.valid_to.is_(None))).mappings()]
               for c in COLLECTIONS}
        out["attribute_schema"] = [dict(r) for r in conn.execute(sa.select(attribute_schema)).mappings()]
    snap = snapshot()
    out["jurisdictions"] = snap["jurisdictions"]
    out["registers"] = snap["registers"]
    out["version"] = snapshot_version(snap)
    return out


class Lookup:
    """Name / alias / id resolution for one collection (case-insensitive)."""

    def __init__(self, rows: list[dict]):
        self.rows = {r["id"]: r for r in rows}
        self.index: dict[str, str] = {}
        for r in rows:
            self.index[_fold(r["id"])] = r["id"]
            self.index[_fold(r["name"])] = r["id"]
            for a in r.get("aliases") or []:
                self.index.setdefault(_fold(a), r["id"])

    def resolve(self, value: str) -> dict | None:
        rid = self.index.get(_fold(value))
        return self.rows.get(rid) if rid else None

    def names(self) -> list[str]:
        return sorted(r["name"] for r in self.rows.values())
