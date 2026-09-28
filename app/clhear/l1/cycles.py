# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Worker-owned L1 cycles. Commands use the ordinary durable L0 outbox.

This module plans and accounts for work; acquisition and encoding remain in
the existing L1 adapter handler. A completed cycle is not a published release.
"""
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import re
import uuid

import sqlalchemy as sa

from app.clhear.models import metadata, Json
from app.clhear.platform import events
from app.clhear.l1 import workflow


cycles = sa.Table("l1_cycles", metadata,
    sa.Column("cycle_id", sa.Text, primary_key=True),
    sa.Column("request_event_id", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("scheduled_for", sa.DateTime(timezone=True)),
    sa.Column("code_revision", sa.Text),
    sa.Column("worker_image_digest", sa.Text),
    sa.Column("parser_configuration_digest", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("manifest", Json, nullable=False, default=dict),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)
children = sa.Table("l1_cycle_children", metadata,
    sa.Column("child_id", sa.Text, primary_key=True),
    sa.Column("cycle_id", sa.Text, nullable=False, index=True),
    sa.Column("adapter_key", sa.Text, nullable=False),
    sa.Column("event_id", sa.Text),
    sa.Column("event_time", sa.Text),
    sa.Column("command_event_id", sa.Text),
    sa.Column("job_id", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("source_keys", Json, nullable=False, default=list),
    sa.Column("inventory_hash", sa.Text),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("cycle_id", "adapter_key"),
)
queue = sa.Table("l1_cycle_queue", metadata,
    sa.Column("position", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("cycle_id", sa.Text, nullable=False, unique=True),
    sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("recovery_count", sa.Integer, nullable=False, default=0),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)
slot = sa.Table("l1_cycle_slot", metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("cycle_id", sa.Text),
)
SLOT_NAME = "all_publishers"
FENCE_KEY = 0x434C48314C31
RECOVERY_SECONDS = 300
TERMINAL_CHILD = {"completed", "completed_for_review", "failed"}
TERMINAL_CYCLE = {"candidate_verified", "completed_for_review", "failed"}


class CycleRevisionChanged(ValueError):
    """Pending work belongs to a different immutable worker deployment."""


@contextmanager
def _execution_fence(engine, *, exclusive=False):
    """A dead lease cannot release a slot while a handler is still executing.

    Session locks disappear with the PostgreSQL connection/process. SQLite's
    equivalent is an OS file lock, including across worker processes. Neither
    holds a database transaction open during publisher requests.
    """
    if engine.dialect.name == "postgresql":
        suffix = "" if exclusive else "_shared"
        with engine.connect() as conn:
            locked = conn.execute(sa.text(f"SELECT pg_try_advisory_lock{suffix}(:key)"), {"key": FENCE_KEY}).scalar_one()
            conn.commit()
            if not locked:
                raise workflow.LeaseBusy("L1 cycle handlers are still executing")
            try:
                yield
            finally:
                try:
                    conn.execute(sa.text(f"SELECT pg_advisory_unlock{suffix}(:key)"), {"key": FENCE_KEY})
                    conn.commit()
                except Exception:
                    conn.invalidate()  # never return a locked session to the pool
                    raise
    elif engine.dialect.name == "sqlite":
        import fcntl
        import tempfile
        database = engine.url.database
        lock_path = (str(Path(database).resolve()) + ".l1-cycle.lock" if database and database != ":memory:"
                     else str(Path(tempfile.gettempdir()) / f"clhear-cycle-{id(engine)}.lock"))
        with open(lock_path, "a") as handle:
            try:
                fcntl.flock(handle.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise workflow.LeaseBusy("L1 cycle handlers are still executing") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        raise ValueError("L1 cycle serialization requires PostgreSQL or SQLite")


def _lock_slot(conn):
    workflow._insert_once(conn, slot, {"name": SLOT_NAME})
    # A write acquires the SQLite writer lock too; FOR UPDATE alone does not.
    conn.execute(slot.update().where(slot.c.name == SLOT_NAME).values(cycle_id=slot.c.cycle_id))
    return conn.execute(sa.select(slot.c.cycle_id).where(slot.c.name == SLOT_NAME)).scalar_one()


def _enqueue(conn, cycle_id):
    _lock_slot(conn)
    workflow._insert_once(conn, queue, {"cycle_id": cycle_id, "queued_at": workflow.utcnow(), "recovery_count": 0})
    conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(status="queued"))


def _touch(conn, cycle_id, *, progress=False, now=None):
    now = now or workflow.utcnow()
    values = {"heartbeat_at": now, "lease_until": now + timedelta(seconds=RECOVERY_SECONDS)}
    if progress:
        values["recovery_count"] = 0
    conn.execute(queue.update().where(queue.c.cycle_id == cycle_id, queue.c.finished_at.is_(None)).values(**values))


def _terminal(conn, cycle_id):
    conn.execute(queue.update().where(queue.c.cycle_id == cycle_id).values(finished_at=workflow.utcnow(), lease_until=None))
    # Admission runs on L0 after the finishing handler releases its fence.
    _emit(conn, "L1CycleAdvanceRequested", cycle_id)


@contextmanager
def operation_guard(engine, envelope):
    """Worker dispatch hook: guard source writes/audits, not scheduler receipts."""
    kinds = {"AdapterRunRequested", "L1InventoryAuditRequested", "L1CycleDiscoveryRequested",
             "L1CycleEvaluationRequested", "TranslationRequested", "L1TranslationRequested"}
    if envelope.kind not in kinds or (envelope.kind == "AdapterRunRequested" and envelope.producer == "eventbridge"):
        yield
        return
    # Standalone audit/translation commands cannot borrow a cycle ID to bypass
    # its frozen command plan. In-cycle translation is synchronous cycle work.
    cycle_id = envelope.payload.get("cycle_id") if envelope.kind in {
        "AdapterRunRequested", "L1CycleDiscoveryRequested", "L1CycleEvaluationRequested"} else None
    with _execution_fence(engine, exclusive=not cycle_id):
        with engine.connect() as conn:
            active = conn.execute(sa.select(slot.c.cycle_id).where(slot.c.name == SLOT_NAME)).scalar()
            if cycle_id:
                row = _row(conn, cycle_id)
                if row["status"] in TERMINAL_CYCLE:
                    # Late duplicate handlers must return their terminal result;
                    # they are never entitled to acquire or encode again.
                    raise CycleRevisionChanged("Cycle has already finished; retain its terminal evidence")
                if active != cycle_id:
                    raise workflow.RetryDeferred("Cycle is queued behind another whole L1 cycle")
                if any(row[key] != value for key, value in runtime_identity().items()):
                    raise CycleRevisionChanged("Cycle requires its original worker revision; request a new cycle")
                phase = {"L1CycleDiscoveryRequested": "discovering", "L1CycleEvaluationRequested": "evaluating"}.get(envelope.kind)
                if phase and row["status"] != phase:
                    raise workflow.RetryDeferred("Command belongs to an earlier or later cycle phase")
                if phase:
                    command = conn.execute(sa.select(events.events).where(events.events.c.event_id == envelope.event_id)).mappings().first()
                    if (not command or command["kind"] != envelope.kind or command["subject_ref"] != cycle_id
                            or command["payload"] != envelope.payload or envelope.producer != "l0.l1_cycle"):
                        raise ValueError("Cycle phase command is not bound to its durable L0 request")
            elif active:
                raise workflow.RetryDeferred("Direct L1 work waits until the active whole cycle finishes")
        if not cycle_id:
            yield
            return
        def touch():
            with engine.begin() as conn:
                _touch(conn, cycle_id)
        touch()
        with workflow.heartbeat(touch):
            yield


def reconcile(engine, *, now=None, admit=True):
    """L0 poll hook: FIFO admission and bounded recovery of the same commands.

    Lease expiry never steals executing work. Missing scheduler receipts do
    not occupy the slot. Original request/occurrence identities stay unchanged.
    """
    now = now or workflow.utcnow()
    try:
        with _execution_fence(engine, exclusive=True), engine.begin() as conn:
            active = _lock_slot(conn)
            # Migration preserves earlier history. Pre-serialization active
            # cycles cannot be silently joined to a new deployed runtime.
            legacy = conn.execute(sa.select(cycles).where(cycles.c.status.in_(
                ["requested", "discovering", "planned", "running", "evaluating"]),
                ~cycles.c.cycle_id.in_(sa.select(queue.c.cycle_id)))).mappings().all()
            for prior in legacy:
                _fail_locked(conn, dict(prior), "unserialized_cycle_requires_new_cycle")
            if active:
                row = _row(conn, active, lock=True)
                identity_changed = any(row[key] != value for key, value in runtime_identity().items())
                if row["status"] not in TERMINAL_CYCLE and identity_changed:
                    _fail_locked(conn, row, "worker_revision_changed_requires_new_cycle")
                    row = _row(conn, active)
                if row["status"] in TERMINAL_CYCLE:
                    conn.execute(slot.update().where(slot.c.name == SLOT_NAME).values(cycle_id=None))
                    active = None
                else:
                    pending = conn.execute(sa.select(queue).where(queue.c.cycle_id == active)).mappings().one()
                    if admit and (pending["lease_until"] is None or workflow._aware(pending["lease_until"]) <= now):
                        if pending["recovery_count"] >= workflow.MAX_ATTEMPTS:
                            _fail_locked(conn, row, "cycle_recovery_attempts_exhausted")
                            conn.execute(slot.update().where(slot.c.name == SLOT_NAME).values(cycle_id=None))
                            active = None
                        else:
                            _recover_commands(conn, row)
                            _touch(conn, active, now=now)
                            conn.execute(queue.update().where(queue.c.cycle_id == active).values(
                                recovery_count=queue.c.recovery_count + 1))
            if not active and admit:
                pending = conn.execute(sa.select(cycles).join(queue, queue.c.cycle_id == cycles.c.cycle_id).where(
                    queue.c.finished_at.is_(None), cycles.c.status == "queued").order_by(queue.c.position)).mappings().all()
                for candidate in pending:
                    if any(candidate[key] != value for key, value in runtime_identity().items()):
                        _fail_locked(conn, dict(candidate), "worker_revision_changed_requires_new_cycle")
                        continue
                    active = candidate["cycle_id"]
                    conn.execute(slot.update().where(slot.c.name == SLOT_NAME).values(cycle_id=active))
                    conn.execute(cycles.update().where(cycles.c.cycle_id == active).values(status="discovering"))
                    conn.execute(queue.update().where(queue.c.cycle_id == active).values(started_at=now))
                    _touch(conn, active, progress=True, now=now)
                    _emit(conn, "L1CycleDiscoveryRequested", active)
                    break
            return {"active_cycle_id": active, "status": "active" if active else "idle"}
    except workflow.LeaseBusy:
        return {"status": "executing"}


def _fail_locked(conn, row, reason):
    from app.clhear.l1.viewer_snapshot import request_refresh
    result = {"cycle_id": row["cycle_id"], "status": "failed", "execution_failed": True,
              "reason": reason, "accepted_release": False, "downstream": "held",
              "original_code_revision": row["code_revision"], "original_worker_image_digest": row["worker_image_digest"],
              **runtime_identity()}
    conn.execute(children.update().where(children.c.cycle_id == row["cycle_id"],
        children.c.status.not_in(TERMINAL_CHILD)).values(status="failed", result=result, finished_at=workflow.utcnow()))
    conn.execute(cycles.update().where(cycles.c.cycle_id == row["cycle_id"]).values(
        status="failed", result=result, finished_at=workflow.utcnow()))
    _terminal(conn, row["cycle_id"])
    request_refresh(conn, reason="l1_cycle_failed", job_id=row["cycle_id"])


def _recover_commands(conn, row):
    """Re-relay immutable event IDs so completed deliveries stay deduplicated."""
    if row["status"] in {"planned", "running"}:
        _emit(conn, "L1CycleAdvanceRequested", row["cycle_id"])
    if row["status"] == "running":
        event_ids = list(conn.execute(sa.select(children.c.command_event_id).where(
            children.c.cycle_id == row["cycle_id"], children.c.status.not_in(TERMINAL_CHILD))).scalars())
    else:
        kind = {"discovering": "L1CycleDiscoveryRequested", "evaluating": "L1CycleEvaluationRequested"}.get(row["status"])
        event_id = conn.execute(sa.select(events.events.c.event_id).where(events.events.c.subject_ref == row["cycle_id"],
            events.events.c.kind == kind).order_by(events.events.c.id.desc()).limit(1)).scalar() if kind else None
        event_ids = [event_id] if event_id else []
    conn.execute(events.events.update().where(events.events.c.event_id.in_(event_ids)).values(relayed_at=None))


def verify_runtime(engine, cycle_id):
    """An interrupted cycle cannot silently resume under a different parser."""
    from app.clhear.l1.viewer_snapshot import request_refresh
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        matches = all(row[key] == value for key, value in runtime_identity().items())
        if matches:
            return True
        if row["status"] not in TERMINAL_CYCLE:
            result = {"cycle_id": cycle_id, "status": "failed", "execution_failed": True,
                      "reason": "worker_revision_changed_requires_new_cycle", "accepted_release": False,
                      "downstream": "held", "original_code_revision": row["code_revision"],
                      "original_worker_image_digest": row["worker_image_digest"], **runtime_identity()}
            conn.execute(children.update().where(children.c.cycle_id == cycle_id,
                children.c.status.not_in(TERMINAL_CHILD)).values(status="failed", result=result, finished_at=workflow.utcnow()))
            conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
                status="failed", result=result, finished_at=workflow.utcnow()))
            _terminal(conn, cycle_id)
            request_refresh(conn, reason="l1_cycle_worker_revision_changed", job_id=cycle_id)
        return False


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def runtime_identity():
    from app.clhear.l1 import http as l1_http, originals, translation
    from app.clhear.settings import get_settings
    settings = get_settings()
    revision = os.environ.get("CLHEAR_CODE_REVISION", "")
    image = os.environ.get("CLHEAR_WORKER_IMAGE_DIGEST", "")
    configuration = {name: getattr(settings, name) for name in (
        "clhear_fidelity_threshold", "clhear_ingest_max_attempts", "clhear_salvage_cap", "clhear_model_repair")}
    configuration.update(normalization=originals.NORMALIZATION_VERSION, offset_unit=originals.OFFSET_UNIT,
                         english_policy=translation._template_hash(), http_mode=l1_http._mode())
    return {"code_revision": revision if re.fullmatch(r"[0-9a-f]{40}", revision) else None,
            "worker_image_digest": image if re.fullmatch(r"sha256:[0-9a-f]{64}", image) else None,
            "parser_configuration_digest": digest(configuration)}


def adapter_keys():
    from app.clhear.l1.fleet import fleet_adapter_keys
    from app.clhear.l1.models import FLEET_SCHEDULES
    # A declared lane remains expected when its documents are not configured.
    return sorted(set(fleet_adapter_keys()) | set(FLEET_SCHEDULES))


def child_id_for(cycle_id, adapter):
    return "child-l1-" + digest([cycle_id, adapter])[:24]


def _row(conn, cycle_id, *, lock=False):
    query = sa.select(cycles).where(cycles.c.cycle_id == cycle_id)
    if lock:
        query = query.with_for_update()
    return dict(conn.execute(query).mappings().one())


def _emit(conn, kind, cycle_id, payload=None):
    return events.emit(conn, layer="l0" if kind in {"L1CycleRequested", "L1CycleAdvanceRequested"} else "l1",
                       kind=kind, subject_ref=cycle_id, payload={"cycle_id": cycle_id, **(payload or {})}, producer="l0.l1_cycle")


REPEAT_SUFFIX = "-repeat"


CYCLE_SCOPES = frozenset({"registered", "all_publishers"})


def request_cycle(engine, verification_id, *, scope="all_publishers", unchanged_repeat=False):
    """L0 CLI receipt only. No discovery, imports or acceptance in the caller.

    ``unchanged_repeat`` chains a second full cycle after this one finishes: the
    same scope again, expected to change nothing. Its evaluation compares every
    source version with the first cycle's and reports ``unchanged_repeat``.
    """
    if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
        raise ValueError("Only the L0 worker may request an L1 cycle")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", verification_id or ""):
        raise ValueError("A safe, unique verification ID is required")
    if scope not in CYCLE_SCOPES:
        raise ValueError("scope must be all_publishers or registered")
    cycle_id = "cycle-manual-" + verification_id
    payload = {"cycle_id": cycle_id, "scope": scope}
    if unchanged_repeat:
        payload["follow_up"] = {"kind": "unchanged_repeat", "cycle_id": cycle_id + REPEAT_SUFFIX}
    with engine.begin() as conn:
        # Stable request UUID prevents repeat CLI dispatch from creating work twice.
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, cycle_id))
        workflow._insert_once(conn, events.events, dict(event_id=event_id, layer="l0", kind="L1CycleRequested",
            subject_ref=cycle_id, payload=payload, producer="worker.cli", schema_version=1))
        original = conn.execute(sa.select(events.events.c.payload).where(events.events.c.event_id == event_id)).scalar_one()
        if {k: original.get(k) for k in ("cycle_id", "scope")} != {"cycle_id": cycle_id, "scope": scope}:
            raise ValueError("Verification ID is already bound to another scope")
    return {"cycle_id": cycle_id, "event_id": event_id, "status": "requested", "origin": "manual",
            "follow_up": payload.get("follow_up"), "scheduler_delivery_verified": False, **runtime_identity()}


def _create(conn, cycle_id, event_id, origin, scope, scheduled_for=None, manifest=None):
    workflow._insert_once(conn, cycles, dict(cycle_id=cycle_id, request_event_id=event_id,
        origin=origin, scope=scope, scheduled_for=scheduled_for, status="requested", manifest=manifest or {}, result={},
        created_at=workflow.utcnow(), **runtime_identity()))
    row = _row(conn, cycle_id, lock=True)
    if row["origin"] != origin or row["scope"] != scope:
        raise ValueError("Cycle identity belongs to another request scope or origin")
    return row


def start(engine, envelope):
    if envelope.producer == "eventbridge":
        raise ValueError("Per-adapter scheduler deliveries must retain their individual occurrence identities")
    scope = envelope.payload.get("scope", "all_publishers")
    if scope not in CYCLE_SCOPES:
        raise ValueError("L1 cycles cover the complete declared publisher scope")
    cycle_id = envelope.payload.get("cycle_id") or "cycle-manual-" + digest(envelope.event_id)[:24]
    if not re.fullmatch(r"cycle-manual-[A-Za-z0-9._-]{1,110}", cycle_id):
        raise ValueError("Invalid manual cycle ID")
    # Chain identity travels in the request and is frozen into the manifest here:
    # a repeat knows which cycle it must match, a first cycle knows what to request next.
    chain = {k: envelope.payload[k] for k in ("follow_up", "repeat_of") if envelope.payload.get(k)}
    if chain.get("follow_up") and (not isinstance(chain["follow_up"], dict) or chain["follow_up"].get("kind") != "unchanged_repeat"
                                   or not re.fullmatch(r"cycle-manual-[A-Za-z0-9._-]{1,110}", str(chain["follow_up"].get("cycle_id", "")))):
        raise ValueError("Unsupported cycle follow-up")
    with engine.begin() as conn:
        _lock_slot(conn)
        row = _create(conn, cycle_id, envelope.event_id, "manual", scope, manifest=chain)
        if row["request_event_id"] != envelope.event_id:
            raise ValueError("Cycle ID already belongs to another request event")
        if row["status"] == "requested":
            _enqueue(conn, cycle_id)
    reconcile(engine)
    with engine.connect() as conn:
        status = _row(conn, cycle_id)["status"]
    return {"cycle_id": cycle_id, "status": status, "origin": "manual"}


def plan_sources(engine, scope, audit_id=None):
    from app.clhear.l1.fleet import fleet_plan
    from app.clhear.l1 import inventory
    plans = {}
    for key in adapter_keys():
        source_keys = {adapter.meta().source_key for _, adapter in fleet_plan(key)}
        source_keys.update(entry["key"] for entry in inventory.planned_entries(engine, scope=scope, adapter_key=key,
                          **({"audit_id": audit_id} if audit_id else {})))
        plans[key] = sorted(source_keys)
    return plans


def discovered(engine, cycle_id, audit):
    with engine.connect() as conn:
        row = _row(conn, cycle_id)
    pending = audit.get("discovery", {}).get("pending_pages", 0)
    if pending:
        # A bounded page batch is not a finished traversal. Commit the next
        # command with its progress before acknowledging the current one.
        with engine.begin() as conn:
            row = _row(conn, cycle_id, lock=True)
            if row["status"] != "discovering":
                return {"cycle_id": cycle_id, "status": row["status"]}
            batches = list(row["result"].get("discovery_batches", []))
            progress_hash = digest({"pending_pages": pending, "inventory_hash": audit["inventory_hash"],
                "pages": sorted((p.get("publisher_id", ""), p.get("url", ""), p.get("sha256", ""))
                                for p in audit.get("discovery", {}).get("pages", []))})
            if batches and batches[-1].get("progress_hash") == progress_hash:
                # Let normal SQS visibility/backoff wait for active page leases;
                # do not generate an unbounded stream of identical continuations.
                raise workflow.RetryDeferred("Discovery frontier has no new completed progress; retry the persisted command")
            if audit["audit_id"] not in {batch["audit_id"] for batch in batches}:
                batches.append({"audit_id": audit["audit_id"], "inventory_hash": audit["inventory_hash"],
                                "pending_pages": pending, "duration_ms": audit.get("duration_ms"), "progress_hash": progress_hash})
                conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
                    result={**row["result"], "discovery_batches": batches, "pending_pages": pending}))
                _touch(conn, cycle_id, progress=True)
                _emit(conn, "L1CycleDiscoveryRequested", cycle_id, {"batch": len(batches) + 1})
        return {"cycle_id": cycle_id, "status": "discovering", "pending_pages": pending,
                "audit_id": audit["audit_id"], "next_batch_requested": True}
    plans = plan_sources(engine, row["scope"], audit["audit_id"])
    chain = {k: row["manifest"][k] for k in ("follow_up", "repeat_of") if row["manifest"].get(k)}
    manifest = {**chain, "adapter_keys": sorted(plans), "sources_by_adapter": plans,
                "expected_source_keys": sorted(s["source_key"] for s in audit["sources"]),
                "inventory_hash": audit["inventory_hash"], "audit_id": audit["audit_id"],
                "known_expected_is_lower_bound": audit.get("known_expected_is_lower_bound", True),
                "discovery_cycle_date": discovery_date(row),
                "frozen_at": workflow.utcnow().isoformat(), **runtime_identity()}
    manifest["manifest_hash"] = digest(manifest)
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] != "discovering":
            return {"cycle_id": cycle_id, "status": row["status"]}
        claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                               cycles.c.status == "discovering").values(status="planned", manifest=manifest,
                               result={**row["result"], "pending_pages": 0,
                                       "final_discovery_audit_id": audit["audit_id"]})).rowcount
        if claimed:
            _touch(conn, cycle_id, progress=True)
            _emit(conn, "L1CycleAdvanceRequested", cycle_id)
    return {"cycle_id": cycle_id, "status": "planned", "manifest_hash": manifest["manifest_hash"]}


def discovery_date(row):
    return workflow._aware(row["scheduled_for"] or row["created_at"]).astimezone(timezone.utc).date().isoformat()


def advance(engine, cycle_id):
    reconcile(engine)
    if not verify_runtime(engine, cycle_id):
        return {"cycle_id": cycle_id, "status": "failed", "reason": "worker_revision_changed_requires_new_cycle"}
    with engine.begin() as conn:
        _lock_slot(conn)
        row = _row(conn, cycle_id, lock=True)
        if row["status"] == "collecting":
            received = set(conn.execute(sa.select(children.c.adapter_key).where(children.c.cycle_id == cycle_id,
                children.c.event_time.is_not(None), children.c.event_id.is_not(None))).scalars())
            if received == set(row["manifest"]["adapter_keys"]):
                _enqueue(conn, cycle_id)
        elif row["status"] == "planned":
            claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                                   cycles.c.status == "planned").values(status="running")).rowcount
            for adapter in row["manifest"]["adapter_keys"] if claimed else []:
                child_id = child_id_for(cycle_id, adapter)
                job_id = workflow.job_id_for(child_id, adapter)
                event_id = events.emit(conn, layer="l1", kind="AdapterRunRequested", subject_ref=adapter,
                    producer="l0.l1_cycle", payload={"adapter": adapter, "cycle_id": cycle_id,
                                                     "child_id": child_id, "job_id": job_id})
                workflow._insert_once(conn, children, dict(child_id=child_id, cycle_id=cycle_id, adapter_key=adapter,
                    event_id=event_id, command_event_id=event_id, job_id=job_id, status="queued", result={},
                    source_keys=row["manifest"]["sources_by_adapter"][adapter], inventory_hash=row["manifest"]["inventory_hash"]))
                # A scheduled receipt's event ID/time are immutable provenance;
                # the work command has a different outbox identity.
                conn.execute(children.update().where(children.c.child_id == child_id).values(
                    command_event_id=event_id, job_id=job_id, status="queued",
                    source_keys=row["manifest"]["sources_by_adapter"][adapter], inventory_hash=row["manifest"]["inventory_hash"]))
        elif row["status"] == "running":
            found = list(conn.execute(sa.select(children).where(children.c.cycle_id == cycle_id)).mappings())
            expected = set(row["manifest"]["adapter_keys"])
            if {c["adapter_key"] for c in found} == expected and all(c["status"] in TERMINAL_CHILD for c in found):
                claimed = conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id,
                                       cycles.c.status == "running").values(status="evaluating")).rowcount
                if claimed:
                    _touch(conn, cycle_id, progress=True)
                    _emit(conn, "L1CycleEvaluationRequested", cycle_id)
    reconcile(engine)
    with engine.connect() as conn:
        state = _row(conn, cycle_id)
    return {"cycle_id": cycle_id, "status": state["status"]}


def scheduled_child(engine, envelope):
    """Group real adapter events by the advertised UTC slot, never by receipt day."""
    adapter = envelope.payload.get("adapter", envelope.subject_ref)
    if adapter not in adapter_keys():
        raise ValueError("Unknown scheduled L1 adapter")
    at = datetime.fromisoformat(envelope.ts.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("Scheduled occurrence must contain a timezone")
    at = at.astimezone(timezone.utc)
    if (at.hour, at.minute) != (0, 0):
        raise ValueError("Adapter event does not match its declared 00:00 UTC schedule")
    slot = at.replace(second=0, microsecond=0)
    cycle_id = "cycle-scheduled-" + slot.strftime("%Y%m%dT%H%MZ")
    child_id = child_id_for(cycle_id, adapter)
    with engine.begin() as conn:
        row = _create(conn, cycle_id, "eventbridge:" + slot.isoformat(), "scheduled", "all_publishers", slot)
        if not row["manifest"]:
            manifest = {"adapter_keys": adapter_keys(), "plan_mode": "discovery_after_all_scheduler_receipts",
                        "scheduled_for": slot.isoformat(), **runtime_identity()}
            manifest["manifest_hash"] = digest(manifest)
            conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(status="collecting", manifest=manifest))
        existing = conn.execute(sa.select(children).where(children.c.child_id == child_id)).mappings().first()
        if existing and existing["event_id"] != envelope.event_id:
            raise ValueError("A different scheduler event already owns this adapter occurrence")
        workflow._insert_once(conn, children, dict(child_id=child_id, cycle_id=cycle_id, adapter_key=adapter,
            event_id=envelope.event_id, event_time=envelope.ts, status="received", source_keys=[], result={}))
        _emit(conn, "L1CycleAdvanceRequested", cycle_id)
    return cycle_id, child_id


def child_context(engine, cycle_id, child_id, envelope):
    if not verify_runtime(engine, cycle_id):
        raise CycleRevisionChanged("Cycle cannot mix deployed worker revisions or image digests")
    with engine.begin() as conn:
        row = _row(conn, cycle_id)
        child = dict(conn.execute(sa.select(children).where(children.c.child_id == child_id,
                                                          children.c.cycle_id == cycle_id)).mappings().one())
        if child["command_event_id"] != envelope.event_id or child["adapter_key"] != envelope.payload.get("adapter", envelope.subject_ref):
            raise ValueError("Child request does not match its durable cycle binding")
        if child["job_id"] != envelope.payload.get("job_id"):
            raise ValueError("Child job identity differs from its frozen work command")
        conn.execute(children.update().where(children.c.child_id == child_id).values(
            status="running" if child["status"] not in TERMINAL_CHILD else child["status"],
            started_at=sa.func.coalesce(children.c.started_at, workflow.utcnow())))
    return {"cycle_id": cycle_id, "child_id": child_id, "scope": row["scope"], "origin": row["origin"],
            "source_keys": child["source_keys"] if child["inventory_hash"] else None,
            "inventory_hash": child["inventory_hash"], "audit_id": row["manifest"].get("audit_id"), "job_id": child["job_id"]}


def freeze_child(engine, context, job_id, keys, inventory_hash):
    with engine.begin() as conn:
        row = dict(conn.execute(sa.select(children).where(children.c.child_id == context["child_id"])
                               .with_for_update()).mappings().one())
        if row["inventory_hash"] and row["source_keys"] != sorted(keys):
            raise ValueError("Cycle child source set differs from its frozen manifest")
        conn.execute(children.update().where(children.c.child_id == context["child_id"]).values(
            job_id=job_id, source_keys=sorted(keys), inventory_hash=row["inventory_hash"] or inventory_hash))


def output_bindings(engine, cycle_id, audit):
    """A different concurrent cycle must not supply this cycle's claimed output."""
    from app.clhear.l1.source_registry import source_role
    current = {s["source_key"]: s for s in audit["sources"]}
    with engine.connect() as conn:
        frozen = set(_row(conn, cycle_id)["manifest"].get("expected_source_keys", []))
        rows = conn.execute(sa.select(workflow.tasks).where(workflow.tasks.c.job_id.in_(
            sa.select(children.c.job_id).where(children.c.cycle_id == cycle_id)))).mappings().all()
    bindings, missing, verified = [], set(), set()
    for row in rows:
        if row["source_key"] not in frozen:
            if source_role(row["source_key"]) not in {"collection", "reference"}:
                missing.add(row["source_key"])
            continue
        if row["status"] != "completed":
            missing.add(row["source_key"])
            continue
        summary = row["summary"] or {}
        expected = current.get(row["source_key"], {})
        binding = {"source_key": row["source_key"], "task_id": row["task_id"], "job_id": row["job_id"],
                   "source_version_id": summary.get("source_version_id"), "content_hash": summary.get("content_hash")}
        bindings.append(binding)
        if (not binding["source_version_id"] or not binding["content_hash"] or
                any(binding[k] != expected.get(k) for k in ("source_version_id", "content_hash"))):
            missing.add(row["source_key"])
        else:
            verified.add(row["source_key"])
    missing.update(frozen - verified)
    bindings.sort(key=lambda row: row["task_id"])
    return {"passed": bool(frozen) and not missing, "bindings_hash": digest(bindings),
            "bindings": bindings, "unverified_sources": sorted(missing)}


