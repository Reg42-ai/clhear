# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Explicit private-POC permission, inventory-scope and artifact reviews.

This is an operator-authorized grant for the user-protected live instance, not
a publisher licence. Activate records ``display_public`` so signed-in /l1 can
show imported protected text. Revoke writes an ``approved=False`` replacement
and requests a viewer refresh. Every row is recorded through
``L1EvidenceReviewRecorded``.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear.platform.events import Envelope
from app.clhear.settings import get_settings

POC_APPROVED_BY = "owner: private POC test environment"
POC_GRANT = {
    "acquire": True, "store": True, "parse": True, "display_internal": True,
    "display_public": True,
}


def enabled() -> bool:
    """True on the live instance when every current L1 source should be fetched."""
    return bool(get_settings().clhear_private_completeness)


def protected_source_keys() -> list[str]:
    from app.clhear.l1.permissions import required_for
    from app.clhear.l1.source_registry import S

    return sorted({entry["key"] for entry in S if required_for(entry)})


def _event_id(*parts: str) -> str:
    return "poc-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def _dispatch(engine: Engine, *, event_id: str, kind: str, subject_ref: str, payload: dict):
    from app.clhear import workers

    envelope = Envelope(
        event_id=event_id, layer="l0", kind="L1EvidenceReviewRecorded",
        subject_ref=subject_ref, payload=payload, producer="operator.poc",
        ts=datetime.now(timezone.utc).isoformat(),
    )
    # L0 (and tests with fleet=all) go through the delivery ledger. L1 workers
    # record the same review in-process so completeness does not wait on a
    # cross-fleet hop.
    fleet = os.environ.get("CLHEAR_FLEET", "all").lower()
    if fleet in {"l0", "all"}:
        return workers.handle_envelope(engine, None, envelope.model_dump_json())
    return workers.handle_l1_evidence_review(engine, None, envelope)


def apply_private_review(engine: Engine, action: str, evidence_ref: str, *,
                         verification_id: str, approved_by: str = POC_APPROVED_BY) -> dict:
    """Activate or revoke POC permissions for every protected registry source."""
    if action not in {"activate", "revoke"}:
        raise ValueError("action must be activate or revoke")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("evidence_ref is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", verification_id or ""):
        raise ValueError("verification_id is required")
    approved = action == "activate"
    permissions = dict(POC_GRANT if approved else {key: False for key in POC_GRANT})
    keys = protected_source_keys()
    recorded, skipped = [], []
    for source_key in keys:
        event_id = _event_id(verification_id, action, source_key, evidence_ref.strip())
        result = _dispatch(engine, event_id=event_id, kind="permissions", subject_ref=source_key, payload={
            "review_kind": "permissions", "source_key": source_key, "permissions": permissions,
            "evidence_ref": evidence_ref.strip(), "approved_by": approved_by, "approved": approved,
        })
        (skipped if result is None else recorded).append(source_key)
    return {
        "status": "recorded", "action": action, "verification_id": verification_id,
        "approved_by": approved_by, "evidence_ref": evidence_ref.strip(),
        "sources": keys, "recorded": recorded, "already_recorded": skipped,
        "display_public": bool(permissions.get("display_public")), "acceptance": "not_claimed",
    }


def approve_inventory(engine: Engine, inventory_hash: str, *, verification_id: str,
                      evidence_ref: str = "poc:private-scope-review",
                      approved_by: str = POC_APPROVED_BY, approved: bool = True) -> dict:
    """Record a scope review for one frozen inventory hash."""
    if not re.fullmatch(r"[a-f0-9]{64}", inventory_hash or ""):
        raise ValueError("inventory_hash must identify one frozen SHA-256 inventory")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", verification_id or ""):
        raise ValueError("verification_id is required")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ValueError("evidence_ref is required")
    event_id = _event_id(verification_id, "scope", inventory_hash, evidence_ref.strip())
    result = _dispatch(engine, event_id=event_id, kind="scope", subject_ref="inventory", payload={
        "review_kind": "scope", "inventory_hash": inventory_hash,
        "evidence_ref": evidence_ref.strip(), "approved_by": approved_by, "approved": approved,
    })
    return {
        "status": "already_recorded" if result is None else "recorded",
        "verification_id": verification_id, "inventory_hash": inventory_hash,
        "approved": approved, "approved_by": approved_by,
        "evidence_ref": evidence_ref.strip(), "acceptance": "not_claimed",
        "record": None if result is None else result.get("record"),
    }


def approve_artifact(engine: Engine, source_key: str, content_hash: str, *,
                     publisher_edition: str, canonical_url: str, verification_id: str,
                     evidence_ref: str = "poc:private-completeness-artifact",
                     approved_by: str = POC_APPROVED_BY) -> dict:
    """Bind acquired restricted-file bytes to the declared edition."""
    if not re.fullmatch(r"[a-f0-9]{64}", content_hash or ""):
        raise ValueError("content_hash must identify the acquired artifact set")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", verification_id or ""):
        raise ValueError("verification_id is required")
    event_id = _event_id(verification_id, "artifact", source_key, content_hash)
    result = _dispatch(engine, event_id=event_id, kind="artifact", subject_ref=source_key, payload={
        "review_kind": "artifact", "source_key": source_key, "content_hash": content_hash,
        "publisher_edition": publisher_edition, "canonical_url": canonical_url,
        "coverage": "full", "evidence_ref": evidence_ref.strip(),
        "approved_by": approved_by, "approved": True,
    })
    return {
        "status": "already_recorded" if result is None else "recorded",
        "verification_id": verification_id, "source_key": source_key,
        "content_hash": content_hash, "approved": True, "approved_by": approved_by,
        "record": None if result is None else result.get("record"),
    }


def record_restricted_artifact(engine: Engine, meta, content_hash: str, *, verification_id: str) -> dict | None:
    """Best-effort artifact identity after a successful restricted_file ingest."""
    if not enabled() or not content_hash or getattr(meta, "adapter", "") != "restricted_file":
        return None
    url = str(getattr(meta, "canonical_url", "") or "")
    if not url.startswith("https://"):
        return None
    from app.clhear.l1.inventory import EXPECTED_EDITIONS
    edition = EXPECTED_EDITIONS.get(meta.source_key) or str(getattr(meta, "name", "") or meta.source_key)
    try:
        return approve_artifact(
            engine, meta.source_key, content_hash, publisher_edition=edition,
            canonical_url=url, verification_id=verification_id,
        )
    except Exception:  # noqa: BLE001 — ingest already succeeded
        return None
