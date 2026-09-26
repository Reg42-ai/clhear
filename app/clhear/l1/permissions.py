# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Explicit, operation-specific permission evidence for protected L1 sources.

This ledger supplements ``rights_records``; it does not change a source's
licence or make private text public. Only trusted worker/administrative code
may call ``record_permission`` after reviewing the actual permission evidence.
There is deliberately no HTTP endpoint, upload inference, or BYOL shortcut.

Each append is a complete replacement permission snapshot for one exact source
key. The latest effective snapshot wins, including a denial or expired grant;
an older approval never revives after its replacement expires. Training is
denied unless explicitly included in an approved grant, just like every other
operation. This module records the decision; it cannot authenticate a licence.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlparse

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.l1.models import BigId, Json, L1_SCHEMA

OPERATIONS = (
    "acquire", "store", "parse", "embed", "infer", "train", "derive", "translate",
    "display_internal", "display_public", "redistribute",
)
_SOURCE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}\Z")
metadata = sa.MetaData(schema=L1_SCHEMA)
source_permissions = sa.Table(
    "source_permissions", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    # No foreign key: permission must be recordable before acquisition begins.
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("permissions", Json, nullable=False, default=dict),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("approved", sa.Boolean, nullable=False, default=False, server_default=sa.false()),
    sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True)),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("length(trim(source_key)) > 0", name="source_permissions_source_check"),
    sa.CheckConstraint("length(trim(evidence_ref)) > 0", name="source_permissions_evidence_check"),
    sa.CheckConstraint("length(trim(approved_by)) > 0", name="source_permissions_owner_check"),
    sa.CheckConstraint("expires_at IS NULL OR expires_at > valid_from", name="source_permissions_period_check"),
)


def _field(meta, name: str, default=""):
    if isinstance(meta, Mapping):
        return meta.get(name, default)
    return getattr(meta, name, default)


def required_for(meta) -> bool:
    """Require explicit grants for FINRA and restricted sources/ingestors.

    Known protected namespaces remain protected even if adapter/licence labels
    change. There are no test, demo, host, or synthetic-namespace exemptions.
    """
    key = str(_field(meta, "source_key", _field(meta, "key"))).lower()
    adapter = str(_field(meta, "adapter")).lower()
    host = (urlparse(str(_field(meta, "canonical_url"))).hostname or "").lower()
    return (
        key.split("/", 1)[0] in {"finra", "iso", "aicpa", "pci", "ifrs"}
        or adapter in {"finra", "finra_enforcement", "restricted_file"}
        or host == "finra.org" or host.endswith(".finra.org")
        or str(_field(meta, "license")).lower() == "restricted"
    )


def _timestamp(value, *, field: str, database: bool = False) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp with a timezone") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a timestamp with a timezone")
    if value.tzinfo is None or value.utcoffset() is None:
        if not database:
            raise ValueError(f"{field} must include a timezone")
        # SQLite drops timezone information from our UTC DateTime columns.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _key(value) -> str:
    if not isinstance(value, str) or not _SOURCE_KEY.fullmatch(value):
        raise ValueError("source_key must be one exact, nonempty source key, without wildcards")
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _permissions(value) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("permissions must be an object containing supported operations")
    if any(key not in OPERATIONS for key in value):
        raise ValueError("permissions contains an unsupported operation")
    if any(type(allowed) is not bool for allowed in value.values()):
        raise ValueError("each permission must be a boolean")
    return {operation: value.get(operation, False) for operation in OPERATIONS}


def _plain(row) -> dict:
    out = dict(row)
    for field in ("valid_from", "expires_at", "recorded_at"):
        if out.get(field) is not None:
            out[field] = _timestamp(out[field], field=field, database=True).isoformat()
    return out