def finish_child(engine, context, result, *, retryable=False):
    status = "retrying" if retryable else "failed" if result.get("execution_failed") else (
        "completed_for_review" if result.get("failures") else "completed")
    with engine.begin() as conn:
        cycle = _row(conn, context["cycle_id"], lock=True)
        if cycle["status"] in TERMINAL_CYCLE:
            return cycle["status"]
        conn.execute(children.update().where(children.c.child_id == context["child_id"]).values(
            status=status, result=result, finished_at=None if retryable else workflow.utcnow()))
        _touch(conn, context["cycle_id"], progress=not retryable)
        _emit(conn, "L1CycleAdvanceRequested", context["cycle_id"])
    return status


def unhandled_child_error(engine, context, envelope, error):
    """Bound setup failures as well as source failures; never invent task success."""
    event_key = f"{envelope.event_id}:{envelope.ts}" if envelope.producer == "eventbridge" else envelope.event_id
    consumer = "fleet." + os.environ.get("CLHEAR_FLEET", "all").lower() + ":AdapterRunRequested"
    with engine.connect() as conn:
        child = conn.execute(sa.select(children).where(children.c.child_id == context["child_id"])).mappings().one()
        attempts = conn.execute(sa.select(workflow.deliveries.c.attempt).where(
            workflow.deliveries.c.event_key == event_key, workflow.deliveries.c.consumer == consumer)).scalar() or 1
    if child["status"] in TERMINAL_CHILD:
        return
    # Source retries have their own budget; do not overwrite their outcome.
    if isinstance(error, (workflow.LeaseBusy, workflow.RetryDeferred)) or child["result"].get("retryable"):
        return
    finish_child(engine, context, {"execution_failed": True, "error_type": type(error).__name__,
                                  "job_id": child["job_id"], "failures": ["child_setup_or_execution_failed"]},
                 retryable=attempts < workflow.MAX_ATTEMPTS)


