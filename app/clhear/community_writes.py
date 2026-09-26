# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Community write path.

The public web app runs on a READ-ONLY corpus snapshot (Lambda pulls it from
S3; the ingestion worker is the single writer). Community writes therefore
flow as operations: applied directly when the DB is writable (worker, dev,
tests), or enqueued to the community SQS queue for the worker to apply and
publish on its next snapshot push.

# ARCH: when Aurora is wired (HLD §5) the queue hop disappears and these ops
# become plain writes; the op schema is the migration seam.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.community_models import submissions, users, votes
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.community")

USER_NS = uuid.UUID("2f1aeb2e-90a1-4c56-9c92-aa17c02b61e4")


def user_id_for(email: str) -> str:
    """Deterministic contributor id: identical on every writer, no round-trip."""
    return str(uuid.uuid5(USER_NS, email.strip().lower()))


def snapshot_readonly() -> bool:
    import os

    return bool(os.environ.get("CLHEAR_DB_S3_URI"))


def dispatch(engine: Engine, op: dict) -> dict:
    """Apply now (writable DB) or enqueue for the single writer.

    The queued form is a CommunityWrite envelope on the clhear-events queue:
    the worker applies it and the change reaches the public snapshot on the
    next publish (a minute or two). The UI says so honestly.
    """
    if not snapshot_readonly():
        return apply_op(engine, op)
    settings = get_settings()
    queue = settings.clhear_events_queue_url
    if not queue:
        raise RuntimeError("events queue is not configured on this read-only deployment")
    import boto3

    envelope = {
        "event_id": str(uuid.uuid4()),
        "layer": "community",
        "kind": "CommunityWrite",
        "subject_ref": op.get("obligation_id") or op.get("id") or op.get("email", ""),
        "payload": op,
        "schema_version": 1,
        "producer": "webui",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    boto3.client("sqs", region_name=settings.aws_region).send_message(
        QueueUrl=queue, MessageBody=json.dumps(envelope, default=str)
    )
    return {"queued": True, "note": "recorded — appears on the site after the next snapshot publish (~2 min)",
            **{k: op.get(k) for k in ("op", "id", "obligation_id") if op.get(k)}}


def apply_op(engine: Engine, op: dict) -> dict:
    kind = op["op"]
    if kind == "upsert_user":
        return _apply_upsert_user(engine, op)
    if kind == "create_submission":
        return _apply_create_submission(engine, op)
    if kind == "vote":
        return _apply_vote(engine, op)
    raise ValueError(f"unknown community op {kind}")


def _apply_upsert_user(engine: Engine, op: dict) -> dict:
    email = op["email"].strip().lower()
    uid = user_id_for(email)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        row = conn.execute(sa.select(users).where(users.c.id == uid)).first()
        if row is None:
            conn.execute(
                users.insert().values(
                    id=uid, email=email,
                    display_name=op.get("display_name") or email.split("@")[0],
                    provider=op.get("provider", "email"), provider_sub=op.get("provider_sub", ""),
                    last_login_at=now,
                )
            )
        else:
            conn.execute(users.update().where(users.c.id == uid).values(last_login_at=now))
    return {"id": uid, "email": email, "display_name": op.get("display_name") or email.split("@")[0]}


def _apply_create_submission(engine: Engine, op: dict) -> dict:
    from app.clhear.platform import proposals as l0_proposals

    _apply_upsert_user(engine, {"op": "upsert_user", "email": op["submitter_email"],
                                "display_name": op.get("submitter_name", "")})
    with engine.begin() as conn:
        existing = conn.execute(sa.select(submissions.c.id).where(submissions.c.id == op["id"])).first()
        if existing:
            return {"id": op["id"], "status": "duplicate"}
        proposal_id = l0_proposals.create_proposal(
            conn,
            layer="community",
            kind=f"community_{op['kind']}",
            subject_ref=op.get("target_id") or op.get("target_layer") or "general",
            draft={"submission_id": op["id"], "title": op["title"], "body": op.get("body", "")[:4000],
                   "evidence_url": op.get("evidence_url", "")[:500], "submitter": op["submitter_email"]},
            rationale=f"community case by {op.get('submitter_name') or op['submitter_email']}",
        )
        conn.execute(
            submissions.insert().values(
                id=op["id"], kind=op["kind"],
                target_layer=op.get("target_layer", "")[:8], target_id=op.get("target_id", "")[:300],
                title=op["title"], body=op.get("body", "")[:4000],
                evidence_url=op.get("evidence_url", "")[:500],
                submitter_id=user_id_for(op["submitter_email"]), proposal_id=proposal_id,
            )
        )
    if op.get("kind") == "correction" and op.get("target_id"):
        try:
            from app.clhear.governance import file_correction

            file_correction(
                engine,
                layer=op.get("target_layer") or "L2",
                subject_ref=op["target_id"],
                filed_by=op["submitter_email"],
                body=op.get("body") or op.get("title") or "",
                submission_id=op["id"],
            )
        except Exception:
            log.exception("file_correction from community submission failed")
    return {"id": op["id"], "status": "new", "proposal_id": proposal_id}


def _apply_vote(engine: Engine, op: dict) -> dict:
    _apply_upsert_user(engine, {"op": "upsert_user", "email": op["voter_email"],
                                "display_name": op.get("voter_name", "")})
    uid = user_id_for(op["voter_email"])
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(votes.c.id).where(votes.c.obligation_id == op["obligation_id"]).where(votes.c.user_id == uid)
        ).first()
        if existing:
            conn.execute(
                votes.update().where(votes.c.id == existing.id).values(vote=op["vote"], comment=op.get("comment", "")[:1000])
            )
        else:
            conn.execute(
                votes.insert().values(
                    obligation_id=op["obligation_id"], user_id=uid, vote=op["vote"], comment=op.get("comment", "")[:1000]
                )
            )
    return {"obligation_id": op["obligation_id"], "vote": op["vote"], "applied": True}
