# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Unified l0_proposals model + approve/reject (HLD §7.1).

One queue across layers. Agents propose; humans ratify: approval requires an
authenticated maintainer identity, recorded with a timestamp. Approving emits
a downstream ProposalApproved outbox event in the same transaction.
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.models import proposals
from app.clhear.platform import events as l0_events


class ProposalNotPending(RuntimeError):
    pass


def create_proposal(
    conn: Connection,
    *,
    layer: str,
    kind: str,
    subject_ref: str,
    draft: dict,
    rationale: str = "",
    confidence: float | None = None,
) -> str:
    proposal_id = str(uuid.uuid4())
    conn.execute(
        proposals.insert().values(
            id=proposal_id,
            layer=layer,
            kind=kind,
            subject_ref=subject_ref,
            draft=draft,
            rationale=rationale,
            confidence=confidence,
            status="proposed",
        )
    )
    return proposal_id


def list_proposals(engine: Engine, status: str | None = None) -> list[dict]:
    query = sa.select(proposals).order_by(proposals.c.created_at.desc())
    if status:
        query = query.where(proposals.c.status == status)
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(query)]


def get_proposal(engine: Engine, proposal_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(proposals).where(proposals.c.id == proposal_id)).first()
    return dict(row._mapping) if row else None


def _decide(engine: Engine, proposal_id: str, decision: str, approver: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(proposals).where(proposals.c.id == proposal_id).with_for_update()
            if engine.dialect.name == "postgresql"
            else sa.select(proposals).where(proposals.c.id == proposal_id)
        ).first()
        if row is None:
            raise KeyError(proposal_id)
        if row.status != "proposed":
            raise ProposalNotPending(f"proposal {proposal_id} is already {row.status}")
        conn.execute(
            proposals.update()
            .where(proposals.c.id == proposal_id)
            .values(status=decision, approver=approver, decided_at=datetime.now(timezone.utc))
        )
        if row.kind == "parse_hint":
            # ARCH: layer hook — ratifying a parse_hint proposal promotes or
            # retires the learned hints it created (l1 hint memory lifecycle).
            from app.clhear.l1.models import parse_hints

            conn.execute(
                parse_hints.update()
                .where(parse_hints.c.proposal_id == proposal_id)
                .values(status="approved" if decision == "approved" else "retired")
            )
        l0_events.emit(
            conn,
            layer=row.layer,
            kind=f"Proposal{decision.capitalize()}",
            subject_ref=row.subject_ref,
            payload={"proposal_id": proposal_id, "kind": row.kind, "approver": approver},
            producer="l0.proposals",
        )
    result = get_proposal(engine, proposal_id)
    assert result is not None
    return result


def approve(engine: Engine, proposal_id: str, approver: str) -> dict:
    return _decide(engine, proposal_id, "approved", approver)


def reject(engine: Engine, proposal_id: str, approver: str) -> dict:
    return _decide(engine, proposal_id, "rejected", approver)