def failed_phase(engine, cycle_id, phase, error, attempt):
    """After bounded infrastructure failures retain failed cycle evidence."""
    if attempt < workflow.MAX_ATTEMPTS or isinstance(error, (workflow.LeaseBusy, workflow.RetryDeferred, CycleRevisionChanged)):
        return
    from app.clhear.l1.viewer_snapshot import request_refresh
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] in TERMINAL_CYCLE:
            return
        result = {"status": "failed", "cycle_id": cycle_id, "phase": phase,
                  "error_type": type(error).__name__, "execution_failed": True,
                  "accepted_release": False, "downstream": "held"}
        conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
            status="failed", result=result, finished_at=workflow.utcnow()))
        _terminal(conn, cycle_id)
        request_refresh(conn, reason="l1_cycle_failed", job_id=cycle_id)


def finish_cycle(engine, cycle_id, result):
    """Final result and viewer refresh commit together; no release promotion."""
    from app.clhear.l1.viewer_snapshot import request_refresh
    with engine.begin() as conn:
        row = _row(conn, cycle_id, lock=True)
        if row["status"] in TERMINAL_CYCLE:
            return row["result"]
        found = list(conn.execute(sa.select(children).where(children.c.cycle_id == cycle_id)).mappings())
        if (row["status"] != "evaluating" or {c["adapter_key"] for c in found} != set(row["manifest"]["adapter_keys"])
                or any(c["status"] not in TERMINAL_CHILD for c in found)):
            raise ValueError("All cycle children must be terminal before final evaluation")
        result = {**result, "cycle_id": cycle_id, "origin": row["origin"], "accepted_release": False,
                  "downstream": "held", "finished_at": workflow.utcnow().isoformat(),
                  "duration_ms": int((workflow.utcnow() - workflow._aware(row["created_at"])).total_seconds() * 1000),
                  "scheduler_delivery_verified": row["origin"] == "scheduled" and all(c["event_time"] for c in found),
                  **{k: row[k] for k in runtime_identity()}}
        repeat_of = row["manifest"].get("repeat_of")
        if repeat_of:
            result["unchanged_repeat"] = _compare_repeat(conn, repeat_of, result)
        status = "failed" if any(c["status"] == "failed" for c in found) or result.get("execution_failed") else (
            "candidate_verified" if result.get("acceptance_passed") else "completed_for_review")
        result["status"] = status
        conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(
            status=status, result=result, finished_at=workflow.utcnow()))
        _terminal(conn, cycle_id)
        request_refresh(conn, reason="l1_cycle_finished", job_id=cycle_id)
        follow_up = row["manifest"].get("follow_up")
        if follow_up and status != "failed":
            # The unchanged-source repeat: one request, in the same transaction as the
            # result it follows; a failed first cycle is repaired, not repeated.
            next_id = follow_up["cycle_id"]
            workflow._insert_once(conn, events.events, dict(event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, next_id)),
                layer="l0", kind="L1CycleRequested", subject_ref=next_id,
                payload={"cycle_id": next_id, "scope": row["scope"], "repeat_of": cycle_id}, producer="l0.cycle_follow_up",
                schema_version=1))
            result["follow_up_requested"] = next_id
            conn.execute(cycles.update().where(cycles.c.cycle_id == cycle_id).values(result=result))
    return result


