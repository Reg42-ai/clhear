# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Deferred-delivery ledger: the durable home of a queue message a worker
must not process now and must not leave circulating.

A fleet that receives a held downstream event, a command it does not own, an
unknown or malformed body, or a scheduled occurrence it cannot identify used
to leave the message in its queue. SQS redelivered it every visibility
timeout until the dead-letter policy took it, and nothing recorded why. Now
the worker writes the exact message here first — original identity and
timestamp, payload hash, queue metadata, the reason — and only then
acknowledges it. Deferred downstream work stays unexecuted while L2–L8 are
held; a replay later is an explicit, idempotent operation, never a side
effect of consumption.

Bodies are stored once per hash (the same event fans out to several queues),
deliveries once per queue message.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.models import Json, metadata

REASONS = (
    "downstream_held",       # a layer event / command for L2–L8 while CLHEAR_L1_ONLY holds
    "wrong_owner",           # a valid command delivered to a fleet that does not own it
    "audit_only",            # an audit record that no fleet consumes
    "unknown_kind",          # a well-formed envelope of a kind no table names
    "malformed",             # not an envelope at all
    "unidentifiable_schedule",  # a scheduled occurrence without its timestamp
    "unresolvable_reference",   # an outbox reference whose row or hash is gone
)
STATUSES = ("deferred", "quarantined", "rerouted", "replayed")

deferred_bodies = sa.Table(
    "deferred_bodies", metadata,
    sa.Column("payload_hash", sa.Text, primary_key=True),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("byte_count", sa.Integer, nullable=False),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
)

deferred_deliveries = sa.Table(
    "deferred_deliveries", metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("channel", sa.Text, nullable=False),            # sqs | outbox | dlq
    sa.Column("queue", sa.Text, nullable=False),              # queue name or "outbox"
    sa.Column("message_id", sa.Text, nullable=False),         # SQS MessageId or outbox event_id
    sa.Column("fleet", sa.Text, nullable=False),              # the fleet that deferred it
    sa.Column("event_id", sa.Text),
    sa.Column("event_kind", sa.Text),
    sa.Column("event_layer", sa.Text),
    sa.Column("event_ts", sa.Text),                           # original occurrence timestamp, verbatim
    sa.Column("producer", sa.Text),
    sa.Column("payload_hash", sa.Text, nullable=False),
    sa.Column("queue_metadata", Json, nullable=False, default=dict),   # receive count, sent timestamp, …
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("detail", sa.Text),
    sa.Column("status", sa.Text, nullable=False, default="deferred"),
    sa.Column("deferred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.Column("resolution", Json, nullable=False, default=dict),
    sa.UniqueConstraint("channel", "queue", "message_id", name="deferred_deliveries_message_uq"),
    sa.Index("deferred_deliveries_status_kind_idx", "status", "event_kind"),
)

TABLES = (deferred_bodies, deferred_deliveries)


def _now():
    return datetime.now(timezone.utc)


def payload_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _identity(body: str) -> dict:
    """What can be read from the message without trusting it."""
    try:
        data = json.loads(body)
    except ValueError:
        return {"malformed": True}
    if not isinstance(data, dict):
        return {"malformed": True}
    # EventBridge-wrapped bodies carry the envelope in ``detail``.
    if "detail-type" in data and isinstance(data.get("detail"), (dict, str)):
        inner = data["detail"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = {}
        data = {**(inner if isinstance(inner, dict) else {}), "kind": data.get("detail-type")}
    return {"event_id": data.get("event_id"), "kind": data.get("kind"), "layer": data.get("layer"),
            "ts": data.get("ts"), "producer": data.get("producer"), "malformed": False}


def record(conn: Connection, *, channel: str, queue: str, message_id: str, fleet: str, body: str, reason: str,
           detail: str | None = None, queue_metadata: dict | None = None, status: str = "deferred") -> dict:
    """Persist one delivery exactly once; a redelivery of the same message is a no-op.
    Must be committed before the caller acknowledges the message."""
    if reason not in REASONS:
        raise ValueError(f"unknown deferral reason {reason!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown deferral status {status!r}")
    digest = payload_hash(body)
    identity = _identity(body)
    now = _now()
    existing = conn.execute(sa.select(deferred_bodies.c.payload_hash).where(deferred_bodies.c.payload_hash == digest)).first()
    if existing is None:
        conn.execute(deferred_bodies.insert().values(payload_hash=digest, body=body, byte_count=len(body.encode("utf-8")),
                                                     first_seen_at=now))
    already = conn.execute(sa.select(deferred_deliveries.c.id, deferred_deliveries.c.status).where(
        deferred_deliveries.c.channel == channel, deferred_deliveries.c.queue == queue,
        deferred_deliveries.c.message_id == message_id)).first()
    if already is not None:
        return {"id": already.id, "status": already.status, "payload_hash": digest, "duplicate": True}
    row_id = conn.execute(deferred_deliveries.insert().values(
        channel=channel, queue=queue, message_id=message_id, fleet=fleet,
        event_id=str(identity.get("event_id")) if identity.get("event_id") else None,
        event_kind=identity.get("kind"), event_layer=identity.get("layer"),
        event_ts=str(identity.get("ts")) if identity.get("ts") else None, producer=identity.get("producer"),
        payload_hash=digest, queue_metadata=queue_metadata or {}, reason=reason,
        detail=(detail or "")[:500] or None, status=status, deferred_at=now, resolution={},
    ).returning(deferred_deliveries.c.id)).scalar_one()
    return {"id": row_id, "status": status, "payload_hash": digest, "duplicate": False, **{k: v for k, v in identity.items() if k != "malformed"}}


def resolve(conn: Connection, row_id: int, *, status: str, resolution: dict) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown deferral status {status!r}")
    conn.execute(deferred_deliveries.update().where(deferred_deliveries.c.id == row_id)
                 .values(status=status, resolved_at=_now(), resolution=resolution))


def body_for(conn: Connection, digest: str) -> str | None:
    return conn.execute(sa.select(deferred_bodies.c.body).where(deferred_bodies.c.payload_hash == digest)).scalar_one_or_none()


def counts(engine: Engine) -> dict:
    """Small enough for a status record: totals by status, reason and kind."""
    schema = None if engine.dialect.name == "sqlite" else metadata.schema
    if not sa.inspect(engine).has_table(deferred_deliveries.name, schema=schema):
        return {"available": False, "total": 0, "by_status": {}, "by_reason": {}, "by_kind": {}}
    with engine.connect() as conn:
        by_status = dict(conn.execute(sa.select(deferred_deliveries.c.status, sa.func.count()).group_by(deferred_deliveries.c.status)).all())
        by_reason = dict(conn.execute(sa.select(deferred_deliveries.c.reason, sa.func.count()).group_by(deferred_deliveries.c.reason)).all())
        by_kind = dict(conn.execute(sa.select(sa.func.coalesce(deferred_deliveries.c.event_kind, "(malformed)"), sa.func.count())
                                    .group_by(deferred_deliveries.c.event_kind).order_by(sa.func.count().desc()).limit(25)).all())
        latest = conn.execute(sa.select(sa.func.max(deferred_deliveries.c.deferred_at))).scalar_one()
    return {"available": True, "total": sum(by_status.values()), "by_status": by_status, "by_reason": by_reason,
            "by_kind": by_kind, "latest_deferred_at": str(latest) if latest else None}
