# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Contribution flow (HLD v2 §6, I12 — contributions never write directly).

    proposal (web form / API / PR) → automated checks (schema, rights, duplicates)
    → the relevant fleet re-derives with the proposal as evidence and reports
      agreement / disagreement with reasons
    → two reviewers accept → applied through the approval console's record path
    → the next release ships it with attribution
    → the contributor is told the impact ("your correction changed 41 blueprints").

Roles (§6): Reader (anyone) · Contributor (signed CLA — the signature grants the
role) · Reviewer (verified professional; two distinct reviewers accept, never
the author) · Maintainer (per cluster; the ``CLHEAR_MAINTAINERS`` list counts
until SAML groups land) · Steering. Grants are rows with a validity window.

Nothing in this module writes a layer table on its own: acceptance calls the
console's ``_apply_l2_field`` / ``_apply_l3_field`` (versioned, why-trailed,
recorded as a human edit under the reviewers' names) or ``record.write`` for a
new edge with a why-trail. A contribution that fails its checks, is disputed by
the fleet, or is rejected by a reviewer leaves the registry untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.community_models import (
    CLA_VERSION,
    CONTRIBUTION_CHANNELS,
    CONTRIBUTION_KINDS,
    REVIEW_DECISIONS,
    ROLES,
    cla_signatures,
    contribution_reviews,
    contributions,
    contributor_notifications,
    roles,
    users,
)
from app.clhear.community_writes import user_id_for
from app.clhear.derived_models import blocks, blueprint_items, blueprints, equivalences, obligations, requires
from app.clhear.l1.models import clauses, source_versions, sources
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform import record
from app.clhear.platform.ids import next_id
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.contributions")

FLOW_VERSION = "contributions-v1"
REQUIRED_ACCEPTS = 2
PROPOSAL_KIND = "community_contribution"
REVIEWER_ROLES = frozenset({"reviewer", "maintainer", "steering"})
GRANTING_ROLES = frozenset({"maintainer", "steering"})
VERBATIM_MIN_CHARS = 40

# What each kind must carry inside ``proposed``.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "correction": ("value",),
    "missing_source": ("url", "title", "jurisdiction"),
    "equivalence": ("obligation_a", "obligation_b"),
    "characteristic": ("key", "value"),
    "ontology_entry": ("collection", "name", "jurisdiction"),
    "fill": ("text",),
    "translation": ("language", "text"),
    "golden_case": ("suite", "case"),
    "evidence_template": ("conformance_level", "text"),
    "enforcement_link": ("obligation_id",),
}
DEFAULT_LAYER: dict[str, str] = {
    "correction": "L2", "missing_source": "L1", "equivalence": "L2", "characteristic": "L3",
    "ontology_entry": "L4", "fill": "L8", "translation": "L2", "golden_case": "L0",
    "evidence_template": "L0", "enforcement_link": "L7",
}
ONTOLOGY_COLLECTIONS = ("licences", "products_services", "client_types", "channels")
GOVERNANCE_DIR = Path(__file__).resolve().parents[3] / "export" / "clhear" / "governance"

_URL = re.compile(r"^https?://[^\s]+$", re.I)


class CLARequired(PermissionError):
    """The contributor has not signed the current CLA."""


class NotAReviewer(PermissionError):
    """Only Reviewer / Maintainer / Steering identities review."""


class SelfReview(ValueError):
    """A contributor cannot review their own contribution."""


class InvalidContribution(ValueError):
    """Malformed submission (unknown kind, channel, decision…)."""