def _compare_repeat(conn, first_cycle_id, result):
    """Every source the first cycle bound must read back as the same version."""
    first = conn.execute(sa.select(cycles.c.result, cycles.c.status).where(cycles.c.cycle_id == first_cycle_id)).mappings().first()
    if first is None:
        return {"passed": False, "repeat_of": first_cycle_id, "reason": "first cycle is missing"}
    before = {b["source_key"]: (b.get("source_version_id"), b.get("content_hash"))
              for b in ((first["result"] or {}).get("output_bindings") or {}).get("bindings", [])}
    after = {b["source_key"]: (b.get("source_version_id"), b.get("content_hash"))
             for b in (result.get("output_bindings") or {}).get("bindings", [])}
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    missing = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    return {"passed": bool(before) and not changed and not missing, "repeat_of": first_cycle_id,
            "compared": len(before), "changed": changed, "missing": missing, "added": added,
            "first_status": first["status"], "reason": "verified_unchanged_repeat" if bool(before) and not changed and not missing else "repeat_not_verified"}


def read_query(conn):
    """Old immutable review snapshots remain readable with missing evidence."""
    schema = cycles.schema if conn.dialect.name == "postgresql" else None
    present = {c["name"] for c in sa.inspect(conn).get_columns(cycles.name, schema=schema)}
    return sa.select(*[column if column.name in present else sa.cast(sa.null(), column.type).label(column.name)
                       for column in cycles.c])


