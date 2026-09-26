# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Community participation: cases (submissions), validation votes, wall.

Flow: contributor opens a case or votes -> the case mirrors into the l0
proposals queue (audit + single review surface) -> a maintainer decides ->
the case status syncs and the contributor is publicly credited on acceptance.
Community confirms upgrade nothing by themselves: promotion of an obligation
to `validated` is always a recorded maintainer action (named-human gate).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.engine import Engine

from app.clhear.accounts import current_user, require_user
from app.clhear.community_models import SUBMISSION_KINDS, submissions, users, votes
from app.clhear.db import get_engine
from app.clhear.derived_models import obligations
from app.clhear.platform import proposals as l0_proposals
from app.clhear.settings import get_settings

router = APIRouter(prefix="/api/clhear/community", tags=["community"])

PROMOTION_CONFIRMS = 3  # confirms with zero disputes suggests promotion


def _display_names(engine: Engine, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    with engine.connect() as conn:
        return {
            row.id: row.display_name or "contributor"
            for row in conn.execute(sa.select(users.c.id, users.c.display_name).where(users.c.id.in_(user_ids)))
        }


# ------------------------------------------------------------------- cases


@router.post("/submissions")
async def create_submission(request: Request, user: dict = Depends(require_user)) -> dict:
    body = await request.json()
    if body.get("website"):  # honeypot
        return {"ok": True}
    kind = body.get("kind", "")
    if kind not in SUBMISSION_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {SUBMISSION_KINDS}")
    title = (body.get("title") or "").strip()
    if not 5 <= len(title) <= 200:
        raise HTTPException(status_code=400, detail="title must be 5-200 characters")
    engine = get_engine()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with engine.connect() as conn:
        recent = conn.execute(
            sa.select(sa.func.count())
            .select_from(submissions)
            .where(submissions.c.submitter_id == user["id"])
            .where(submissions.c.created_at >= since)
        ).scalar_one()
    if recent >= get_settings().clhear_submissions_daily_limit:
        raise HTTPException(status_code=429, detail="daily submission limit reached — thank you for the enthusiasm")
    import uuid

    from app.clhear import community_writes

    submission_id = str(uuid.uuid4())
    result = community_writes.dispatch(
        engine,
        {
            "op": "create_submission",
            "id": submission_id,
            "kind": kind,
            "target_layer": (body.get("target_layer") or "")[:8],
            "target_id": (body.get("target_id") or "")[:300],
            "title": title,
            "body": (body.get("body") or "")[:4000],
            "evidence_url": (body.get("evidence_url") or "")[:500],
            "submitter_email": user["email"],
            "submitter_name": user["display_name"],
        },
    )
    return {"id": submission_id, "status": "queued" if result.get("queued") else "new", **({"note": result["note"]} if result.get("note") else {})}


@router.get("/submissions")
def list_submissions(request: Request, mine: bool = False, status: str | None = None,
                     limit: int = Query(default=100, ge=1, le=300)) -> list[dict]:
    engine = get_engine()
    query = sa.select(submissions).order_by(submissions.c.created_at.desc()).limit(limit)
    if mine:
        user = require_user(request)
        query = query.where(submissions.c.submitter_id == user["id"])
    if status:
        query = query.where(submissions.c.status == status)
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(query).mappings()]
    names = _display_names(engine, {r["submitter_id"] for r in rows})
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"], "kind": r["kind"], "target_layer": r["target_layer"], "target_id": r["target_id"],
                "title": r["title"], "body": r["body"], "evidence_url": r["evidence_url"], "status": r["status"],
                "resolution": r["resolution"], "created_at": str(r["created_at"]),
                "decided_at": str(r["decided_at"]) if r["decided_at"] else None,
                "contributor": names.get(r["submitter_id"], "contributor"),
            }
        )
    return out


def sync_submission_from_proposal(engine: Engine, proposal: dict) -> None:
    """Called when a community_* proposal is decided in the review console."""
    draft = proposal.get("draft") or {}
    submission_id = draft.get("submission_id")
    if not submission_id:
        return
    status = "accepted" if proposal.get("status") == "approved" else "rejected"
    with engine.begin() as conn:
        conn.execute(
            submissions.update()
            .where(submissions.c.id == submission_id)
            .values(status=status, decided_by=proposal.get("approver"), decided_at=datetime.now(timezone.utc))
        )