class WrongStatus(RuntimeError):
    """The transition is not allowed from the contribution's current status."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v, default):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _plain(row: dict) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime):
            v = v.isoformat()
        elif hasattr(v, "isoformat"):
            v = v.isoformat()
        out[k] = v
    for k in ("proposed", "evidence", "checks", "rederivation", "applied", "impact"):
        if k in out and isinstance(out[k], str):
            out[k] = _json(out[k], None)
    return out


# --------------------------------------------------------------------------- identity & roles


def _ensure_user(conn: Connection, email: str, display_name: str = "") -> str:
    email = email.strip().lower()
    uid = user_id_for(email)
    if conn.execute(sa.select(users.c.id).where(users.c.id == uid)).first() is None:
        conn.execute(users.insert().values(id=uid, email=email, display_name=display_name or email.split("@")[0],
                                           provider="email", provider_sub="", last_login_at=_now()))
    return uid


def cla_text() -> str:
    path = GOVERNANCE_DIR / "CLA.md"
    return path.read_text(encoding="utf-8") if path.exists() else f"CLHEAR Contributor License Agreement v{CLA_VERSION}"


def cla_hash() -> str:
    return hashlib.sha256(cla_text().encode("utf-8")).hexdigest()


def cla_signed(conn: Connection, email: str, version: str = CLA_VERSION) -> dict | None:
    row = conn.execute(sa.select(cla_signatures).where(cla_signatures.c.user_id == user_id_for(email),
                                                       cla_signatures.c.cla_version == version)).mappings().first()
    return _plain(row) if row else None


def sign_cla(engine: Engine, email: str, *, display_name: str = "", acknowledge_patent_grant: bool = True) -> dict:
    """Sign the current CLA (idempotent per version). Signing grants the Contributor role."""
    if not acknowledge_patent_grant:
        raise InvalidContribution("the CLA's patent grant (limited to use of the standard) must be acknowledged")
    email = email.strip().lower()
    with engine.begin() as conn:
        uid = _ensure_user(conn, email, display_name)
        existing = cla_signed(conn, email)
        if existing:
            return existing
        conn.execute(cla_signatures.insert().values(user_id=uid, email=email, cla_version=CLA_VERSION,
                                                    text_hash=cla_hash(), patent_grant_acknowledged=True))
        conn.execute(roles.insert().values(user_id=uid, email=email, role="contributor", granted_by="cla",
                                           note=f"signed CLA v{CLA_VERSION}"))
    with engine.connect() as conn:
        return cla_signed(conn, email) or {}


def roles_for(conn: Connection, email: str) -> set[str]:
    """Effective roles: reader for everyone, granted rows still valid, the maintainer list."""
    email = (email or "").strip().lower()
    out = {"reader"}
    if not email:
        return out
    if email in {m.lower() for m in get_settings().maintainer_set}:
        out.add("maintainer")
    for r in conn.execute(sa.select(roles.c.role).where(roles.c.user_id == user_id_for(email), roles.c.valid_to.is_(None))):
        out.add(r.role)
    return out


def grant_role(engine: Engine, *, email: str, role: str, granted_by: str, note: str = "") -> dict:
    if role not in ROLES or role in ("reader", "contributor"):
        raise InvalidContribution("grantable roles are reviewer, maintainer, steering (contributor comes from the CLA)")
    with engine.begin() as conn:
        if not (roles_for(conn, granted_by) & GRANTING_ROLES):
            raise PermissionError(f"{granted_by} may not grant roles (needs maintainer or steering)")
        uid = _ensure_user(conn, email)
        live = conn.execute(sa.select(roles).where(roles.c.user_id == uid, roles.c.role == role,
                                                   roles.c.valid_to.is_(None))).mappings().first()
        if live:
            return _plain(live)
        conn.execute(roles.insert().values(user_id=uid, email=email.strip().lower(), role=role,
                                           granted_by=granted_by, note=note))
        live = conn.execute(sa.select(roles).where(roles.c.user_id == uid, roles.c.role == role,
                                                   roles.c.valid_to.is_(None))).mappings().first()
    return _plain(live)


def revoke_role(engine: Engine, *, email: str, role: str, revoked_by: str) -> int:
    """Close the grant (valid_to); the row stays (I2)."""
    with engine.begin() as conn:
        if not (roles_for(conn, revoked_by) & GRANTING_ROLES):
            raise PermissionError(f"{revoked_by} may not revoke roles")
        res = conn.execute(roles.update().where(roles.c.user_id == user_id_for(email), roles.c.role == role,
                                                roles.c.valid_to.is_(None)).values(valid_to=_now()))
        return res.rowcount


def role_holders(engine: Engine, role: str | None = None) -> list[dict]:
    q = sa.select(roles).where(roles.c.valid_to.is_(None)).order_by(roles.c.granted_at)
    if role:
        q = q.where(roles.c.role == role)
    with engine.connect() as conn:
        return [_plain(r) for r in conn.execute(q).mappings()]


# --------------------------------------------------------------------------- automated checks


def _content_hash(kind: str, layer: str, target_ref: str, field: str, proposed: dict) -> str:
    body = json.dumps([kind, layer, target_ref, field, proposed], sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _resolve_obligation(conn: Connection, ref: str) -> dict | None:
    from app.clhear.l2 import registry

    oid = registry.resolve_obligation_id(conn, ref) or ref
    row = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().first()
    return dict(row) if row else None


def _block(conn: Connection, block_id: str) -> dict | None:
    row = conn.execute(sa.select(blocks).where(blocks.c.id == block_id)).mappings().first()
    return dict(row) if row else None


def _live_event(conn: Connection, event_id: str) -> dict | None:
    from app.clhear.l7.models import enforcement_events

    row = conn.execute(sa.select(enforcement_events).where(enforcement_events.c.id == event_id,
                                                           enforcement_events.c.valid_to.is_(None))).mappings().first()
    return dict(row) if row else None


def _check_schema(kind: str, channel: str, proposed: dict, evidence: list) -> dict:
    problems = []
    if kind not in CONTRIBUTION_KINDS:
        problems.append(f"unknown kind {kind!r}")
    if channel not in CONTRIBUTION_CHANNELS:
        problems.append(f"unknown channel {channel!r}")
    if not isinstance(proposed, dict):
        problems.append("proposed must be an object")
    else:
        for key in REQUIRED_FIELDS.get(kind, ()):
            v = proposed.get(key)
            if v is None or (isinstance(v, str) and not v.strip()):
                problems.append(f"proposed.{key} is required for {kind}")
        if kind == "golden_case" and proposed.get("case") is not None:
            case = proposed["case"]
            if isinstance(case, str):
                try:
                    case = json.loads(case)
                    proposed["case"] = case
                except ValueError:
                    problems.append("proposed.case must be a JSON object")
            if isinstance(case, dict):
                if not case.get("id"):
                    problems.append("proposed.case.id is required")
                if "expected" not in case and not any(k.startswith("expected_") for k in case):
                    problems.append("proposed.case needs `expected` or `expected_*` keys (see evals/harness.py)")
            elif not isinstance(case, str):
                problems.append("proposed.case must be a JSON object")
    if not isinstance(evidence, list):
        problems.append("evidence must be a list of {url, quote}")
    else:
        for e in evidence:
            if not isinstance(e, dict):
                problems.append("evidence entries must be objects")
            elif e.get("url") and not _URL.match(str(e["url"])):
                problems.append(f"evidence url is not http(s): {e['url']!r}")
    return {"check": "schema", "ok": not problems, "detail": "; ".join(problems) or "well-formed"}


def _check_target(conn: Connection, kind: str, layer: str, target_ref: str, field: str, proposed: dict) -> dict:
    from app.clhear.platform.console import L2_FIELDS, L3_FIELDS

    if kind in ("correction", "translation"):
        if layer == "L2":
            ob = _resolve_obligation(conn, target_ref)
            if ob is None:
                return {"check": "target", "ok": False, "detail": f"no obligation {target_ref}"}
            if kind == "correction" and field not in L2_FIELDS:
                return {"check": "target", "ok": False, "detail": f"field {field!r} is not editable on obligations"}
            return {"check": "target", "ok": True, "detail": f"obligation {ob['id']} ({ob.get('status')})"}
        if layer == "L3":
            b = _block(conn, target_ref)
            if b is None:
                return {"check": "target", "ok": False, "detail": f"no block {target_ref}"}
            if kind == "correction" and field not in L3_FIELDS and not field.startswith("characteristic:"):
                return {"check": "target", "ok": False, "detail": f"field {field!r} is not editable on blocks"}
            return {"check": "target", "ok": True, "detail": f"block {b['id']} ({b.get('kind')})"}
        return {"check": "target", "ok": False, "detail": f"{kind} targets L2 or L3, not {layer}"}
    if kind == "characteristic":
        from app.clhear.l3 import kinds as l3_kinds

        b = _block(conn, target_ref)
        if b is None:
            return {"check": "target", "ok": False, "detail": f"no block {target_ref}"}
        allowed = l3_kinds.required_fields(b["kind"]) if b.get("kind") in l3_kinds.KIND_SCHEMAS else ()
        if allowed and proposed.get("key") not in allowed:
            return {"check": "target", "ok": False,
                    "detail": f"{proposed.get('key')!r} is not a characteristic of a {b['kind']} ({', '.join(allowed)})"}
        return {"check": "target", "ok": True, "detail": f"block {b['id']} ({b['kind']}) key {proposed.get('key')}"}
    if kind == "equivalence":
        a = _resolve_obligation(conn, str(proposed.get("obligation_a")))
        b = _resolve_obligation(conn, str(proposed.get("obligation_b")))
        if a is None or b is None:
            return {"check": "target", "ok": False, "detail": "both obligations must exist"}
        if a["id"] == b["id"]:
            return {"check": "target", "ok": False, "detail": "an obligation is not equivalent to itself"}
        return {"check": "target", "ok": True, "detail": f"{a['id']} ≡ {b['id']}"}
    if kind == "enforcement_link":
        ev = _live_event(conn, target_ref)
        ob = _resolve_obligation(conn, str(proposed.get("obligation_id")))
        if ev is None:
            return {"check": "target", "ok": False, "detail": f"no live enforcement event {target_ref}"}
        if ob is None:
            return {"check": "target", "ok": False, "detail": f"no obligation {proposed.get('obligation_id')}"}
        return {"check": "target", "ok": True, "detail": f"{ev['id']} → {ob['id']}"}
    if kind == "ontology_entry":
        if proposed.get("collection") not in ONTOLOGY_COLLECTIONS:
            return {"check": "target", "ok": False, "detail": f"collection must be one of {ONTOLOGY_COLLECTIONS}"}
        return {"check": "target", "ok": True, "detail": f"{proposed['collection']} / {proposed['name']}"}
    if kind == "missing_source":
        return {"check": "target", "ok": bool(_URL.match(str(proposed.get("url", "")))),
                "detail": "source url is http(s)" if _URL.match(str(proposed.get("url", ""))) else "source url must be http(s)"}
    if kind == "golden_case":
        case = proposed.get("case")
        ok = isinstance(case, dict) and bool(case.get("id")) and "expected" in case
        return {"check": "target", "ok": ok, "detail": "case has id and expected" if ok else "case needs {id, …, expected}"}
    if kind == "fill":
        b = _block(conn, target_ref)
        return {"check": "target", "ok": b is not None, "detail": f"block {target_ref}" if b else f"no block {target_ref}"}
    return {"check": "target", "ok": True, "detail": "no target constraint for this kind"}


def _check_rights(conn: Connection, proposed: dict, evidence: list) -> dict:
    """I8 / §9: nothing verbatim from a source without a republication basis."""
    texts = [s for s in _strings(proposed) + [str(e.get("quote", "")) for e in evidence if isinstance(e, dict)]
             if len(s.strip()) >= VERBATIM_MIN_CHARS]
    for s in texts:
        needle = " ".join(s.split())[:400]
        hit = conn.execute(sa.select(clauses.c.id).where(clauses.c.public_ok.is_(False),
                                                          clauses.c.text.contains(needle, autoescape=True)).limit(1)).first()
        if hit:
            return {"check": "rights", "ok": False,
                    "detail": f"quotes verbatim text of a source without a republication basis (clause {hit.id})"}
    return {"check": "rights", "ok": True, "detail": "no verbatim restricted text"}


def _check_duplicates(conn: Connection, *, kind: str, layer: str, target_ref: str, field: str, proposed: dict,
                      content_hash: str) -> dict:
    dup = conn.execute(sa.select(contributions.c.id, contributions.c.status)
                       .where(contributions.c.content_hash == content_hash,
                              contributions.c.status.notin_(("rejected", "checks_failed")))
                       .order_by(contributions.c.created_at).limit(1)).first()
    if dup:
        return {"check": "duplicates", "ok": False, "detail": f"same proposal already open as {dup.id} ({dup.status})"}
    if kind == "correction" and layer == "L2":
        ob = _resolve_obligation(conn, target_ref)
        if ob is not None and field in ob and str(ob[field] or "") == str(proposed.get("value") or ""):
            return {"check": "duplicates", "ok": False, "detail": f"{field} already has this value"}
    if kind == "equivalence":
        a = _resolve_obligation(conn, str(proposed.get("obligation_a")))
        b = _resolve_obligation(conn, str(proposed.get("obligation_b")))
        if a and b:
            ids = {a["id"], b["id"]}
            live = conn.execute(sa.select(equivalences.c.id).where(
                equivalences.c.valid_to.is_(None), equivalences.c.obligation_a.in_(ids), equivalences.c.obligation_b.in_(ids)
            ).limit(1)).first()
            if live:
                return {"check": "duplicates", "ok": False, "detail": f"equivalence already recorded as {live.id}"}
    if kind == "missing_source":
        url = str(proposed.get("url", "")).rstrip("/").lower()
        for r in conn.execute(sa.select(sources.c.key, sources.c.canonical_url, sources.c.name)):
            if url and (r.canonical_url or "").rstrip("/").lower() == url:
                return {"check": "duplicates", "ok": False, "detail": f"already in the corpus as {r.key}"}
    return {"check": "duplicates", "ok": True, "detail": "no open duplicate"}


def automated_checks(conn: Connection, *, kind: str, channel: str, layer: str, target_ref: str, field: str,
                     proposed: dict, evidence: list, content_hash: str) -> list[dict]:
    checks = [_check_schema(kind, channel, proposed, evidence)]
    if not checks[0]["ok"]:
        return checks
    checks.append(_check_target(conn, kind, layer, target_ref, field, proposed))
    checks.append(_check_rights(conn, proposed, evidence))
    checks.append(_check_duplicates(conn, kind=kind, layer=layer, target_ref=target_ref, field=field,
                                    proposed=proposed, content_hash=content_hash))
    return checks


# --------------------------------------------------------------------------- submit


def _notify(conn: Connection, *, user_id: str, contribution_id: str, kind: str, message: str, payload: dict | None = None) -> None:
    conn.execute(contributor_notifications.insert().values(user_id=user_id, contribution_id=contribution_id, kind=kind,
                                                           message=message, payload=payload or {}))


def submit(engine: Engine, *, contributor_email: str, kind: str, proposed: dict, target_ref: str = "",
           layer: str | None = None, field: str = "", evidence: list | None = None, rationale: str = "",
           channel: str = "web", display_name: str = "") -> dict:
    """File a contribution. Requires a signed CLA; runs the automated checks; mirrors
    into the l0 proposals queue when they pass. Never touches a layer table."""
    if kind not in CONTRIBUTION_KINDS:
        raise InvalidContribution(f"kind must be one of {CONTRIBUTION_KINDS}")
    if channel not in CONTRIBUTION_CHANNELS:
        raise InvalidContribution(f"channel must be one of {CONTRIBUTION_CHANNELS}")
    email = contributor_email.strip().lower()
    layer = (layer or DEFAULT_LAYER[kind]).upper()
    proposed = dict(proposed or {})
    evidence = list(evidence or [])
    field = field or (str(proposed.get("field", "")) if kind == "correction" else "")
    if kind == "correction" and not field:
        raise InvalidContribution("a correction names the field it corrects")
    content_hash = _content_hash(kind, layer, target_ref, field, proposed)
    with engine.begin() as conn:
        uid = _ensure_user(conn, email, display_name)
        if "contributor" not in roles_for(conn, email):
            raise CLARequired(f"{email} has not signed CLA v{CLA_VERSION}")
        checks = automated_checks(conn, kind=kind, channel=channel, layer=layer, target_ref=target_ref, field=field,
                                  proposed=proposed, evidence=evidence, content_hash=content_hash)
        ok = all(c["ok"] for c in checks)
        cid = next_id(conn, "CON")
        proposal_id = None
        if ok:
            proposal_id = l0_proposals.create_proposal(
                conn, layer="community", kind=PROPOSAL_KIND, subject_ref=target_ref or layer,
                draft={"contribution_id": cid, "kind": kind, "layer": layer, "target_ref": target_ref, "field": field,
                       "proposed": proposed, "evidence": evidence, "contributor": email},
                rationale=f"community contribution {cid} by {email}: {rationale}"[:1000],
            )
        conn.execute(contributions.insert().values(
            id=cid, kind=kind, channel=channel, layer=layer, target_ref=target_ref, field=field, proposed=proposed,
            evidence=evidence, rationale=rationale[:4000], content_hash=content_hash, contributor_id=uid,
            contributor_email=email, status="checked" if ok else "checks_failed", checks=checks,
            proposal_id=proposal_id, updated_at=_now(),
        ))
        if not ok:
            failed = "; ".join(c["detail"] for c in checks if not c["ok"])
            _notify(conn, user_id=uid, contribution_id=cid, kind="checks_failed",
                    message=f"{cid} did not pass the automated checks: {failed}", payload={"checks": checks})
    return get(engine, cid)


# --------------------------------------------------------------------------- re-derivation


def _clause_text_for_obligation(conn: Connection, ob: dict) -> str:
    from app.clhear.derived_models import asserts

    row = conn.execute(sa.select(clauses.c.text).select_from(asserts.join(clauses, clauses.c.id == asserts.c.clause_id))
                       .where(asserts.c.obligation_id == ob["id"], asserts.c.valid_to.is_(None))
                       .order_by(asserts.c.id.desc()).limit(1)).first()
    if row:
        return row.text or ""
    row = conn.execute(sa.select(clauses.c.text)
                       .select_from(clauses.join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                                    .join(sources, sources.c.id == source_versions.c.source_id))
                       .where(sources.c.key == ob["source_key"], clauses.c.ref == ob["clause_ref"])
                       .order_by(clauses.c.id.desc()).limit(1)).first()
    return (row.text if row else "") or ""


def _backing_texts_for_block(conn: Connection, block_id: str) -> list[str]:
    rows = conn.execute(sa.select(obligations.c.statement, obligations.c.title)
                        .select_from(requires.join(obligations, obligations.c.id == requires.c.obligation_id))
                        .where(requires.c.block_id == block_id, requires.c.valid_to.is_(None))).all()
    return [(r.statement or r.title or "") for r in rows]


def _norm(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def _verdict(agreement: str, reasons: list[str], *, method: str, derived: Any = None) -> dict:
    return {"agreement": agreement, "reasons": reasons, "derived": derived, "method": method,
            "at": _now().isoformat(), "flow": FLOW_VERSION}


def _rederive_l2_correction(conn: Connection, row: dict) -> dict:
    from app.clhear.l2 import registry, structured

    ob = _resolve_obligation(conn, row["target_ref"])
    if ob is None:
        return _verdict("disagree", [f"obligation {row['target_ref']} no longer exists"], method="l2.extract")
    field, value = row["field"], row["proposed"].get("value")
    text = _clause_text_for_obligation(conn, ob)
    if not text:
        return _verdict("unverified", ["the anchoring clause text is not available to the fleet (rights or missing)"],
                        method="l2.extract")
    derived = registry.structured_fields(text, ob.get("modality") or "")
    if field in derived:
        same = _norm(derived[field]) == _norm(value)
        grounded = structured.grounded(str(value), text)
        if same:
            return _verdict("agree", [f"the deterministic L2 reader derives the same {field} from the clause"],
                            method="l2.extract", derived=derived[field])
        if grounded:
            return _verdict("agree", [f"the proposed {field} is grounded in the clause text (≥ {int(structured.GROUNDING_MIN * 100)} % of its words)",
                                      f"the reader's own reading differs: {derived[field]!r}"],
                            method="l2.extract", derived=derived[field])
        return _verdict("disagree", [f"the proposed {field} is not grounded in the clause text", f"reader: {derived[field]!r}"],
                        method="l2.extract", derived=derived[field])
    if field in ("title", "statement", "addressee"):
        grounded = structured.grounded(str(value), text)
        return _verdict("agree" if grounded else "disagree",
                        [f"the proposed {field} {'is' if grounded else 'is not'} grounded in the clause text"],
                        method="l2.extract")
    if field == "modality":
        found = _norm(value) in _norm(text)
        return _verdict("agree" if found else "disagree",
                        [f"modality {value!r} {'appears' if found else 'does not appear'} in the clause"], method="l2.extract")
    return _verdict("unverified", [f"the L2 fleet does not derive {field}; reviewers check the source"], method="l2.extract",
                    derived=ob.get(field))


def _rederive_l3(conn: Connection, row: dict) -> dict:
    from app.clhear.l2 import structured
    from app.clhear.l3 import kinds as l3_kinds

    b = _block(conn, row["target_ref"])
    if b is None:
        return _verdict("disagree", [f"block {row['target_ref']} no longer exists"], method="l3.characterize")
    if row["kind"] == "characteristic":
        key, value = row["proposed"].get("key"), row["proposed"].get("value")
    else:
        key, value = row["field"], row["proposed"].get("value")
        if key == "kind":
            ok = value in l3_kinds.KINDS
            return _verdict("agree" if ok else "disagree", [f"{value!r} {'is' if ok else 'is not'} one of the eight kinds"],
                            method="l3.kinds")
        if key == "status":
            return _verdict("unverified", ["status is a maintainer decision, not a derivation"], method="l3.characterize")
    backing = _backing_texts_for_block(conn, b["id"])
    if not backing:
        return _verdict("unverified", ["no live obligation backs this block; nothing to ground the value in"],
                        method="l3.characterize")
    hay = " ".join(backing)
    grounded = structured.grounded(str(value), hay)
    return _verdict("agree" if grounded else "disagree",
                    [f"{key} value {'is' if grounded else 'is not'} grounded in the {len(backing)} backing obligation(s)"],
                    method="l3.characterize")


def _rederive_equivalence(conn: Connection, row: dict) -> dict:
    from app.clhear.l2 import dedupe

    a = _resolve_obligation(conn, str(row["proposed"].get("obligation_a")))
    b = _resolve_obligation(conn, str(row["proposed"].get("obligation_b")))
    if a is None or b is None:
        return _verdict("disagree", ["an obligation no longer exists"], method="l2.consolidate")
    ta, tb = a.get("statement") or a.get("title") or "", b.get("statement") or b.get("title") or ""
    if not ta or not tb:
        return _verdict("unverified", ["one statement is withheld (rights); lexical comparison impossible"], method="l2.consolidate")
    sim = dedupe.similarity(ta, tb)
    agree = sim >= dedupe.EQUIVALENCE_THRESHOLD
    reasons = [f"lexical similarity {sim:.2f} vs threshold {dedupe.EQUIVALENCE_THRESHOLD:.2f}"]
    if a.get("jurisdiction") == b.get("jurisdiction"):
        reasons.append("same jurisdiction — this may be a duplicate rather than an equivalence")
    return _verdict("agree" if agree else "disagree", reasons, method="l2.consolidate", derived={"similarity": round(sim, 3)})


def _rederive_enforcement_link(conn: Connection, row: dict) -> dict:
    from app.clhear.l7 import enforcement

    ev = _live_event(conn, row["target_ref"])
    ob = _resolve_obligation(conn, str(row["proposed"].get("obligation_id")))
    if ev is None or ob is None:
        return _verdict("disagree", ["event or obligation no longer exists"], method="l7.link")
    text = ""
    if ev.get("clause_id"):
        r = conn.execute(sa.select(clauses.c.text).where(clauses.c.id == ev["clause_id"])).first()
        text = (r.text if r else "") or ""
    text = text or ev.get("summary") or ev.get("title") or ""
    links = enforcement.link_text(text, enforcement._registry_index(conn))
    hits = [l for l in links if l.get("obligation_id") == ob["id"]]
    if hits:
        return _verdict("agree", [f"the linker resolves the notice's citation {hits[0].get('citation')!r} to {ob['id']}"],
                        method="l7.link", derived=hits[0])
    quotes = [str(e.get("quote", "")) for e in row["evidence"] if isinstance(e, dict) and e.get("quote")]
    if any(_norm(q) in _norm(text) for q in quotes):
        return _verdict("unverified", ["the quoted passage is in the notice but does not cite the obligation's provision; reviewers decide"],
                        method="l7.link")
    return _verdict("disagree", ["the notice does not cite the obligation's provision and no quote from it was supplied"],
                    method="l7.link", derived=[l.get("obligation_id") for l in links])


def _rederive_missing_source(conn: Connection, row: dict) -> dict:
    url = str(row["proposed"].get("url", "")).rstrip("/").lower()
    for r in conn.execute(sa.select(sources.c.key, sources.c.canonical_url)):
        if url and (r.canonical_url or "").rstrip("/").lower() == url:
            return _verdict("disagree", [f"already in the corpus as {r.key}"], method="l1.registry")
    return _verdict("unverified", ["not in the corpus; an L1 adapter and rights basis are a maintainer decision"], method="l1.registry")


def _rederive_golden_case(conn: Connection, row: dict) -> dict:
    case = row["proposed"].get("case") or {}
    ok = isinstance(case, dict) and bool(case.get("id")) and "expected" in case
    return _verdict("agree" if ok else "disagree", ["case is well-formed" if ok else "case needs id and expected"],
                    method="evals.schema")


_AGREEMENT_PHRASE = {"agree": "agrees", "disagree": "disagrees", "unverified": "could not verify it"}


def rederive(engine: Engine, contribution_id: str) -> dict:
    """The relevant fleet re-reads the source with the proposal as evidence and
    reports agree / disagree / unverified with reasons. Writes nothing to a layer."""
    row = get(engine, contribution_id)
    if row is None:
        raise KeyError(contribution_id)
    if row["status"] not in ("checked", "rederived"):
        raise WrongStatus(f"{contribution_id} is {row['status']}; only checked contributions are re-derived")
    with engine.begin() as conn:
        kind, layer = row["kind"], row["layer"]
        if kind in ("correction", "translation") and layer == "L2":
            verdict = _rederive_l2_correction(conn, row) if kind == "correction" else _verdict(
                "unverified", ["translations are reviewed by jurisdiction reviewers"], method="l2.extract")
        elif (kind == "correction" and layer == "L3") or kind == "characteristic":
            verdict = _rederive_l3(conn, row)
        elif kind == "equivalence":
            verdict = _rederive_equivalence(conn, row)
        elif kind == "enforcement_link":
            verdict = _rederive_enforcement_link(conn, row)
        elif kind == "missing_source":
            verdict = _rederive_missing_source(conn, row)
        elif kind == "golden_case":
            verdict = _rederive_golden_case(conn, row)
        else:
            verdict = _verdict("unverified", [f"no automated re-derivation for {kind}; two reviewers decide"], method="human")
        conn.execute(contributions.update().where(contributions.c.id == contribution_id)
                     .values(rederivation=verdict, status="rederived", updated_at=_now()))
        _notify(conn, user_id=row["contributor_id"], contribution_id=contribution_id, kind="rederived",
                message=f"{contribution_id}: the fleet {_AGREEMENT_PHRASE[verdict['agreement']]} — {'; '.join(verdict['reasons'])}",
                payload=verdict)
    return verdict


def rederive_pending(engine: Engine) -> dict:
    """Nightly: every checked contribution gets its fleet verdict."""
    with engine.connect() as conn:
        ids = [r.id for r in conn.execute(sa.select(contributions.c.id).where(contributions.c.status == "checked"))]
    out = {"rederived": 0, "agree": 0, "disagree": 0, "unverified": 0}
    for cid in ids:
        v = rederive(engine, cid)
        out["rederived"] += 1
        out[v["agreement"]] = out.get(v["agreement"], 0) + 1
    return out


# --------------------------------------------------------------------------- review & accept


def _apply(conn: Connection, row: dict, *, approver: str, rationale: str) -> dict:
    """Acceptance goes through the record path — the same functions the console uses."""
    from app.clhear.platform import console

    kind, layer, proposed = row["kind"], row["layer"], row["proposed"]
    if kind == "correction" and layer == "L2":
        ob = _resolve_obligation(conn, row["target_ref"])
        if ob is None:
            raise KeyError(row["target_ref"])
        return console._apply_l2_field(conn, oid=ob["id"], field=row["field"], value=proposed.get("value"),
                                       approver=approver, proposal_id=row.get("proposal_id"), rationale=rationale)
    if kind == "correction" and layer == "L3":
        return console._apply_l3_field(conn, block_id=row["target_ref"], field=row["field"], value=proposed.get("value"),
                                       approver=approver, proposal_id=row.get("proposal_id"), rationale=rationale)
    if kind == "characteristic":
        return console._apply_l3_field(conn, block_id=row["target_ref"], field=f"characteristic:{proposed['key']}",
                                       value=proposed.get("value"), approver=approver, proposal_id=row.get("proposal_id"),
                                       rationale=rationale)
    if kind == "equivalence":
        a = _resolve_obligation(conn, str(proposed.get("obligation_a")))
        b = _resolve_obligation(conn, str(proposed.get("obligation_b")))
        if a is None or b is None:
            raise KeyError("obligation")
        sim = ((row.get("rederivation") or {}).get("derived") or {}).get("similarity")
        why = record.WhyTrail(layer="L2", subject_ref=a["id"],
                              reasoning_summary=f"community contribution {row['id']} accepted by {approver}: {rationale}"[:1000],
                              evidence_refs=[{"contribution": row["id"]}, *row["evidence"]],
                              inputs=(a["id"], b["id"], row["id"]), agent_id="community", skill_version=FLOW_VERSION,
                              confidence=1.0, input_layers=("L1",))
        out = record.write(conn, equivalences, {"id": next_id(conn, "EQV"), "obligation_a": a["id"], "obligation_b": b["id"],
                                                "basis": "human", "similarity": sim, "method": f"contribution:{row['id']}"},
                           why=why)
        return {"edge": "equivalence", "id": out.get("id"), "obligation_a": a["id"], "obligation_b": b["id"]}
    if kind == "enforcement_link":
        from app.clhear.l7.models import enforcement_links

        ev = _live_event(conn, row["target_ref"])
        ob = _resolve_obligation(conn, str(proposed.get("obligation_id")))
        if ev is None or ob is None:
            raise KeyError(row["target_ref"])
        quote = next((str(e.get("quote")) for e in row["evidence"] if isinstance(e, dict) and e.get("quote")), "")
        why = record.WhyTrail(layer="L7", subject_ref=ev["id"],
                              reasoning_summary=f"community contribution {row['id']} accepted by {approver}: {rationale}"[:1000],
                              evidence_refs=[{"contribution": row["id"]}, *row["evidence"]],
                              inputs=(ev["id"], ob["id"], row["id"]), agent_id="community", skill_version=FLOW_VERSION,
                              confidence=1.0, input_layers=("L1", "L2"))
        out = record.write(conn, enforcement_links, {"event_id": ev["id"], "obligation_id": ob["id"], "citation": quote[:500],
                                                     "method": "human", "event_text_hash": ev.get("text_hash") or ""}, why=why)
        return {"edge": "enforcement_link", "id": out.get("id"), "event_id": ev["id"], "obligation_id": ob["id"]}
    handoff = {
        "missing_source": "L1 starter-corpus backlog: adapter + rights basis, then ingest",
        "ontology_entry": "L4 curated ontology (curated/l4_ontology.json) on the next ontology build",
        "fill": "L8 fill generator seeds; endorsed on the next fill review",
        "translation": "L2 translation set for the jurisdiction reviewers' cluster",
        "golden_case": "exported to evals/contributed/<suite>.json in the public repo on release",
        "evidence_template": "conformance evidence templates (export/clhear/conformance/)",
    }
    return {"handoff": handoff.get(kind, "maintainers apply through the layer's curated data"), "writes_registry": False}


def _flip_proposal(engine: Engine, proposal_id: str | None, decision: str, approver: str) -> None:
    if not proposal_id:
        return
    p = l0_proposals.get_proposal(engine, proposal_id)
    if p and p.get("status") == "proposed":
        (l0_proposals.approve if decision == "approved" else l0_proposals.reject)(engine, proposal_id, approver)


def review(engine: Engine, contribution_id: str, *, reviewer_email: str, decision: str, note: str = "") -> dict:
    """One reviewer's decision. Two distinct accepts apply the contribution through the
    record path; one reject closes it. Authors cannot review their own work."""
    if decision not in REVIEW_DECISIONS:
        raise InvalidContribution(f"decision must be one of {REVIEW_DECISIONS}")
    reviewer = reviewer_email.strip().lower()
    row = get(engine, contribution_id)
    if row is None:
        raise KeyError(contribution_id)
    if row["status"] == "checked":
        rederive(engine, contribution_id)
        row = get(engine, contribution_id)
    if row["status"] != "rederived":
        raise WrongStatus(f"{contribution_id} is {row['status']}; reviews are taken on re-derived contributions")
    with engine.begin() as conn:
        if not (roles_for(conn, reviewer) & REVIEWER_ROLES):
            raise NotAReviewer(f"{reviewer} is not a reviewer")
        if reviewer == row["contributor_email"]:
            raise SelfReview("a contributor does not review their own contribution")
        rid = _ensure_user(conn, reviewer)
        seen = (row.get("rederivation") or {}).get("agreement", "")
        existing = conn.execute(sa.select(contribution_reviews.c.id).where(
            contribution_reviews.c.contribution_id == contribution_id, contribution_reviews.c.reviewer_id == rid)).first()
        if existing:
            conn.execute(contribution_reviews.update().where(contribution_reviews.c.id == existing.id)
                         .values(decision=decision, note=note[:2000], rederivation_seen=seen, created_at=_now()))
        else:
            conn.execute(contribution_reviews.insert().values(contribution_id=contribution_id, reviewer_id=rid,
                                                              reviewer_email=reviewer, decision=decision,
                                                              note=note[:2000], rederivation_seen=seen))
        accepts = [r.reviewer_email for r in conn.execute(
            sa.select(contribution_reviews.c.reviewer_email).where(contribution_reviews.c.contribution_id == contribution_id,
                                                                    contribution_reviews.c.decision == "accept"))]
        outcome = None
        if decision == "reject":
            conn.execute(contributions.update().where(contributions.c.id == contribution_id)
                         .values(status="rejected", updated_at=_now()))
            _notify(conn, user_id=row["contributor_id"], contribution_id=contribution_id, kind="rejected",
                    message=f"{contribution_id} was not accepted by {reviewer}: {note or 'no note'}", payload={"reviewer": reviewer})
            outcome = "rejected"
        elif decision == "request_changes":
            _notify(conn, user_id=row["contributor_id"], contribution_id=contribution_id, kind="changes_requested",
                    message=f"{reviewer} asks for changes on {contribution_id}: {note or 'no note'}", payload={"reviewer": reviewer})
        elif len(set(accepts)) >= REQUIRED_ACCEPTS:
            approver = "reviewers:" + ",".join(sorted(set(accepts)))
            rationale = f"community contribution {contribution_id} ({row['kind']}): {row.get('rationale') or ''}".strip(": ")
            applied = _apply(conn, row, approver=approver, rationale=rationale)
            conn.execute(contributions.update().where(contributions.c.id == contribution_id)
                         .values(status="accepted", applied=applied, accepted_at=_now(), updated_at=_now()))
            _notify(conn, user_id=row["contributor_id"], contribution_id=contribution_id, kind="accepted",
                    message=f"{contribution_id} was accepted by {len(set(accepts))} reviewers and will ship with the next release",
                    payload={"reviewers": sorted(set(accepts)), "applied": applied})
            outcome = "accepted"
    if outcome == "accepted":
        _flip_proposal(engine, row.get("proposal_id"), "approved", "reviewers:" + ",".join(sorted(set(accepts))))
    elif outcome == "rejected":
        _flip_proposal(engine, row.get("proposal_id"), "rejected", reviewer)
    return get(engine, contribution_id)


def review_from_console(engine: Engine, proposal: dict, *, decision: str, approver: str) -> dict:
    """The approval console's decision on a ``community_contribution`` proposal is one
    reviewer's vote — the two-reviewer rule still holds."""
    cid = (_json(proposal.get("draft"), {}) or {}).get("contribution_id")
    if not cid:
        raise KeyError("contribution_id")
    return review(engine, cid, reviewer_email=approver, decision="accept" if decision == "approved" else "reject",
                  note="decided in the approval console")