def cycle_summary(engine, cycle_id=None, *, offset=0, limit=100):
    offset, limit = max(0, offset), max(1, min(500, limit))
    with engine.connect() as conn:
        if not sa.inspect(conn).has_table(cycles.name, schema=cycles.schema if engine.dialect.name == "postgresql" else None):
            return {"status": "unavailable", "cycles": [], "children": [], "total": 0}
        query = read_query(conn)
        if cycle_id:
            query = query.where(cycles.c.cycle_id == cycle_id)
        total = conn.execute(sa.select(sa.func.count()).select_from(query.subquery())).scalar_one()
        rows = [dict(r) for r in conn.execute(query.order_by(cycles.c.created_at.desc()).offset(offset).limit(limit)).mappings()]
        child_rows = [dict(r) for r in conn.execute(sa.select(children).where(children.c.cycle_id.in_([r["cycle_id"] for r in rows]))
                                                  .order_by(children.c.adapter_key)).mappings()]
        queue_rows = ([dict(r) for r in conn.execute(sa.select(queue).where(
            queue.c.cycle_id.in_([r["cycle_id"] for r in rows])).order_by(queue.c.position)).mappings()]
            if sa.inspect(conn).has_table(queue.name, schema=queue.schema if engine.dialect.name == "postgresql" else None) else [])
    return {"status": "available", "cycles": rows, "children": child_rows, "total": total,
            "queue": queue_rows, "offset": offset, "limit": limit, "has_more": offset + len(rows) < total}