# ------------------------------------------------------------------- votes


@router.post("/obligations/{obligation_id:path}/vote")
async def vote_obligation(obligation_id: str, request: Request, user: dict = Depends(require_user)) -> dict:
    body = await request.json()
    choice = body.get("vote")
    if choice not in ("confirm", "dispute"):
        raise HTTPException(status_code=400, detail="vote must be confirm or dispute")
    engine = get_engine()
    with engine.connect() as conn:
        ob = conn.execute(sa.select(obligations.c.id).where(obligations.c.id == obligation_id)).first()
    if ob is None:
        raise HTTPException(status_code=404, detail="obligation not found")
    from app.clhear import community_writes

    result = community_writes.dispatch(
        engine,
        {
            "op": "vote",
            "obligation_id": obligation_id,
            "vote": choice,
            "comment": (body.get("comment") or "")[:1000],
            "voter_email": user["email"],
            "voter_name": user["display_name"],
        },
    )
    return {
        "obligation_id": obligation_id,
        "queued": bool(result.get("queued")),
        **({"note": result["note"]} if result.get("note") else {}),
        **vote_tally(engine, obligation_id),
    }


def vote_tally(engine: Engine, obligation_id: str) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(votes.c.vote, sa.func.count().label("n"))
            .where(votes.c.obligation_id == obligation_id)
            .group_by(votes.c.vote)
        ).all()
    tally = {"confirm": 0, "dispute": 0}
    for row in rows:
        tally[row.vote] = row.n
    tally["promotion_suggested"] = tally["confirm"] >= PROMOTION_CONFIRMS and tally["dispute"] == 0
    return tally


def vote_tallies(engine: Engine, obligation_ids: list[str]) -> dict[str, dict]:
    if not obligation_ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(votes.c.obligation_id, votes.c.vote, sa.func.count().label("n"))
            .where(votes.c.obligation_id.in_(obligation_ids))
            .group_by(votes.c.obligation_id, votes.c.vote)
        ).all()
    out: dict[str, dict] = {}
    for row in rows:
        slot = out.setdefault(row.obligation_id, {"confirm": 0, "dispute": 0})
        slot[row.vote] = row.n
    for slot in out.values():
        slot["promotion_suggested"] = slot["confirm"] >= PROMOTION_CONFIRMS and slot["dispute"] == 0
    return out


@router.get("/obligations/{obligation_id:path}/votes")
def obligation_votes(obligation_id: str, request: Request) -> dict:
    engine = get_engine()
    tally = vote_tally(engine, obligation_id)
    me = current_user(request)
    my_vote = None
    if me:
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(votes.c.vote).where(votes.c.obligation_id == obligation_id).where(votes.c.user_id == me["id"])
            ).first()
        my_vote = row.vote if row else None
    return {"obligation_id": obligation_id, **tally, "my_vote": my_vote}


# ------------------------------------------------------------ contributors


@router.get("/wall")
def contributors_wall() -> dict:
    engine = get_engine()
    with engine.connect() as conn:
        accepted = conn.execute(
            sa.select(submissions.c.submitter_id, sa.func.count().label("n"))
            .where(submissions.c.status.in_(("accepted", "shipped")))
            .group_by(submissions.c.submitter_id)
            .order_by(sa.desc("n"))
            .limit(50)
        ).all()
        voters = conn.execute(
            sa.select(votes.c.user_id, sa.func.count().label("n")).group_by(votes.c.user_id).order_by(sa.desc("n")).limit(50)
        ).all()
        totals = {
            "cases_total": conn.execute(sa.select(sa.func.count()).select_from(submissions)).scalar_one(),
            "cases_accepted": conn.execute(
                sa.select(sa.func.count()).select_from(submissions).where(submissions.c.status.in_(("accepted", "shipped")))
            ).scalar_one(),
            "validation_votes": conn.execute(sa.select(sa.func.count()).select_from(votes)).scalar_one(),
            "contributors": conn.execute(sa.select(sa.func.count()).select_from(users)).scalar_one(),
        }
    names = _display_names(engine, {r.submitter_id for r in accepted} | {r.user_id for r in voters})
    return {
        "totals": totals,
        "top_contributors": [{"name": names.get(r.submitter_id, "contributor"), "accepted_cases": r.n} for r in accepted],
        "top_validators": [{"name": names.get(r.user_id, "contributor"), "votes": r.n} for r in voters],
    }