# --------------------------------------------------------------------------- release, attribution, impact


def _impact(conn: Connection, row: dict) -> dict:
    """What the accepted change reached: the current blueprints whose items rest on the
    touched obligation(s) / block, plus the obligations changed."""
    kind, layer, proposed = row["kind"], row["layer"], row["proposed"]
    ob_ids: set[str] = set()
    block_ids: set[str] = set()
    if kind in ("correction", "translation") and layer == "L2":
        ob = _resolve_obligation(conn, row["target_ref"])
        if ob:
            ob_ids |= {ob["id"], *([ob["stable_id"]] if ob.get("stable_id") else [])}
    elif kind == "equivalence":
        for key in ("obligation_a", "obligation_b"):
            ob = _resolve_obligation(conn, str(proposed.get(key)))
            if ob:
                ob_ids |= {ob["id"], *([ob["stable_id"]] if ob.get("stable_id") else [])}
    elif kind == "enforcement_link":
        ob = _resolve_obligation(conn, str(proposed.get("obligation_id")))
        if ob:
            ob_ids |= {ob["id"], *([ob["stable_id"]] if ob.get("stable_id") else [])}
    elif layer == "L3" or kind in ("characteristic", "fill"):
        block_ids.add(row["target_ref"])
    touched: set[str] = set()
    if ob_ids or block_ids:
        current = {r.stable_id for r in conn.execute(sa.select(blueprints.c.stable_id).where(blueprints.c.status == "current"))
                   if r.stable_id}
        for it in conn.execute(sa.select(blueprint_items.c.blueprint_id, blueprint_items.c.block_id,
                                         blueprint_items.c.obligations_satisfied).where(blueprint_items.c.valid_to.is_(None))):
            if it.blueprint_id not in current:
                continue
            sat = set(_json(it.obligations_satisfied, []) or [])
            if it.block_id in block_ids or (sat & ob_ids):
                touched.add(it.blueprint_id)
    distinct_obs = {o for o in ob_ids if o.startswith("OBL-")} or ob_ids
    return {"blueprints_changed": len(touched), "blueprints": sorted(touched)[:50],
            "obligations_changed": len(distinct_obs), "blocks_changed": len(block_ids)}


