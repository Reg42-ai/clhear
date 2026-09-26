# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Outbox writer + SQS relay + envelope schema (HLD §7.1).

Writers insert into l0_platform.events in the SAME transaction as their data
change; the relay ships unrelayed rows to SQS and stamps relayed_at. Nothing
publishes to SQS directly. Consumers must be idempotent on event_id.
"""
import hashlib
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Literal, Protocol

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection, Engine

from app.clhear.models import events

log = logging.getLogger("clhear.events")

ENVELOPE_SCHEMA_VERSION = 1
# The live queues retain the 256 KiB limit. Leave room for EventBridge's
# routing fields as well; a larger cloud quota is not a correctness strategy.
MAX_INLINE_ENVELOPE_BYTES = 240 * 1024


class Envelope(BaseModel):
    """Frozen event envelope — the most expensive thing to change later (HLD §6.1)."""

    model_config = {"frozen": True}

    event_id: str
    layer: str
    kind: str
    subject_ref: str
    payload: dict = Field(default_factory=dict)
    schema_version: int = ENVELOPE_SCHEMA_VERSION
    producer: str
    ts: str


class OutboxReference(BaseModel):
    """Transport-only indirection; the original event stays in the outbox."""

    model_config = {"frozen": True, "extra": "forbid"}
    transport_schema: Literal["clhear.outbox-reference.v1"] = "clhear.outbox-reference.v1"
    event_id: uuid.UUID
    layer: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=256)
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def parse_transport(body: str) -> Envelope | OutboxReference:
    value = json.loads(body)
    if isinstance(value, dict) and "transport_schema" in value:
        return OutboxReference.model_validate(value)
    return Envelope.model_validate(value)


def _envelope_hash(envelope: Envelope) -> str:
    value = envelope.model_dump(mode="json")
    # JSONB can reorder object keys, and PostgreSQL sessions can render a
    # timestamptz in different zones. Lists and all payload strings stay exact.
    stamp = datetime.fromisoformat(envelope.ts.replace("Z", "+00:00"))
    value["ts"] = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def transport_body(envelope: Envelope) -> str:
    body = envelope.model_dump_json()
    if len(body.encode("utf-8")) <= MAX_INLINE_ENVELOPE_BYTES:
        return body
    return OutboxReference(event_id=envelope.event_id, layer=envelope.layer, kind=envelope.kind,
                           envelope_sha256=_envelope_hash(envelope)).model_dump_json()


def resolve_envelope(engine: Engine, body: str) -> Envelope:
    """Resolve one local durable event, then let normal worker gates run."""
    wire = parse_transport(body)
    if isinstance(wire, Envelope):
        return wire
    with engine.connect() as conn:
        row = conn.execute(sa.select(events).where(events.c.event_id == str(wire.event_id))).one_or_none()
    if row is None or row.created_at is None:
        raise ValueError("Referenced outbox event or its original timestamp is unavailable")
    envelope = _row_to_envelope(row)
    if (envelope.layer != wire.layer or envelope.kind != wire.kind
            or _envelope_hash(envelope) != wire.envelope_sha256):
        raise ValueError("Referenced outbox event does not match its immutable transport identity")
    return envelope


def emit(
    conn: Connection,
    *,
    layer: str,
    kind: str,
    subject_ref: str,
    payload: dict | None = None,
    producer: str,
    schema_version: int = ENVELOPE_SCHEMA_VERSION,
) -> str:
    """Write an outbox row inside the caller's transaction. Returns event_id."""
    event_id = str(uuid.uuid4())
    conn.execute(
        events.insert().values(
            event_id=event_id,
            layer=layer,
            kind=kind,
            subject_ref=subject_ref,
            payload=payload or {},
            schema_version=schema_version,
            producer=producer,
        )
    )
    return event_id


# HLD v2 §3: every layer publishes these on the `clhear` EventBridge bus as
# detail-type `clhear.<layer>.<event>`; rules fan them out to per-fleet queues.
LAYER_EVENTS = ("derived", "changed", "invalidated", "below_gate")
BUS_NAME = "clhear"


def layer_event_kind(layer: str, event: str) -> str:
    if event not in LAYER_EVENTS:
        raise ValueError(f"unknown layer event {event!r}; expected one of {LAYER_EVENTS}")
    return f"clhear.{layer.lower()}.{event}"


def publish_layer_event(
    conn: Connection,
    *,
    layer: str,
    event: str,
    subject_ref: str,
    payload: dict | None = None,
    producer: str,
) -> str:
    """Outbox write for `clhear.<layer>.<event>` (same transaction as the data change)."""
    return emit(
        conn,
        layer=layer.lower(),
        kind=layer_event_kind(layer, event),
        subject_ref=subject_ref,
        payload=payload or {},
        producer=producer,
    )


class Transport(Protocol):
    def send(self, body: str) -> None: ...