def record_permission(
    conn: Connection | Engine, *, source_key: str, permissions: dict[str, bool],
    evidence_ref: str, approved_by: str, approved: bool,
    valid_from: datetime | str | None = None, expires_at: datetime | str | None = None,
) -> dict:
    """Append a reviewed permission snapshot from trusted worker code only.

    ``approved=False`` explicitly denies all operations from its effective date.
    Omitted operation flags are false, never inherited from an earlier grant.
    An Engine owns its transaction; a Connection uses the caller's transaction.
    No source row is required or changed and no evidence URL is fetched.
    """
    if type(approved) is not bool:
        raise ValueError("approved must be an explicit boolean")
    start = _timestamp(valid_from if valid_from is not None else datetime.now(timezone.utc), field="valid_from")
    end = _timestamp(expires_at, field="expires_at") if expires_at is not None else None
    if end is not None and end <= start:
        raise ValueError("expires_at must be after valid_from")
    values = {
        "source_key": _key(source_key), "permissions": _permissions(permissions),
        "evidence_ref": _text(evidence_ref, "evidence_ref"),
        "approved_by": _text(approved_by, "approved_by"), "approved": approved,
        "valid_from": start, "expires_at": end,
    }
    if isinstance(conn, Engine):
        with conn.begin() as transaction:
            return _record(transaction, values)
    return _record(conn, values)


def _record(conn: Connection, values: dict) -> dict:
    row = conn.execute(source_permissions.insert().values(**values).returning(source_permissions)).mappings().one()
    return _plain(row)


def decision(conn: Connection, source_key: str, operation: str, now: datetime | str | None = None) -> dict:
    """Read a fail-closed decision for an exact source and one operation.

    Does not infer permission from source existence, purchase, authentication,
    rights_basis, file presence, or another operation. Database failures surface
    as errors rather than permitting access.
    """
    out = {
        "allowed": False, "reason": "missing_permission", "source_key": source_key,
        "operation": operation, "evidence_ref": None, "permission_id": None,
    }
    if operation not in OPERATIONS:
        return {**out, "reason": "unsupported_operation"}
    try:
        _key(source_key)
        at = _timestamp(now if now is not None else datetime.now(timezone.utc), field="now")
    except ValueError:
        return {**out, "reason": "invalid_request"}
    query = sa.select(source_permissions).where(source_permissions.c.source_key == source_key)
    row = conn.execute(query.where(source_permissions.c.valid_from <= at)
                       .order_by(source_permissions.c.id.desc()).limit(1)).mappings().first()
    if row is None:
        future = conn.execute(query.limit(1)).first()
        return {**out, "reason": "not_yet_valid" if future else "missing_permission"}
    out.update({
        "permission_id": row["id"], "evidence_ref": row["evidence_ref"],
        "approved_by": row["approved_by"],
        "valid_from": _timestamp(row["valid_from"], field="valid_from", database=True).isoformat(),
        "expires_at": _timestamp(row["expires_at"], field="expires_at", database=True).isoformat() if row["expires_at"] else None,
    })
    try:
        flags = _permissions(row["permissions"])
        _text(row["evidence_ref"], "evidence_ref")
        _text(row["approved_by"], "approved_by")
    except ValueError:
        return {**out, "reason": "invalid_permission"}
    if not row["approved"]:
        return {**out, "reason": "not_approved"}
    if row["expires_at"] is not None and _timestamp(row["expires_at"], field="expires_at", database=True) <= at:
        return {**out, "reason": "expired"}
    if not flags[operation]:
        return {**out, "reason": "operation_not_granted"}
    return {**out, "allowed": True, "reason": "explicit_permission"}


def candidate_decision(conn: Connection, source_key: str, operation: str,
                       now: datetime | str | None = None, *, canonical_url: str | None = None) -> dict:
    """Private candidate use only. Strict publisher permissions remain separate.

    This helper must never authorize a release, public export or model use via
    an operator exception. A namespaced permission_id is a comparison token,
    not a source_permissions row or a claim of publisher authorization.
    """
    strict = decision(conn, source_key, operation, now=now)
    if strict["allowed"]:
        return {**strict, "authority_type": "publisher_permission", "release_eligible": True}
    if strict.get("reason") == "not_approved":
        # A publisher's explicit denial is a decision, not an absence of evidence.
        # No operator exception may stand in for it, even for private review.
        return {**strict, "authority_type": "publisher_permission", "release_eligible": False, "denied": True}
    from app.clhear.l1 import operator_exceptions
    if operation in operator_exceptions.OPERATIONS:
        candidate = operator_exceptions.decision(conn, source_key, operation, now=now, canonical_url=canonical_url)
        if candidate["allowed"]:
            return {**candidate, "publisher_permission": strict}
    return {**strict, "authority_type": "publisher_permission", "release_eligible": False}


__all__ = ["OPERATIONS", "decision", "candidate_decision", "record_permission", "required_for", "source_permissions"]
