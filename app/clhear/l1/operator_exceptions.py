# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Owner decisions for private FINRA review, never publisher permission.

L0 alone records these append-only events and binds identities from its reviewed
manifest/frontier. An activation has no expiry; an explicit revocation ends it.
Reactivation requires a new command and new source bindings. No source rights,
originals or permission grants are changed. Acceptance must remain blocked.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l1.models import BigId, L1_SCHEMA

FINRA_EXCEPTION_ID = "finra-private-review"
OPERATIONS = frozenset({"acquire", "store", "parse", "display_internal"})
metadata = sa.MetaData(schema=L1_SCHEMA)
exception_events = sa.Table(
    "l1_operator_exceptions", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("exception_id", sa.Text, nullable=False, index=True),
    sa.Column("command_id", sa.Text, nullable=False, unique=True),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("rationale", sa.Text, nullable=False),
    sa.Column("event_hash", sa.Text, nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("action IN ('activate', 'revoke')", name="operator_exception_action_check"),
    sa.CheckConstraint("exception_id = 'finra-private-review'", name="operator_exception_finra_check"),
)
source_bindings = sa.Table(
    "l1_operator_exception_sources", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("activation_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.l1_operator_exceptions.id"), nullable=False, index=True),
    sa.Column("exception_id", sa.Text, nullable=False),
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("canonical_url", sa.Text, nullable=False),
    sa.Column("source_role", sa.Text, nullable=False),
    sa.Column("manifest_hash", sa.Text, nullable=False),
    sa.Column("scope_version", sa.Text, nullable=False),
    sa.Column("bound_by", sa.Text, nullable=False),
    sa.Column("binding_hash", sa.Text, nullable=False, unique=True),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("source_role IN ('document', 'collection')", name="operator_exception_source_role_check"),
    sa.UniqueConstraint("activation_id", "source_key", "canonical_url", "manifest_hash", name="operator_exception_source_identity_unique"),
)
TABLES = (exception_events, source_bindings)
_EVENT_FIELDS = ("exception_id", "command_id", "action", "approved_by", "evidence_ref", "rationale")
_BINDING_FIELDS = ("activation_id", "exception_id", "source_key", "canonical_url", "source_role", "manifest_hash", "scope_version", "bound_by")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{name} must be nonempty text of at most 4096 characters")
    return value.strip()


def _exception_id(value):
    if value != FINRA_EXCEPTION_ID:
        raise ValueError("Only the reviewed FINRA private-review exception is supported")
    return value