def release_contributions(engine: Engine, release_id: str) -> list[dict]:
    """Ship every accepted contribution in ``release_id``: status → released, impact
    computed, contributor notified. Returns the attribution list for the release notes."""
    shipped: list[dict] = []
    with engine.begin() as conn:
        rows = [dict(r) for r in conn.execute(sa.select(contributions).where(contributions.c.status == "accepted")
                                              .order_by(contributions.c.accepted_at)).mappings()]
        names = {r.id: (r.display_name or r.email) for r in conn.execute(sa.select(users.c.id, users.c.display_name, users.c.email))}
        for row in rows:
            row = _plain(row)
            impact = _impact(conn, row)
            conn.execute(contributions.update().where(contributions.c.id == row["id"])
                         .values(status="released", released_in=release_id, impact=impact, updated_at=_now()))
            n = impact["blueprints_changed"]
            message = (f"Your {row['kind'].replace('_', ' ')} {row['id']} shipped in release {release_id}: "
                       f"it changed {n} blueprint{'s' if n != 1 else ''}"
                       + (f" and {impact['obligations_changed']} obligation{'s' if impact['obligations_changed'] != 1 else ''}"
                          if impact["obligations_changed"] else "") + ".")
            _notify(conn, user_id=row["contributor_id"], contribution_id=row["id"], kind="released",
                    message=message, payload={"release": release_id, "impact": impact})
            shipped.append({"contribution_id": row["id"], "kind": row["kind"], "layer": row["layer"],
                            "target_ref": row["target_ref"], "field": row["field"],
                            "contributor": names.get(row["contributor_id"], row["contributor_email"]),
                            "impact": impact, "release": release_id})
    return shipped


