# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L0 materializes the owner's private FINRA exception; L1 only requests bindings.

The reviewed scope is the existing FINRA discovery contract, never an arbitrary
URL or a licence grant. No publisher bytes are acquired by this coordinator.
"""
import os
import re
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.l1 import discovery, inventory, operator_exceptions as exceptions
from app.clhear.platform import events

AUTHORIZATION_COMMAND = "finra-private-review-owner-authorization-2026-09-16"


def manifest():
    from app.clhear.l1.source_registry import S, source_role
    seeds = [{"url": inventory._url(url), "source_key": f"finra/catalog/{key}", "category": key}
             for key, _, url in inventory.finra_seed_categories()]
    profile = {"scope_version": inventory.SCOPE_VERSION, "boundaries": inventory.FINRA_BOUNDARIES}
    registered = [{"source_key": e["key"], "canonical_url": inventory._url(e["canonical_url"]),
                   "source_role": source_role(e["key"])}
                  for e in S if e["key"].startswith("finra/")]
    initial = [{"source_key": s["source_key"], "canonical_url": s["url"], "source_role": "collection"}
               for s in seeds] + registered
    return {"profile_hash": discovery._hash({"profile": profile, "seeds": seeds}),
            "manifest_hash": discovery._hash({"profile": profile, "seeds": seeds, "registered": registered}),
            "scope_version": inventory.SCOPE_VERSION, "sources": initial}


def _l0():
    if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
        raise PermissionError("Only L0 may materialize FINRA operator exception bindings")


def bootstrap(engine):
    """One owner-merged authorization, not an automatic grant on every deploy."""
    _l0()
    revision = os.environ.get("CLHEAR_CODE_REVISION", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        return {"status": "not_configured", "reason": "owner_merged_revision_required"}
    contract = manifest()
    with engine.begin() as conn:
        previous = exceptions.latest_event(conn, exceptions.FINRA_EXCEPTION_ID)
        if previous is None:
            previous = exceptions.record_exception(conn, exception_id=exceptions.FINRA_EXCEPTION_ID,
                command_id=AUTHORIZATION_COMMAND, action="activate", approved_by="Reg42-ai",
                evidence_ref=f"https://github.com/Reg42-ai/CLHEAR-MVP/commit/{revision}",
                rationale="Owner-authorized private FINRA review until revoked; publisher permission remains unverified.")
        active = exceptions.latest_active(conn, exceptions.FINRA_EXCEPTION_ID)
        if active is None:
            return {"status": "revoked", "reactivated": False}
        bound = exceptions.bind_sources(conn, exception_id=exceptions.FINRA_EXCEPTION_ID,
            activation_id=active["id"], manifest_hash=contract["manifest_hash"],
            scope_version=contract["scope_version"], sources=contract["sources"], bound_by="l0.deployment_bootstrap")
    return {"status": "active", "exception_id": exceptions.FINRA_EXCEPTION_ID,
            "manifest_hash": contract["manifest_hash"], "bound_sources": len(bound),
            "publisher_permission_verified": False, "release_eligible": False}


def request_frontier_bindings(engine, cycle_id):
    """L1 queues exact persisted metadata for L0, without writing an approval."""
    with engine.begin() as conn:
        cycle = conn.execute(sa.select(discovery.cycles).where(discovery.cycles.c.id == cycle_id)).mappings().one()
        active = exceptions.latest_active(conn)
        if cycle["publisher_id"] != "finra" or active is None:
            return
        rows = conn.execute(sa.select(discovery.pages).where(discovery.pages.c.cycle_id == cycle_id,
            discovery.pages.c.status.in_(["pending", "permission_blocked", "exception_scope_blocked"]))).mappings().all()
        contract = manifest()
        from app.clhear.l1 import permissions
        waiting, denied = [], []
        for row in rows:
            if (row["status"] == "exception_scope_blocked"
                    and row["result"].get("denied_activation_id") == active["id"]
                    and row["result"].get("denied_manifest_hash") == contract["manifest_hash"]):
                continue
            if exceptions.decision(conn, row["source_key"], "acquire", canonical_url=row["url"])["allowed"]:
                continue
            # A genuine publisher denial stays a permission finding of its own; it is
            # never queued for an exception binding and never mixed with "no evidence".
            if permissions.decision(conn, row["source_key"], "acquire").get("reason") == "not_approved":
                denied.append(row)
                continue
            waiting.append(row["id"])
        for row in denied:
            findings = [f for f in (row["result"] or {}).get("findings", []) if f.get("code") != "publisher_permission_denied"]
            findings.append({"code": "publisher_permission_denied",
                             "detail": "The publisher explicitly denied acquisition; the operator exception does not apply."})
            conn.execute(discovery.pages.update().where(discovery.pages.c.id == row["id"]).values(
                status="permission_blocked", result={**(row["result"] or {}), "findings": findings, "publisher_denied": True}))
        if waiting:
            conn.execute(discovery.pages.update().where(discovery.pages.c.id.in_(waiting)).values(status="awaiting_exception_binding"))
        # Enumerated leaves are inserted directly as awaiting; they need the
        # same L0 request even when no fetched page is waiting.
        already_waiting = conn.execute(sa.select(sa.func.count()).select_from(discovery.pages).where(
            discovery.pages.c.cycle_id == cycle_id, discovery.pages.c.status == "awaiting_exception_binding")).scalar_one()
        if waiting or already_waiting:
            # One outstanding request per discovery cycle: a second batch that finds
            # more waiting pages while the first request is unrelayed does not add another.
            outstanding = conn.execute(sa.select(events.events.c.id).where(
                events.events.c.kind == "L1ExceptionBindingsRequested", events.events.c.subject_ref == cycle_id,
                events.events.c.relayed_at.is_(None))).first()
            if outstanding is None:
                events.emit(conn, layer="l0", kind="L1ExceptionBindingsRequested", subject_ref=cycle_id,
                            payload={"discovery_cycle_id": cycle_id}, producer="l1.discovery")


def bind_frontier(engine, cycle_id):
    """L0 verifies the saved frontier against the approved discovery contract."""
    _l0()
    contract = manifest()
    with engine.begin() as conn:
        cycle = conn.execute(sa.select(discovery.cycles).where(discovery.cycles.c.id == cycle_id)).mappings().one()
        if cycle["publisher_id"] != "finra" or cycle["profile_hash"] != contract["profile_hash"]:
            raise ValueError("Discovery frontier does not match the reviewed FINRA manifest")
        active = exceptions.latest_active(conn)
        rows = conn.execute(sa.select(discovery.pages).where(discovery.pages.c.cycle_id == cycle_id,
            discovery.pages.c.status == "awaiting_exception_binding")).mappings().all()
        bound, blocked = 0, 0
        for row in rows:
            if active is not None:
                try:
                    exceptions.bind_sources(conn, exception_id=exceptions.FINRA_EXCEPTION_ID,
                        activation_id=active["id"], manifest_hash=contract["manifest_hash"],
                        scope_version=contract["scope_version"], sources=[{
                            "source_key": row["source_key"], "canonical_url": row["url"], "source_role": row["role"]}],
                        bound_by="l0.discovery_frontier")
                    if (row["result"] or {}).get("terminal"):
                        # Enumerated from its index; the import fetches it, discovery never does.
                        conn.execute(discovery.pages.update().where(discovery.pages.c.id == row["id"]).values(
                            status="checked", checked_at=datetime.now(timezone.utc)))
                    else:
                        conn.execute(discovery.pages.update().where(discovery.pages.c.id == row["id"]).values(
                            status="pending", result={}, checked_at=None))
                    bound += 1
                    continue
                except ValueError:
                    pass  # Unsupported identities stay explicit gaps, never broad grants.
            conn.execute(discovery.pages.update().where(discovery.pages.c.id == row["id"]).values(
                status="exception_scope_blocked", result={"denied_activation_id": active["id"] if active else None,
                    "denied_manifest_hash": contract["manifest_hash"], "findings": [{"code": "operator_exception_binding_denied",
                    "detail": "The source has no active binding in the reviewed FINRA scope."}]}))
            blocked += 1
    return {"discovery_cycle_id": cycle_id, "bound": bound, "blocked": blocked,
            "manifest_hash": contract["manifest_hash"], "release_eligible": False}