def _instant(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be an aware UTC-compatible timestamp")
    return value.astimezone(timezone.utc)


def _plain(row):
    result = dict(row)
    if isinstance(result.get("recorded_at"), datetime):
        value = result["recorded_at"]
        result["recorded_at"] = (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()
    return result


def _available(conn):
    schema = None if conn.dialect.name == "sqlite" else L1_SCHEMA
    inspector = sa.inspect(conn)
    return all(inspector.has_table(table.name, schema=schema) for table in TABLES)


def _lock(conn, exception_id):
    if conn.dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(("operator-exception:" + exception_id).encode()).digest()[:8], "big", signed=True)
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _insert_once(conn, table, values, conflict):
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    conn.execute(insert(table).values(**values).on_conflict_do_nothing(index_elements=conflict))


def _valid_event(row):
    try:
        return (_exception_id(row["exception_id"]) and row["action"] in {"activate", "revoke"}
                and all(_text(row[k], k) == row[k] for k in _EVENT_FIELDS)
                and row["event_hash"] == _hash({k: row[k] for k in _EVENT_FIELDS}))
    except (ValueError, KeyError, TypeError):
        return False


def latest_event(conn, exception_id=FINRA_EXCEPTION_ID, *, now=None):
    """Latest recorded action; missing migration/invalid evidence fails closed."""
    _exception_id(exception_id)
    if not _available(conn):
        return None
    row = conn.execute(sa.select(exception_events).where(exception_events.c.exception_id == exception_id,
        exception_events.c.recorded_at <= _instant(now)).order_by(exception_events.c.id.desc()).limit(1)).mappings().first()
    return _plain(row) if row is not None and _valid_event(row) else None


def latest_active(conn, exception_id=FINRA_EXCEPTION_ID, *, now=None):
    row = latest_event(conn, exception_id, now=now)
    return row if row and row["action"] == "activate" else None


def record_exception(conn, *, exception_id=FINRA_EXCEPTION_ID, command_id, action, approved_by, evidence_ref, rationale):
    """Append an idempotent L0 owner decision. A reused command cannot change."""
    values = {"exception_id": _exception_id(exception_id), "command_id": _text(command_id, "command_id"),
              "action": action, "approved_by": _text(approved_by, "approved_by"),
              "evidence_ref": _text(evidence_ref, "evidence_ref"), "rationale": _text(rationale, "rationale")}
    if not isinstance(action, str) or action not in {"activate", "revoke"}:
        raise ValueError("action must be activate or revoke")
    values["event_hash"] = _hash(values)
    if isinstance(conn, Engine):
        with conn.begin() as transaction:
            return record_exception(transaction, **{k: values[k] for k in _EVENT_FIELDS})
    _lock(conn, exception_id)
    _insert_once(conn, exception_events, values, ["command_id"])
    row = conn.execute(sa.select(exception_events).where(exception_events.c.command_id == values["command_id"])).mappings().one()
    if not _valid_event(row) or any(row[k] != values[k] for k in (*_EVENT_FIELDS, "event_hash")):
        raise ValueError("Command identity already contains different exception evidence")
    return _plain(row)


def _source(value):
    from app.clhear.l1.inventory import FINRA_CATEGORIES, _in_scope_url, _source_key, _url
    if not isinstance(value, dict) or set(value) != {"source_key", "canonical_url", "source_role"}:
        raise ValueError("Source binding requires only source_key, canonical_url and source_role")
    key, role = value["source_key"], value["source_role"]
    if not isinstance(key, str) or not re.fullmatch(r"finra/[A-Za-z0-9][A-Za-z0-9._/-]{0,500}", key):
        raise ValueError("Source binding must be an exact FINRA key without wildcards")
    url = _url(value["canonical_url"]) if isinstance(value["canonical_url"], str) else None
    if not url or not _in_scope_url(url, attachment=True):
        raise ValueError("Source binding must use an in-scope official FINRA HTTPS URL")
    from urllib.parse import urlparse
    if role == "document":
        if key != _source_key(url) or urlparse(url).query:
            raise ValueError("Document identity does not match its exact FINRA URL")
    elif role == "collection":
        expected = {f"finra/catalog/{category}": _url(seed) for category, _, seed in FINRA_CATEGORIES}
        expected["finra/rulebook"] = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
        seed = expected.get(key)
        if not seed or (urlparse(url).hostname, urlparse(url).path) != (urlparse(seed).hostname, urlparse(seed).path):
            raise ValueError("Collection identity does not match its declared FINRA catalog")
    else:
        raise ValueError("source_role must be document or collection")
    return {"source_key": key, "canonical_url": url, "source_role": role}


def _valid_binding(row):
    try:
        return (row["exception_id"] == FINRA_EXCEPTION_ID
                and _source({k: row[k] for k in ("source_key", "canonical_url", "source_role")})
                    == {k: row[k] for k in ("source_key", "canonical_url", "source_role")}
                and re.fullmatch(r"[0-9a-f]{64}", row["manifest_hash"])
                and row["binding_hash"] == _hash({k: row[k] for k in _BINDING_FIELDS}))
    except (ValueError, KeyError, TypeError):
        return False


def bind_sources(conn, *, exception_id=FINRA_EXCEPTION_ID, activation_id, manifest_hash, scope_version, sources, bound_by):
    """L0 binds a reviewed frozen manifest; no wildcard or automatic expansion."""
    _exception_id(exception_id)
    if type(activation_id) is not int or activation_id < 1:
        raise ValueError("activation_id must identify an existing owner activation")
    if not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        raise ValueError("manifest_hash must be a SHA256")
    scope_version, bound_by = _text(scope_version, "scope_version"), _text(bound_by, "bound_by")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must contain exact reviewed source identities")
    prepared = [_source(source) for source in sources]
    if len({(source["source_key"], source["canonical_url"]) for source in prepared}) != len(prepared):
        raise ValueError("Duplicate source identities in binding manifest")
    if isinstance(conn, Engine):
        with conn.begin() as transaction:
            return bind_sources(transaction, exception_id=exception_id, activation_id=activation_id,
                manifest_hash=manifest_hash, scope_version=scope_version, sources=prepared, bound_by=bound_by)
    _lock(conn, exception_id)
    active = latest_active(conn, exception_id)
    if not active or active["id"] != activation_id:
        raise ValueError("Source binding requires the current active exception decision")
    result = []
    for source in prepared:
        values = {**source, "activation_id": activation_id, "exception_id": exception_id,
                  "manifest_hash": manifest_hash, "scope_version": scope_version, "bound_by": bound_by}
        values["binding_hash"] = _hash(values)
        conflict = ["activation_id", "source_key", "canonical_url", "manifest_hash"]
        _insert_once(conn, source_bindings, values, conflict)
        row = conn.execute(sa.select(source_bindings).where(*(source_bindings.c[k] == values[k] for k in conflict))).mappings().one()
        if any(row[k] != values[k] for k in (*_BINDING_FIELDS, "binding_hash")):
            raise ValueError("Bound source identity already contains different manifest evidence")
        result.append(_plain(row))
    return result


def bind_source(conn, *, source_key, canonical_url, source_role, **kwargs):
    return bind_sources(conn, sources=[dict(source_key=source_key, canonical_url=canonical_url, source_role=source_role)], **kwargs)[0]


def decision(conn, source_key, operation, now=None, *, canonical_url=None):
    """Candidate-only exception decision. Never a release or publisher approval."""
    out = {"allowed": False, "source_key": source_key, "operation": operation,
           "authority_type": "operator_exception", "exception_id": FINRA_EXCEPTION_ID,
           "permission_id": None, "activation_id": None, "binding_id": None, "binding_hash": None,
           "release_eligible": False, "expires_at": None, "expiry_policy": "until_revoked"}
    if operation not in OPERATIONS:
        return {**out, "reason": "operation_not_permitted_by_exception"}
    if not _available(conn):
        return {**out, "reason": "operator_exception_migration_required"}
    active = latest_active(conn, now=now)
    if not active:
        return {**out, "reason": "operator_exception_inactive"}
    out.update(activation_id=active["id"], approved_by=active["approved_by"], evidence_ref=active["evidence_ref"])
    query = sa.select(source_bindings).where(source_bindings.c.activation_id == active["id"],
        source_bindings.c.source_key == source_key, source_bindings.c.recorded_at <= _instant(now))
    if canonical_url is None:
        # Readers have a persisted source, while discovery may precede it. A
        # reused key must not silently authorize a changed publisher identity.
        from app.clhear.l1.models import sources
        schema = None if conn.dialect.name == "sqlite" else L1_SCHEMA
        if sa.inspect(conn).has_table(sources.name, schema=schema):
            stored = conn.execute(sa.select(sources.c.canonical_url).where(sources.c.key == source_key)).first()
            if stored is not None:
                canonical_url = stored[0] or ""
    if canonical_url is not None:
        from app.clhear.l1.inventory import _url
        canonical = _url(canonical_url) if isinstance(canonical_url, str) else None
        if not canonical:
            return {**out, "reason": "operator_exception_identity_mismatch"}
        query = query.where(source_bindings.c.canonical_url == canonical)
    row = conn.execute(query.order_by(source_bindings.c.id.desc()).limit(1)).mappings().first()
    if row is None or not _valid_binding(row):
        return {**out, "reason": "operator_exception_source_unbound"}
    return {**out, "allowed": True, "reason": "operator_exception_active",
            "permission_id": f"operator-exception:{active['id']}:{row['id']}",
            "binding_id": row["id"], "binding_hash": row["binding_hash"],
            "manifest_hash": row["manifest_hash"], "scope_version": row["scope_version"],
            "canonical_url": row["canonical_url"], "source_role": row["source_role"]}


def control_state(conn):
    """Deterministic current ledger evidence for the worker-published control."""
    unavailable = {"status": "unavailable", "exceptions": [], "bindings": [], "digest": None}
    if not _available(conn):
        return {**unavailable, "reason": "migration_required"}
    row = conn.execute(sa.select(exception_events).order_by(exception_events.c.id.desc()).limit(1)).mappings().first()
    events, bindings = [], []
    if row is not None:
        if not _valid_event(row):
            return {**unavailable, "reason": "invalid_exception_evidence"}
        events = [{**{k: row[k] for k in _EVENT_FIELDS}, "activation_id": row["id"], "event_hash": row["event_hash"]}]
        if row["action"] == "activate":
            for bound in conn.execute(sa.select(source_bindings).where(source_bindings.c.activation_id == row["id"])
                                      .order_by(source_bindings.c.id)).mappings():
                if not _valid_binding(bound):
                    return {**unavailable, "reason": "invalid_binding_evidence"}
                bindings.append({**{k: bound[k] for k in _BINDING_FIELDS}, "binding_id": bound["id"], "binding_hash": bound["binding_hash"]})
    state = {"exceptions": events, "bindings": bindings}
    return {"status": "available", **state, "digest": _hash(state)}


__all__ = ["FINRA_EXCEPTION_ID", "OPERATIONS", "TABLES", "exception_events", "source_bindings",
           "record_exception", "bind_source", "bind_sources", "latest_event", "latest_active", "decision", "control_state"]