def schedule_evidence(engine, now=None, *, cycle_id=None):
    """Measure genuine scheduled occurrences, not a 24-hour bag of import runs."""
    if cycle_id is not None:
        if not re.fullmatch(r"cycle-scheduled-\d{8}T0000Z", cycle_id):
            raise ValueError("Schedule evidence requires an exact scheduled occurrence ID")
        slot = datetime.strptime(cycle_id.removeprefix("cycle-scheduled-"), "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    else:
        now = now or workflow.utcnow()
        slot = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        cycle_id = "cycle-scheduled-" + slot.strftime("%Y%m%dT%H%MZ")
    summary = cycle_summary(engine, cycle_id)
    state = summary["cycles"][0] if summary["cycles"] else None
    if state and (state["origin"] != "scheduled" or workflow._aware(state["scheduled_for"]) != slot):
        raise ValueError("Stored cycle does not match its scheduled occurrence ID")
    manifest = state["manifest"] if state else {}
    child_rows = summary["children"]
    expected_adapters = manifest.get("adapter_keys", adapter_keys())
    received = {c["adapter_key"] for c in child_rows if c["event_id"] and c["event_time"]}
    expected_sources, attempted, blocked = set(), set(), set()
    with engine.connect() as conn:
        for child in child_rows:
            expected_sources.update(child["source_keys"])
            if child["job_id"]:
                attempted.update(conn.execute(sa.select(workflow.tasks.c.source_key).where(
                    workflow.tasks.c.job_id == child["job_id"], workflow.tasks.c.attempt > 0)).scalars())
                blocked.update(conn.execute(sa.select(workflow.tasks.c.source_key).where(
                    workflow.tasks.c.job_id == child["job_id"], workflow.tasks.c.status == "blocked")).scalars())
    # Freeze the denominator to the occurrence. Later discovery cannot rewrite
    # an earlier day's task set; unresolved/unimportable documents stay included.
    if "expected_source_keys" in manifest:
        expected_sources.update(manifest["expected_source_keys"])
        lower_bound = manifest.get("known_expected_is_lower_bound", True)
    else:
        from app.clhear.l1 import inventory
        declared = inventory.inventory_summary(engine, scope="registered")
        expected_sources.update(s["source_key"] for s in declared.get("sources", []))
        if not declared.get("sources"):
            expected_sources.update(key for keys in plan_sources(engine, "registered").values() for key in keys)
        lower_bound = declared.get("known_expected_is_lower_bound", True)
    missing = sorted(expected_sources - attempted)
    missing_adapters = sorted(set(expected_adapters) - received)
    complete = not missing and not missing_adapters and bool(expected_sources)
    return {"cycle_id": cycle_id, "scheduled_for": slot.isoformat(), "origin": "scheduled",
            "scheduled_sources": len(expected_sources), "attempted_24h": len(attempted),
            "missed": missing, "missed_count": len(missing), "missing_adapters": missing_adapters,
            "received_adapters": len(received), "expected_adapters": len(expected_adapters),
            "blocked": sorted(blocked), "known_expected_is_lower_bound": lower_bound,
            "method": "scheduled occurrence IDs and frozen child task sets; manual attempts excluded",
            "delivery_verified": not missing_adapters, "all_documents_attempted": complete,
            "import_acceptance": (summary["cycles"][0]["status"] if summary["cycles"] else "not_started")}, complete