def attribution(engine: Engine, release_id: str) -> list[dict]:
    with engine.connect() as conn:
        names = {r.id: (r.display_name or r.email) for r in conn.execute(sa.select(users.c.id, users.c.display_name, users.c.email))}
        rows = conn.execute(sa.select(contributions).where(contributions.c.released_in == release_id)
                            .order_by(contributions.c.accepted_at)).mappings()
        return [{"contribution_id": r["id"], "kind": r["kind"], "layer": r["layer"], "target_ref": r["target_ref"],
                 "field": r["field"], "contributor": names.get(r["contributor_id"], r["contributor_email"]),
                 "impact": _json(r["impact"], {}), "release": release_id} for r in rows]


# --------------------------------------------------------------------------- read side


def get(engine: Engine, contribution_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(contributions).where(contributions.c.id == contribution_id)).mappings().first()
        if row is None:
            return None
        out = _plain(row)
        out["reviews"] = [_plain(r) for r in conn.execute(
            sa.select(contribution_reviews).where(contribution_reviews.c.contribution_id == contribution_id)
            .order_by(contribution_reviews.c.created_at)).mappings()]
        out["accepts"] = sum(1 for r in out["reviews"] if r["decision"] == "accept")
        out["accepts_required"] = REQUIRED_ACCEPTS
        name = conn.execute(sa.select(users.c.display_name).where(users.c.id == row["contributor_id"])).scalar()
        out["contributor"] = name or out["contributor_email"]
    return out


