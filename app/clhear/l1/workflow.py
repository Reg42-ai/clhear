# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Durable L0/L1 execution evidence and leased, resumable source tasks.

This is the worker's execution ledger, not an alternate import path. A claim
is a conditional database UPDATE; only its random lease token may heartbeat
or finish it. Retrying an envelope reuses its job and source task identifiers.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import threading
import time
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import Json, metadata

log = logging.getLogger(__name__)
LEASE_SECONDS = 180
MAX_ATTEMPTS = 3
WORKFLOW_STAGES = ["permission", "discovery", "database_reconciliation", "acquisition_parse",
                   "persistence", "readback_evals", "publication"]

jobs = sa.Table(
    "l1_workflow_jobs", metadata,
    sa.Column("job_id", sa.Text, primary_key=True),
    sa.Column("event_key", sa.Text, nullable=False),
    sa.Column("adapter_key", sa.Text, nullable=False),
    sa.Column("trigger", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("summary", Json, nullable=False, default=dict),
)
tasks = sa.Table(
    "l1_workflow_tasks", metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("job_id", sa.Text, nullable=False, index=True),
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("worker", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False, default=0),
    sa.Column("max_attempts", sa.Integer, nullable=False, default=MAX_ATTEMPTS),
    sa.Column("owner_token", sa.Text),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("queue_wait_ms", sa.BigInteger),
    sa.Column("summary", Json, nullable=False, default=dict),
    sa.Column("error", sa.Text),
    sa.UniqueConstraint("job_id", "source_key"),
)
source_leases = sa.Table(
    "l1_source_leases", metadata,
    sa.Column("source_key", sa.Text, primary_key=True),
    sa.Column("owner_token", sa.Text),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
)
steps = sa.Table(
    "l1_workflow_steps", metadata,
    sa.Column("step_id", sa.Text, primary_key=True),
    sa.Column("job_id", sa.Text, nullable=False, index=True),
    sa.Column("task_id", sa.Text, index=True),
    sa.Column("attempt", sa.Integer),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("duration_ms", sa.BigInteger),
    sa.Column("depends_on", Json, nullable=False, default=list),
    sa.Column("details", Json, nullable=False, default=dict),
)
deliveries = sa.Table(
    "worker_deliveries", metadata,
    sa.Column("consumer", sa.Text, primary_key=True),
    sa.Column("event_key", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("owner_token", sa.Text),
    sa.Column("attempt", sa.Integer, nullable=False, default=0),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("error", sa.Text),
)


class LeaseBusy(RuntimeError):
    """Another worker still owns this task or delivery."""


class LeaseLost(RuntimeError):
    """This worker no longer owns its lease; it cannot finish the task."""


class RetryDeferred(RuntimeError):
    """Task retry backoff has not elapsed."""


class AttemptsExhausted(RuntimeError):
    """An operator or new scheduled job is required after bounded attempts."""


def utcnow():
    return datetime.now(timezone.utc)


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _insert_once(conn, table, values):
    # Native ON CONFLICT avoids aborting a PostgreSQL transaction on races.
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    conn.execute(insert(table).values(**values).on_conflict_do_nothing())


def job_id_for(event_key: str, adapter_key: str) -> str:
    return "job-l1-" + hashlib.sha256(f"{event_key}\0{adapter_key}".encode()).hexdigest()[:24]


def ensure_job(engine, job_id, adapter_key, trigger, event_key):
    with engine.begin() as conn:
        _insert_once(conn, jobs, dict(job_id=job_id, adapter_key=adapter_key, trigger=trigger,
                     event_key=event_key, status="queued", created_at=utcnow(), summary={}))
        row = dict(conn.execute(sa.select(jobs).where(jobs.c.job_id == job_id)).mappings().one())
        if (row["adapter_key"], row["event_key"]) != (adapter_key, event_key):
            raise ValueError("job_id is already bound to a different request")
        return row


def update_job(engine, job_id, status, summary=None):
    values = {"status": status}
    if status == "running":
        values.update(started_at=sa.func.coalesce(jobs.c.started_at, utcnow()), finished_at=None)
    else:
        values["finished_at"] = utcnow()
    with engine.begin() as conn:
        existing = conn.execute(sa.select(jobs.c.summary).where(jobs.c.job_id == job_id)).scalar_one()
        if summary is not None:
            values["summary"] = {**(existing or {}), **summary}
        elif status == "running":
            values["summary"] = {**(existing or {}), "error": None}
        conn.execute(jobs.update().where(jobs.c.job_id == job_id).values(**values))


def ensure_task(engine, job_id, source_key, worker="l1.scribe"):
    task_id = "task-l1-" + hashlib.sha256(f"{job_id}\0{source_key}".encode()).hexdigest()[:24]
    with engine.begin() as conn:
        _insert_once(conn, tasks, dict(task_id=task_id, job_id=job_id, source_key=source_key,
                     worker=worker, status="queued", attempt=0, max_attempts=MAX_ATTEMPTS,
                     created_at=utcnow(), summary={}))
    return task_id


def task_info(engine, task_id):
    with engine.connect() as conn:
        return dict(conn.execute(sa.select(tasks).where(tasks.c.task_id == task_id)).mappings().one())


def claim_task(engine, task_id, *, now=None, lease_seconds=LEASE_SECONDS):
    now = now or utcnow()
    token = uuid.uuid4().hex
    with engine.begin() as conn:
        previous = conn.execute(sa.select(tasks).where(tasks.c.task_id == task_id)).mappings().one()
        result = conn.execute(tasks.update().where(
            tasks.c.task_id == task_id,
            tasks.c.status.in_(["queued", "retrying", "running"]),
            tasks.c.attempt < tasks.c.max_attempts,
            sa.or_(tasks.c.lease_until.is_(None), tasks.c.lease_until <= now),
            sa.or_(tasks.c.next_attempt_at.is_(None), tasks.c.next_attempt_at <= now),
        ).values(status="running", owner_token=token, attempt=tasks.c.attempt + 1,
                 lease_until=now + timedelta(seconds=lease_seconds), heartbeat_at=now,
                 started_at=now, finished_at=None, next_attempt_at=None, error=None))
        row = dict(conn.execute(sa.select(tasks).where(tasks.c.task_id == task_id)).mappings().one())
        if result.rowcount:
            _insert_once(conn, source_leases, {"source_key": row["source_key"]})
            owned = conn.execute(source_leases.update().where(
                source_leases.c.source_key == row["source_key"],
                sa.or_(source_leases.c.lease_until.is_(None), source_leases.c.lease_until <= now),
            ).values(owner_token=token, lease_until=now + timedelta(seconds=lease_seconds))).rowcount
            if not owned:
                # Raising rolls back the task claim and attempt increment too.
                raise LeaseBusy(f"Source is owned by another job: {row['source_key']}")
            conn.execute(tasks.update().where(tasks.c.task_id == task_id).values(
                queue_wait_ms=max(0, int((now - _aware(previous["next_attempt_at"] or previous["lease_until"]
                                                     or previous["created_at"])).total_seconds() * 1000))))
            # A reclaimed task leaves honest interrupted steps, never fabricated durations.
            conn.execute(steps.update().where(steps.c.task_id == task_id,
                         steps.c.status == "running", steps.c.attempt < row["attempt"]).values(status="interrupted"))
            return token
    if row["status"] in {"completed", "blocked"}:
        return None
    if row["attempt"] >= row["max_attempts"]:
        raise AttemptsExhausted(task_id)
    if _aware(row["next_attempt_at"]) and _aware(row["next_attempt_at"]) > now:
        raise RetryDeferred(task_id)
    raise LeaseBusy(task_id)


def heartbeat_task(engine, task_id, token, *, now=None, lease_seconds=LEASE_SECONDS):
    now = now or utcnow()
    with engine.begin() as conn:
        n = conn.execute(tasks.update().where(tasks.c.task_id == task_id,
                         tasks.c.owner_token == token, tasks.c.status == "running", tasks.c.lease_until > now)
                         .values(heartbeat_at=now, lease_until=now + timedelta(seconds=lease_seconds))).rowcount
        owned = conn.execute(source_leases.update().where(source_leases.c.owner_token == token,
                             source_leases.c.lease_until > now).values(
                             lease_until=now + timedelta(seconds=lease_seconds))).rowcount
        if not n or not owned:
            raise LeaseLost(task_id)


def _error_text(error):
    """The ledger's error column: redacted, never the driver's SQL or parameters."""
    if not error:
        return None
    from app.clhear.platform import failures
    described = None
    if isinstance(error, BaseException):
        described = failures.describe(error)
    elif isinstance(error, dict) and error.get("error_code"):
        described = error
    if described is not None:
        code = described["error_code"] + (f" [{described['sqlstate']}]" if described.get("sqlstate") else "")
        return f"{code}: {failures.redact(described.get('message') or '')}"[:1000]
    return failures.redact(str(error), limit=1000)


def finish_task(engine, task_id, token, *, status="completed", summary=None, error=None, now=None):
    now = now or utcnow()
    with engine.begin() as conn:
        row = conn.execute(sa.select(tasks).where(tasks.c.task_id == task_id)).mappings().one()
        if row["owner_token"] != token or row["status"] != "running" or _aware(row["lease_until"]) <= now:
            raise LeaseLost(task_id)
        terminal = status != "failed" or row["attempt"] >= row["max_attempts"]
        changed = conn.execute(tasks.update().where(tasks.c.task_id == task_id, tasks.c.owner_token == token,
                               tasks.c.status == "running", tasks.c.lease_until > now).values(
            status=("failed" if terminal else "retrying") if status == "failed" else status,
            owner_token=None, lease_until=None, summary=summary or {}, error=_error_text(error),
            finished_at=now if terminal else None,
            next_attempt_at=None if terminal else now + timedelta(seconds=min(900, 30 * (2 ** (row["attempt"] - 1)))),
        )).rowcount
        if not changed:
            raise LeaseLost(task_id)
        conn.execute(source_leases.update().where(source_leases.c.source_key == row["source_key"],
                     source_leases.c.owner_token == token).values(owner_token=None, lease_until=None))


def claim_delivery(engine, consumer, event_key, *, now=None, lease_seconds=LEASE_SECONDS):
    now = now or utcnow()
    token = uuid.uuid4().hex
    with engine.begin() as conn:
        _insert_once(conn, deliveries, dict(consumer=consumer, event_key=event_key, status="queued", attempt=0))
        n = conn.execute(deliveries.update().where(deliveries.c.consumer == consumer,
                         deliveries.c.event_key == event_key, deliveries.c.status != "completed",
                         sa.or_(deliveries.c.lease_until.is_(None), deliveries.c.lease_until <= now)).values(
            status="running", owner_token=token, attempt=deliveries.c.attempt + 1,
            heartbeat_at=now, lease_until=now + timedelta(seconds=lease_seconds), error=None)).rowcount
        if n:
            return token
        row = conn.execute(sa.select(deliveries.c.status).where(deliveries.c.consumer == consumer,
                           deliveries.c.event_key == event_key)).scalar_one()
    if row == "completed":
        return None
    raise LeaseBusy(event_key)


def heartbeat_delivery(engine, consumer, event_key, token, *, now=None, lease_seconds=LEASE_SECONDS):
    now = now or utcnow()
    with engine.begin() as conn:
        n = conn.execute(deliveries.update().where(deliveries.c.consumer == consumer,
                         deliveries.c.event_key == event_key, deliveries.c.owner_token == token,
                         deliveries.c.status == "running", deliveries.c.lease_until > now).values(
            heartbeat_at=now, lease_until=now + timedelta(seconds=lease_seconds))).rowcount
    if not n:
        raise LeaseLost(event_key)


def finish_delivery(engine, consumer, event_key, token, error=None):
    now = utcnow()
    with engine.begin() as conn:
        n = conn.execute(deliveries.update().where(deliveries.c.consumer == consumer,
                         deliveries.c.event_key == event_key, deliveries.c.owner_token == token,
                         deliveries.c.status == "running", deliveries.c.lease_until > now).values(
            status="failed" if error else "completed", owner_token=None, lease_until=None,
            finished_at=now, error=_error_text(error))).rowcount
    if not n:
        raise LeaseLost(event_key)


@contextmanager
def heartbeat(callback, *, interval_seconds=45):
    """Refresh a lease while slow adapters/providers run; surface lease loss."""
    stop = threading.Event()
    errors = []
    callback()
    def renew():
        while not stop.wait(interval_seconds):
            try:
                callback()
            except Exception as exc:
                errors.append(exc)
                log.exception("worker lease heartbeat failed")
                return
    thread = threading.Thread(target=renew, daemon=True, name="clhear-lease-heartbeat")
    thread.start()
    try:
        yield
        if errors:
            raise errors[0]
    finally:
        stop.set()
        thread.join(timeout=5)


_context = ContextVar("l1_workflow_execution", default=None)


@contextmanager
def bind_execution(engine, job_id, task_id=None, owner_token=None):
    attempt = task_info(engine, task_id)["attempt"] if task_id else None
    token = _context.set(dict(engine=engine, job_id=job_id, task_id=task_id, attempt=attempt, owner_token=owner_token))
    try:
        yield
    finally:
        _context.reset(token)


def execution_context():
    return {k: v for k, v in (_context.get() or {}).items() if k not in {"engine", "last_step_id", "stage"}}


def current_stage():
    """The innermost open workflow stage on this execution, if any."""
    return (_context.get() or {}).get("stage")


def assert_ownership(conn=None):
    """Fence persistence against reclaimed leases; call inside its transaction."""
    context = _context.get()
    if not context or not context.get("task_id"):
        return
    def check(connection):
        query = sa.select(tasks.c.task_id).join(source_leases, tasks.c.source_key == source_leases.c.source_key).where(
            tasks.c.task_id == context["task_id"], tasks.c.owner_token == context["owner_token"],
            tasks.c.status == "running", tasks.c.lease_until > utcnow(),
            source_leases.c.owner_token == context["owner_token"], source_leases.c.lease_until > utcnow(),
        )
        if connection.dialect.name == "postgresql":
            query = query.with_for_update()
        if not connection.execute(query).first():
            raise LeaseLost(context["task_id"])
    if conn is not None:
        check(conn)
    else:
        with context["engine"].connect() as connection:
            check(connection)


class StageRecorder:
    def __init__(self, name, details=None):
        self.context = _context.get()
        self.stage = name
        self.details = dict(details or {})
        self.status = "succeeded"
        self.step_id = uuid.uuid4().hex

    def __enter__(self):
        self.started = time.monotonic()
        if self.context:
            self._outer_stage = self.context.get("stage")
            self.context["stage"] = self.stage
            assert_ownership()
            with self.context["engine"].begin() as conn:
                conn.execute(steps.insert().values(step_id=self.step_id, job_id=self.context["job_id"],
                    task_id=self.context["task_id"], attempt=self.context["attempt"], stage=self.stage,
                    status="running", started_at=utcnow(), details=self.details,
                    depends_on=[self.context["last_step_id"]] if self.context.get("last_step_id") else []))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.context:
            if exc:
                from app.clhear.platform import failures
                self.status = "failed"
                described = failures.describe(exc)
                self.details["error"] = described["message"]
                self.details["error_code"] = described["error_code"]
                self.details["sqlstate"] = described["sqlstate"]
            self.context["stage"] = getattr(self, "_outer_stage", None)
            assert_ownership()
            with self.context["engine"].begin() as conn:
                conn.execute(steps.update().where(steps.c.step_id == self.step_id, steps.c.status == "running").values(
                    status=self.status, finished_at=utcnow(), duration_ms=int((time.monotonic()-self.started)*1000),
                    details=self.details))
            self.context["last_step_id"] = self.step_id
        return False


def stage(name, details=None):
    return StageRecorder(name, details)


def workflow_summary(engine: Engine, job_id=None, source_key=None, *, task_offset=0, task_limit=200,
                     step_offset=0, step_limit=500, job_offset=0, job_limit=50):
    empty = dict(jobs=[], tasks=[], steps=[], workflow_stages=WORKFLOW_STAGES)
    schema = None if engine.dialect.name == "sqlite" else metadata.schema
    inspector = sa.inspect(engine)
    if not all(inspector.has_table(table.name, schema=schema) for table in (jobs, tasks, steps)):
        return {**empty, "status": "unavailable", "reason": "Workflow migration has not been applied."}
    task_offset, step_offset, job_offset = (max(0, int(n)) for n in (task_offset, step_offset, job_offset))
    task_limit, step_limit, job_limit = (max(1, min(int(n), 1000)) for n in (task_limit, step_limit, job_limit))
    with engine.connect() as conn:
        query = sa.select(jobs)
        if job_id:
            query = query.where(jobs.c.job_id == job_id)
        elif source_key:
            query = query.where(jobs.c.job_id.in_(sa.select(tasks.c.job_id).where(tasks.c.source_key == source_key)))
        job_total = conn.execute(sa.select(sa.func.count()).select_from(query.subquery())).scalar_one()
        found_jobs = list(conn.execute(query.order_by(jobs.c.created_at.desc(), jobs.c.job_id)
                                      .offset(job_offset).limit(job_limit)).mappings())
        ids = {r["job_id"] for r in found_jobs}
        task_query = sa.select(tasks).where(tasks.c.job_id.in_(ids))
        if source_key:
            task_query = task_query.where(tasks.c.source_key == source_key)
        task_total = conn.execute(sa.select(sa.func.count()).select_from(task_query.subquery())).scalar_one()
        found_tasks = list(conn.execute(task_query.order_by(tasks.c.created_at.desc(), tasks.c.task_id)
                                       .offset(task_offset).limit(task_limit)).mappings())
        step_query = sa.select(steps).where(steps.c.job_id.in_(ids))
        if source_key:
            step_query = step_query.where(sa.or_(steps.c.task_id.is_(None), steps.c.task_id.in_(
                sa.select(tasks.c.task_id).where(tasks.c.job_id.in_(ids), tasks.c.source_key == source_key))))
        step_total = conn.execute(sa.select(sa.func.count()).select_from(step_query.subquery())).scalar_one()
        found_steps = list(conn.execute(step_query.order_by(steps.c.started_at.desc(), steps.c.step_id)
                                       .offset(step_offset).limit(step_limit)).mappings())
    def clean(row):
        out = {k: (_aware(v).isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k != "owner_token"}
        if row.get("status") == "running" and row.get("lease_until") and _aware(row["lease_until"]) <= utcnow():
            out["stored_status"] = out["status"]
            out["status"] = "lease_expired"
        return out
    return dict(status="available", workflow_stages=WORKFLOW_STAGES, jobs=[clean(r) for r in found_jobs],
                tasks=[clean(r) for r in found_tasks if r["job_id"] in ids],
                steps=[clean(r) for r in reversed(found_steps)],
                job_total=job_total, task_total=task_total, step_total=step_total,
                pagination={"jobs": {"offset": job_offset, "limit": job_limit, "has_more": job_offset + len(found_jobs) < job_total},
                            "tasks": {"offset": task_offset, "limit": task_limit, "has_more": task_offset + len(found_tasks) < task_total},
                            "steps": {"offset": step_offset, "limit": step_limit, "has_more": step_offset + len(found_steps) < step_total}})