class SqsTransport:
    def __init__(self, queue_url: str, region: str):
        import boto3

        self._client = boto3.client("sqs", region_name=region)
        self._queue_url = queue_url

    def send(self, body: str) -> None:
        self._client.send_message(QueueUrl=self._queue_url, MessageBody=body)


class EventBridgeTransport:
    """Puts layer events on the `clhear` bus; non-layer kinds are skipped here
    because they already travel on the fleet queue."""

    def __init__(self, region: str, bus_name: str = BUS_NAME, source: str = "clhear"):
        import boto3

        self._client = boto3.client("events", region_name=region)
        self._bus = bus_name
        self._source = source

    def send(self, body: str) -> None:
        env = json.loads(body)
        kind = env.get("kind", "")
        if not kind.startswith("clhear."):
            return
        self._client.put_events(
            Entries=[
                {
                    "EventBusName": self._bus,
                    "Source": self._source,
                    "DetailType": kind,
                    "Detail": body,
                }
            ]
        )


class FanoutTransport:
    """Send to every transport (SQS fleet queue + EventBridge bus)."""

    def __init__(self, *transports: Transport):
        self._transports = transports

    def send(self, body: str) -> None:
        for t in self._transports:
            t.send(body)


class InMemoryTransport:
    """Offline stand-in for SQS in tests and local dev."""

    def __init__(self):
        self.queue: deque[str] = deque()

    def send(self, body: str) -> None:
        self.queue.append(body)

    def receive(self) -> str | None:
        return self.queue.popleft() if self.queue else None


def _row_to_envelope(row) -> Envelope:
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    created = row.created_at or datetime.now(timezone.utc)
    if isinstance(created, str):
        ts = created
    else:
        ts = created.isoformat()
    return Envelope(
        event_id=str(row.event_id),
        layer=row.layer,
        kind=row.kind,
        subject_ref=row.subject_ref,
        payload=payload,
        schema_version=row.schema_version,
        producer=row.producer,
        ts=ts,
    )


def relay_once(engine: Engine, transport: Transport, batch_size: int = 100, *, fleet: str = "l0") -> int:
    """Ship unrelayed outbox rows to the transport, one committed disposition per
    event. Returns the number of rows dispatched.

    Each row is stamped in its own transaction: a row the transport cannot
    accept stops this pass but never rolls back the rows before it, so nothing
    is sent twice. Audit-only kinds are stamped ``audit_only`` without a send;
    kinds outside the routing table, and rows without their original
    timestamp, are quarantined into the deferred-delivery ledger with their
    body so they stop blocking the queue and keep their evidence.
    """
    from app.clhear.platform import deferred, routing

    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(events).where(events.c.relayed_at.is_(None)).order_by(events.c.id).limit(batch_size)
        ).all()
    shipped = 0
    for row in rows:
        category, _owner = routing.classify(row.kind)
        now = datetime.now(timezone.utc)
        if category == "audit":
            with engine.begin() as conn:
                conn.execute(events.update().where(events.c.id == row.id, events.c.relayed_at.is_(None))
                             .values(relayed_at=now, relay_disposition="audit_only"))
            continue
        # Do not invent a timestamp for a reference to a malformed legacy row:
        # it could never resolve to the same immutable hash later.
        problem = None
        if row.created_at is None:
            problem = ("unidentifiable_schedule", "outbox event is missing its original timestamp")
        elif category == "unknown":
            problem = ("unknown_kind", f"no route for outbox kind {row.kind!r}")
        if problem is not None:
            reason, detail = problem
            with engine.begin() as conn:
                deferred.record(conn, channel="outbox", queue="outbox", message_id=str(row.event_id), fleet=fleet,
                                body=_row_to_envelope(row).model_dump_json() if row.created_at is not None else
                                json.dumps({"event_id": str(row.event_id), "kind": row.kind, "layer": row.layer,
                                            "subject_ref": row.subject_ref, "payload": row.payload, "producer": row.producer}),
                                reason=reason, detail=detail, status="quarantined")
                conn.execute(events.update().where(events.c.id == row.id, events.c.relayed_at.is_(None))
                             .values(relayed_at=now, relay_disposition="quarantined"))
            log.warning("outbox event %s quarantined: %s", row.event_id, detail)
            continue
        envelope = _row_to_envelope(row)
        transport.send(transport_body(envelope))
        with engine.begin() as conn:
            conn.execute(events.update().where(events.c.id == row.id, events.c.relayed_at.is_(None))
                         .values(relayed_at=now, relay_disposition="sent"))
        shipped += 1
    if shipped:
        log.info("relayed %d event(s)", shipped)
    return shipped


def relay_forever(engine: Engine, transport: Transport, interval_s: float = 2.0) -> None:
    while True:
        try:
            if relay_once(engine, transport) == 0:
                time.sleep(interval_s)
        except Exception:
            log.exception("relay iteration failed; backing off")
            time.sleep(interval_s * 5)