def list_contributions(engine: Engine, *, status: str | None = None, kind: str | None = None,
                       contributor: str | None = None, layer: str | None = None, limit: int = 200) -> list[dict]:
    q = sa.select(contributions).order_by(contributions.c.created_at.desc()).limit(limit)
    if status:
        q = q.where(contributions.c.status == status)
    if kind:
        q = q.where(contributions.c.kind == kind)
    if layer:
        q = q.where(contributions.c.layer == layer.upper())
    if contributor:
        q = q.where(contributions.c.contributor_id == user_id_for(contributor))
    with engine.connect() as conn:
        rows = [_plain(r) for r in conn.execute(q).mappings()]
        names = {r.id: (r.display_name or r.email) for r in conn.execute(sa.select(users.c.id, users.c.display_name, users.c.email))}
        counts = {r.contribution_id: r.n for r in conn.execute(
            sa.select(contribution_reviews.c.contribution_id, sa.func.count().label("n"))
            .where(contribution_reviews.c.decision == "accept").group_by(contribution_reviews.c.contribution_id))}
    for r in rows:
        r["contributor"] = names.get(r["contributor_id"], r["contributor_email"])
        r["accepts"] = counts.get(r["id"], 0)
        r["accepts_required"] = REQUIRED_ACCEPTS
    return rows


