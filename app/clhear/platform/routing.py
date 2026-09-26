# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Explicit event routing: who owns a command, where a layer event goes, and
which outbox rows are audit records that no fleet ever consumes.

Before this table existed the relay sent every kind it did not recognise to
the L0 queue. L0 had no handler for ``SourceChanged`` and refused it as
misrouted, SQS redelivered it until the dead-letter policy took it, and a
single unsupported row could roll back the relay's whole batch so the rows
before it were sent again on every loop. Ownership is now one table shared by
the relay and the consumer, and anything outside it is quarantined with its
evidence instead of dispatched.
"""
from __future__ import annotations

# Commands: exactly one owning fleet consumes each kind from its own queue.
COMMAND_OWNERS: dict[str, str] = {
    "DummyChanged": "l0",
    "CommunityWrite": "l0",
    "AdapterRunRequested": "l1",
    "DemoDeriveRequested": "l0",
    "L1CycleRequested": "l0",
    "L1CycleAdvanceRequested": "l0",
    "L1CycleDiscoveryRequested": "l1",
    "L1CycleEvaluationRequested": "l1",
    "L1ExceptionBindingsRequested": "l0",
    "L1InventoryAuditRequested": "l1",
    "L1EvidenceReviewRecorded": "l0",
    "L1TranslationRequested": "l1",
    "ViewerSnapshotRequested": "l0",
    "PublishReleaseRequested": "l0",
    "GraphRebuildRequested": "l0",
    "DrDrillRequested": "l0",
    "QueueRecoveryRequested": "l0",
}

# Layer events (HLD v2 §3): published on the ``clhear`` EventBridge bus as
# detail-type ``clhear.<layer>.<event>``; rules fan them out to the fleets that
# subscribe. The consumer map is what the worker enforces on receipt.
LAYER_EVENT_PREFIX = "clhear."
LAYER_EVENT_CONSUMERS: dict[str, frozenset[str]] = {
    "clhear.l1.changed": frozenset({"l2"}),
    "clhear.l2.changed": frozenset({"l3", "l4", "l5"}),
    "clhear.l4.changed": frozenset({"l6"}),
    "clhear.l5.changed": frozenset({"l6"}),
}
# Layer events every fleet may see on the bus but nobody consumes from a queue:
# they are published for subscribers outside the fleets (status, audit, operators).
LAYER_EVENTS_WITHOUT_QUEUE_CONSUMER: frozenset[str] = frozenset({
    "clhear.l0.graph_rebuilt", "clhear.l0.dr_drill", "clhear.l6.changed", "clhear.l7.scored",
    "clhear.l3.derived", "clhear.l4.derived", "clhear.l5.derived", "clhear.l6.derived", "clhear.l8.derived",
})

# Audit-only outbox rows: written in the same transaction as the record change
# they describe (HLD v2 §7.1) and read by the L1 activity feed. They are never
# dispatched to a fleet because no fleet has work to do for them.
AUDIT_ONLY_EVENTS: frozenset[str] = frozenset({
    "SourceChanged", "FamilyMembersAdded", "IngestFidelityFailed",
    "ProposalApproved", "ProposalRejected",
})

# Kinds L2–L8 consume; held while L1 acceptance is pending (CLHEAR_L1_ONLY).
DOWNSTREAM_HELD_KINDS: frozenset[str] = frozenset({
    "clhear.l1.changed", "clhear.l2.changed", "clhear.l4.changed", "clhear.l5.changed",
    "PublishReleaseRequested", "GraphRebuildRequested", "DrDrillRequested",
})


def classify(kind: str) -> tuple[str, str | None]:
    """``("command", owner)`` | ``("layer_event", None)`` | ``("audit", None)`` | ``("unknown", None)``."""
    if kind in COMMAND_OWNERS:
        return "command", COMMAND_OWNERS[kind]
    if kind in AUDIT_ONLY_EVENTS:
        return "audit", None
    if kind.startswith(LAYER_EVENT_PREFIX) and (kind in LAYER_EVENT_CONSUMERS or kind in LAYER_EVENTS_WITHOUT_QUEUE_CONSUMER):
        return "layer_event", None
    return "unknown", None


def is_layer_event(kind: str) -> bool:
    return classify(kind)[0] == "layer_event"


def consumers_for(kind: str) -> frozenset[str]:
    """Fleets allowed to consume ``kind`` from a queue."""
    category, owner = classify(kind)
    if category == "command":
        return frozenset({owner})
    if category == "layer_event":
        return LAYER_EVENT_CONSUMERS.get(kind, frozenset())
    return frozenset()
