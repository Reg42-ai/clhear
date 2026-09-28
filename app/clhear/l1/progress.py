# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""A small progress record L0 publishes between full viewer snapshots.

The candidate viewer database is republished only when a job finishes. This
record is a few kilobytes of state read from the ledgers (never text) and is
published beside the candidate as ``progress.json``. It keeps three questions
apart, because their answers come from different evidence:

* **corpus verification**: what did the last technical L1 cycle find?
* **publisher permissions**: how much of the scope has reviewed publisher
  permission, and how much is blocked or denied?
* **nightly validation**: has a scheduled cycle been observed and kept?

``viewer`` carries the viewer link, the deployed revision and the snapshot
timestamp it refers to.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import runs

log = logging.getLogger(__name__)
SCHEMA = "clhear.l1-progress.v1"
PUBLISH_INTERVAL_SECONDS = 30
_last_publish = {"at": 0.0, "hash": None}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _has(engine: Engine, table) -> bool:
    schema = None if engine.dialect.name == "sqlite" else table.schema
    return sa.inspect(engine).has_table(table.name, schema=schema)


def _corpus_verification(engine: Engine) -> dict:
    from app.clhear.l1 import cycles
    if not _has(engine, cycles.cycles):
        return {"state": "unavailable"}
    summary = cycles.cycle_summary(engine, limit=1)
    latest = (summary.get("cycles") or [None])[0]
    if not latest:
        return {"state": "no_cycle_recorded"}
    result = latest.get("result") or {}
    return {"state": latest.get("status"), "cycle_id": latest.get("cycle_id"), "scope": latest.get("scope"),
            "origin": latest.get("origin"), "created_at": str(latest.get("created_at") or ""),
            "finished_at": str(latest.get("finished_at") or ""), "acceptance_passed": result.get("acceptance_passed"),
            "verified": result.get("verified"), "unresolved": result.get("unresolved"),
            "known_expected": result.get("known_expected"), "pending_pages": result.get("pending_pages"),
            "children": len(latest.get("children") or [])}


def _publisher_permissions(engine: Engine) -> dict:
    from app.clhear.l1 import discovery, permissions
    out = {"state": "unknown"}
    with engine.connect() as conn:
        if _has(engine, permissions.source_permissions):
            rows = conn.execute(sa.select(permissions.source_permissions.c.approved, sa.func.count())
                                .group_by(permissions.source_permissions.c.approved)).all()
            out["ledger"] = {"approved": sum(n for a, n in rows if a), "denied": sum(n for a, n in rows if not a)}
        if _has(engine, discovery.pages):
            statuses = dict(conn.execute(sa.select(discovery.pages.c.status, sa.func.count()).group_by(discovery.pages.c.status)).all())
            out["discovery_pages"] = statuses
            out["permission_blocked"] = statuses.get("permission_blocked", 0)
            denied = conn.execute(sa.select(sa.func.count()).select_from(discovery.pages).where(
                discovery.pages.c.status == "permission_blocked",
                discovery.pages.c.result["publisher_denied"].as_string().in_(["true", "1"]))).scalar_one() \
                if engine.dialect.name == "postgresql" else None
            out["publisher_denied_pages"] = denied
    out["state"] = "publisher_permission_required" if out.get("permission_blocked") else "no_permission_waits"
    return out


def _nightly(engine: Engine) -> dict:
    from app.clhear.l1 import cycles
    if not _has(engine, cycles.cycles):
        return {"state": "pending"}
    with engine.connect() as conn:
        row = conn.execute(sa.select(cycles.cycles.c.cycle_id, cycles.cycles.c.status, cycles.cycles.c.finished_at, cycles.cycles.c.result)
                           .where(cycles.cycles.c.origin == "scheduled").order_by(cycles.cycles.c.created_at.desc()).limit(1)).mappings().first()
    if row is None:
        return {"state": "pending", "reason": "no scheduled cycle observed yet"}
    result = row["result"] or {}
    return {"state": "observed" if row["status"] in cycles.TERMINAL_CYCLE else "running", "cycle_id": row["cycle_id"],
            "status": row["status"], "finished_at": str(row["finished_at"] or ""),
            "schedule_kept": (result.get("evals") or {}).get("l1_schedule_kept", {}).get("passed"),
            "nightly_schedule_validation": result.get("nightly_schedule_validation", "pending")}