def notifications(engine: Engine, email: str, *, unread_only: bool = False, limit: int = 100) -> list[dict]:
    q = (sa.select(contributor_notifications).where(contributor_notifications.c.user_id == user_id_for(email))
         .order_by(contributor_notifications.c.created_at.desc()).limit(limit))
    if unread_only:
        q = q.where(contributor_notifications.c.read_at.is_(None))
    with engine.connect() as conn:
        return [_plain(r) for r in conn.execute(q).mappings()]


def mark_read(engine: Engine, email: str) -> int:
    with engine.begin() as conn:
        return conn.execute(contributor_notifications.update()
                            .where(contributor_notifications.c.user_id == user_id_for(email),
                                   contributor_notifications.c.read_at.is_(None)).values(read_at=_now())).rowcount


def _score(accepted: int, released: int, blueprints: int, obligations_n: int) -> int:
    """Reputation by accepted impact: every acceptance counts, every blueprint reached counts more."""
    return accepted * 10 + released * 5 + blueprints * 2 + obligations_n


def leaderboard(engine: Engine, *, limit: int = 50) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(contributions.c.contributor_id, contributions.c.contributor_email, contributions.c.status,
                                      contributions.c.impact).where(contributions.c.status.in_(("accepted", "released")))).all()
        names = {r.id: (r.display_name or r.email) for r in conn.execute(sa.select(users.c.id, users.c.display_name, users.c.email))}
        reviewer_ids = {r.user_id for r in conn.execute(sa.select(roles.c.user_id).where(roles.c.role.in_(("reviewer", "maintainer", "steering")),
                                                                                       roles.c.valid_to.is_(None)))}
    agg: dict[str, dict] = {}
    for r in rows:
        slot = agg.setdefault(r.contributor_id, {"contributor": names.get(r.contributor_id, r.contributor_email),
                                                 "accepted": 0, "released": 0, "blueprints_changed": 0,
                                                 "obligations_changed": 0, "recognized_reviewer": r.contributor_id in reviewer_ids})
        slot["accepted"] += 1
        if r.status == "released":
            slot["released"] += 1
            imp = _json(r.impact, {}) or {}
            slot["blueprints_changed"] += int(imp.get("blueprints_changed") or 0)
            slot["obligations_changed"] += int(imp.get("obligations_changed") or 0)
    board = sorted(agg.values(), key=lambda s: (-_score(s["accepted"], s["released"], s["blueprints_changed"], s["obligations_changed"]),
                                               s["contributor"]))
    for i, s in enumerate(board, 1):
        s["rank"] = i
        s["score"] = _score(s["accepted"], s["released"], s["blueprints_changed"], s["obligations_changed"])
    return board[:limit]


def profile(engine: Engine, email: str) -> dict:
    email = email.strip().lower()
    board = leaderboard(engine, limit=100000)
    with engine.connect() as conn:
        name = conn.execute(sa.select(users.c.display_name).where(users.c.id == user_id_for(email))).scalar()
        signed = cla_signed(conn, email)
        my_roles = sorted(roles_for(conn, email))
        mine = list_contributions(engine, contributor=email, limit=500)
    entry = next((s for s in board if s["contributor"] in (name, email)), None)
    return {"contributor": name or email, "roles": my_roles, "cla": {"signed": bool(signed), "version": CLA_VERSION,
                                                                    "signed_at": signed["signed_at"] if signed else None},
            "rank": entry["rank"] if entry else None, "score": entry["score"] if entry else 0,
            "accepted": entry["accepted"] if entry else 0, "released": entry["released"] if entry else 0,
            "blueprints_changed": entry["blueprints_changed"] if entry else 0,
            "obligations_changed": entry["obligations_changed"] if entry else 0,
            "contributions": [{k: c[k] for k in ("id", "kind", "layer", "target_ref", "status", "released_in", "impact", "created_at")}
                              for c in mine]}


def summary(engine: Engine) -> dict:
    with engine.connect() as conn:
        by_status = {r.status: r.n for r in conn.execute(sa.select(contributions.c.status, sa.func.count().label("n"))
                                                         .group_by(contributions.c.status))}
        contributors = conn.execute(sa.select(sa.func.count(sa.distinct(contributions.c.contributor_id)))).scalar() or 0
        signed = conn.execute(sa.select(sa.func.count()).select_from(cla_signatures)).scalar() or 0
        reviewers = conn.execute(sa.select(sa.func.count(sa.distinct(roles.c.user_id))).where(
            roles.c.role.in_(("reviewer", "maintainer", "steering")), roles.c.valid_to.is_(None))).scalar() or 0
    return {"by_status": by_status, "contributors": contributors, "cla_signatures": signed, "reviewers": reviewers,
            "accepts_required": REQUIRED_ACCEPTS, "flow": FLOW_VERSION, "cla_version": CLA_VERSION}