def _viewer(engine: Engine) -> dict:
    """The last candidate viewer publication, from the L0 delivery ledger (runs)."""
    with engine.connect() as conn:
        row = conn.execute(sa.select(runs.c.outputs, runs.c.created_at).where(
            runs.c.fleet == "worker", runs.c.trigger == "ViewerSnapshotRequested").order_by(runs.c.id.desc()).limit(1)).mappings().first()
    if row is None:
        return {}
    out = row["outputs"] if isinstance(row["outputs"], dict) else json.loads(row["outputs"] or "{}")
    return {k: out.get(k) for k in ("revision", "generated_at", "snapshot_uri", "sha256", "byte_count", "source_environment")}


def compose(engine: Engine) -> dict:
    from app.clhear.platform import deferred
    corpus = _corpus_verification(engine)
    permissions_state = _publisher_permissions(engine)
    nightly = _nightly(engine)
    viewer = _viewer(engine)
    ready = bool(viewer.get("snapshot_uri"))
    record = {
        "schema": SCHEMA, "generated_at": _now(), "generated_by": os.environ.get("CLHEAR_FLEET", "local").lower(),
        "code_revision": os.environ.get("CLHEAR_CODE_REVISION") or None,
        "states": {"corpus_verification": corpus,
                   "publisher_permissions": permissions_state, "nightly_validation": nightly},
        "deferred_messages": deferred.counts(engine),
        "viewer": {
            "ready": ready,
            "viewer_url": (os.environ.get("CLHEAR_PUBLIC_BASE_URL", "").rstrip("/") + "/l1") if ready else None,
            "deployed_revision": os.environ.get("CLHEAR_CODE_REVISION") or None,
            "snapshot_generated_at": viewer.get("generated_at"), "snapshot_revision": viewer.get("revision"),
            "corpus_acceptance": "not_claimed", "reason": None if ready else "no viewer snapshot published",
        },
    }
    return record


def publish(engine: Engine, *, s3_client=None, force: bool = False) -> dict | None:
    """Publish beside the candidate viewer (``…/progress.json``); at most once per
    interval unless forced, and only when something changed."""
    from app.clhear.l1.viewer_snapshot import configured_uri
    uri = configured_uri()
    if not uri or not uri.startswith("s3://"):
        return None
    now = time.monotonic()
    if not force and now - _last_publish["at"] < PUBLISH_INTERVAL_SECONDS:
        return None
    record = compose(engine)
    body = json.dumps(record, sort_keys=True, default=str)
    digest = json.dumps({k: v for k, v in record.items() if k != "generated_at"}, sort_keys=True, default=str)
    if not force and digest == _last_publish["hash"]:
        _last_publish["at"] = now
        return None
    bucket, key = uri[len("s3://"):].split("/", 1)
    target = key.rsplit("/", 1)[0] + "/progress.json"
    if s3_client is None:
        import boto3
        from app.clhear.settings import get_settings
        s3_client = boto3.client("s3", region_name=get_settings().aws_region)
    s3_client.put_object(Bucket=bucket, Key=target, Body=body.encode("utf-8"), ContentType="application/json",
                         ServerSideEncryption="AES256")
    _last_publish.update(at=now, hash=digest)
    return {"uri": f"s3://{bucket}/{target}", "bytes": len(body), "generated_at": record["generated_at"]}


def read_published(*, s3_client=None) -> dict | None:
    """The viewer's side: the last record L0 published, or None."""
    from app.clhear.l1.viewer_snapshot import configured_uri
    uri = os.environ.get("CLHEAR_DB_S3_URI") or configured_uri()
    if not uri or not uri.startswith("s3://"):
        return None
    bucket, key = uri[len("s3://"):].split("/", 1)
    target = key.rsplit("/", 1)[0] + "/progress.json"
    if s3_client is None:
        import boto3
        from app.clhear.settings import get_settings
        s3_client = boto3.client("s3", region_name=get_settings().aws_region)
    try:
        body = s3_client.get_object(Bucket=bucket, Key=target)["Body"].read()
    except Exception as exc:  # noqa: BLE001 — absent or unreadable is "not published yet"
        log.info("no published progress record at s3://%s/%s: %s", bucket, target, type(exc).__name__)
        return None
    return json.loads(body)
