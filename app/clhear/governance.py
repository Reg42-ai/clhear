# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""AI-native item lifecycle: generated → sampled audit / correction loop.

ai_generated --sampled agree--> human_audited
ai_generated --human correction--> correction_pending
correction_pending --AI accepts--> ai_accepted (change applied, human credited)
correction_pending --AI rejects--> ai_rejected
ai_rejected --second human agrees--> second_review
second_review --AI still rejects--> admin_queue (/review)
admin_queue --admin decides--> admin_override
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import corrections, item_lifecycle
from app.clhear.platform.gateway import parse_json_object

log = logging.getLogger("clhear.governance")

AI_GENERATED = "ai_generated"
HUMAN_AUDITED = "human_audited"
CORRECTION_PENDING = "correction_pending"
AI_ACCEPTED = "ai_accepted"
AI_REJECTED = "ai_rejected"
SECOND_REVIEW = "second_review"
ADMIN_QUEUE = "admin_queue"
ADMIN_OVERRIDE = "admin_override"

STATUSES = (
    AI_GENERATED, HUMAN_AUDITED, CORRECTION_PENDING, AI_ACCEPTED,
    AI_REJECTED, SECOND_REVIEW, ADMIN_QUEUE, ADMIN_OVERRIDE,
)

TRANSITIONS = {
    AI_GENERATED: {HUMAN_AUDITED, CORRECTION_PENDING},
    CORRECTION_PENDING: {AI_ACCEPTED, AI_REJECTED},
    AI_REJECTED: {SECOND_REVIEW},
    SECOND_REVIEW: {ADMIN_QUEUE, AI_ACCEPTED, AI_REJECTED},
    ADMIN_QUEUE: {ADMIN_OVERRIDE, AI_ACCEPTED},
    HUMAN_AUDITED: {CORRECTION_PENDING},
    AI_ACCEPTED: {CORRECTION_PENDING},
    ADMIN_OVERRIDE: {CORRECTION_PENDING},
}


class IllegalTransition(RuntimeError):
    pass


def get_lifecycle(engine: Engine, layer: str, subject_ref: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(item_lifecycle)
            .where(item_lifecycle.c.layer == layer)
            .where(item_lifecycle.c.subject_ref == subject_ref)
        ).first()
    if row is None:
        return None
    return {
        "layer": row.layer,
        "subject_ref": row.subject_ref,
        "status": row.status,
        "generated_by": row.generated_by,
        "routing_reason": row.routing_reason,
        "audit_sampled": bool(row.audit_sampled),
        "updated_at": str(row.updated_at),
        "detail": row.detail if isinstance(row.detail, dict) else {},
    }


def mark_generated(
    engine: Engine,
    *,
    layer: str,
    subject_ref: str,
    generated_by: str,
    routing_reason: str = "",
    detail: dict | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        exists = conn.execute(
            sa.select(item_lifecycle.c.subject_ref)
            .where(item_lifecycle.c.layer == layer)
            .where(item_lifecycle.c.subject_ref == subject_ref)
        ).first()
        values = dict(
            status=AI_GENERATED, generated_by=generated_by, routing_reason=routing_reason,
            audit_sampled=False, updated_at=now, detail=detail or {},
        )
        if exists:
            conn.execute(
                item_lifecycle.update()
                .where(item_lifecycle.c.layer == layer)
                .where(item_lifecycle.c.subject_ref == subject_ref)
                .values(**values)
            )
        else:
            conn.execute(
                item_lifecycle.insert().values(layer=layer, subject_ref=subject_ref, **values)
            )
    return get_lifecycle(engine, layer, subject_ref)  # type: ignore[return-value]


def _set_status(engine: Engine, layer: str, subject_ref: str, new_status: str, **extra) -> dict:
    current = get_lifecycle(engine, layer, subject_ref)
    if current is None:
        mark_generated(engine, layer=layer, subject_ref=subject_ref, generated_by=extra.get("generated_by", ""))
        current = get_lifecycle(engine, layer, subject_ref)
        assert current is not None
    allowed = TRANSITIONS.get(current["status"], set())
    if new_status not in allowed:
        raise IllegalTransition(f"{current['status']} -> {new_status} is not allowed")
    values = dict(status=new_status, updated_at=datetime.now(timezone.utc), **extra)
    with engine.begin() as conn:
        conn.execute(
            item_lifecycle.update()
            .where(item_lifecycle.c.layer == layer)
            .where(item_lifecycle.c.subject_ref == subject_ref)
            .values(**values)
        )
    return get_lifecycle(engine, layer, subject_ref)  # type: ignore[return-value]


def mark_audited(engine: Engine, layer: str, subject_ref: str) -> dict:
    return _set_status(engine, layer, subject_ref, HUMAN_AUDITED, audit_sampled=True)


def file_correction(
    engine: Engine,
    *,
    layer: str,
    subject_ref: str,
    filed_by: str,
    body: str,
    submission_id: str | None = None,
) -> dict:
    if get_lifecycle(engine, layer, subject_ref) is None:
        mark_generated(engine, layer=layer, subject_ref=subject_ref, generated_by="unknown")
    life = get_lifecycle(engine, layer, subject_ref)
    assert life is not None
    if life["status"] != CORRECTION_PENDING:
        try:
            _set_status(engine, layer, subject_ref, CORRECTION_PENDING)
        except IllegalTransition:
            # Already in a later state — still record the case.
            pass
    import uuid

    cid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            corrections.insert().values(
                id=cid, layer=layer, subject_ref=subject_ref, filed_by=filed_by,
                body=body[:4000], status=CORRECTION_PENDING, submission_id=submission_id,
            )
        )
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="correction_filed", layer=layer, fleet="governance",
            reasoning=f"Human {filed_by} filed a correction on {subject_ref}",
            detail={"correction_id": cid, "body": body[:240]},
        )
    except Exception:
        log.exception("correction ai_ops failed")
    return {"id": cid, "status": CORRECTION_PENDING, "layer": layer, "subject_ref": subject_ref}


def get_correction(engine: Engine, correction_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(sa.select(corrections).where(corrections.c.id == correction_id)).first()
    if row is None:
        return None
    return dict(row._mapping)


def list_corrections(engine: Engine, status: str | None = None, limit: int = 50) -> list[dict]:
    stmt = sa.select(corrections).order_by(corrections.c.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(corrections.c.status == status)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt)]


def revalidate(engine: Engine, llm, correction_id: str) -> dict:
    """AI referee: accept (apply) or reject with recorded rationale."""
    from app.clhear.platform.router import complete

    case = get_correction(engine, correction_id)
    if case is None:
        raise KeyError(correction_id)
    prompt = (
        "A human filed a correction against a CLHEAR item. Decide whether to ACCEPT "
        "(the human is right; apply the change) or REJECT (the existing item is correct). "
        'Respond JSON: {"verdict": "accept"|"reject", "rationale": <=400 chars}.\n\n'
        f"Layer: {case['layer']}\nItem: {case['subject_ref']}\nCorrection: {case['body']}\n"
    )
    result = complete(
        llm, "l0.revalidate",
        prompt=prompt,
        system="You are CLHEAR's referee. JSON only. Credit the human when they are right.",
        required_keys=["verdict", "rationale"],
        max_tokens=500,
    )
    parsed = parse_json_object(result.text)
    verdict = "accept" if str(parsed.get("verdict", "")).lower().startswith("accept") else "reject"
    rationale = str(parsed.get("rationale", ""))[:500]
    new_status = AI_ACCEPTED if verdict == "accept" else AI_REJECTED
    with engine.begin() as conn:
        conn.execute(
            corrections.update()
            .where(corrections.c.id == correction_id)
            .values(
                status=new_status, ai_verdict=verdict, ai_rationale=rationale,
                updated_at=datetime.now(timezone.utc),
            )
        )
    try:
        _set_status(engine, case["layer"], case["subject_ref"], new_status)
    except IllegalTransition:
        log.warning("lifecycle already at %s for %s", new_status, case["subject_ref"])
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="revalidation", layer=case["layer"], fleet="l0.referee",
            reasoning=(
                f"AI {'accepted' if verdict == 'accept' else 'rejected'} correction {correction_id} "
                f"— {rationale[:180]}"
            ),
            detail={"correction_id": correction_id, "verdict": verdict, "model": result.model},
        )
    except Exception:
        log.exception("revalidation ai_ops failed")
    return {"id": correction_id, "verdict": verdict, "rationale": rationale, "status": new_status, "model": result.model}


def second_human_agree(engine: Engine, correction_id: str, reviewer: str) -> dict:
    case = get_correction(engine, correction_id)
    if case is None:
        raise KeyError(correction_id)
    if case["status"] != AI_REJECTED:
        raise IllegalTransition(f"second review requires ai_rejected, have {case['status']}")
    with engine.begin() as conn:
        conn.execute(
            corrections.update()
            .where(corrections.c.id == correction_id)
            .values(status=SECOND_REVIEW, second_reviewer=reviewer, updated_at=datetime.now(timezone.utc))
        )
    _set_status(engine, case["layer"], case["subject_ref"], SECOND_REVIEW)
    return {"id": correction_id, "status": SECOND_REVIEW, "second_reviewer": reviewer}


def escalate_to_admin(engine: Engine, correction_id: str) -> dict:
    case = get_correction(engine, correction_id)
    if case is None:
        raise KeyError(correction_id)
    with engine.begin() as conn:
        conn.execute(
            corrections.update()
            .where(corrections.c.id == correction_id)
            .values(status=ADMIN_QUEUE, updated_at=datetime.now(timezone.utc))
        )
    _set_status(engine, case["layer"], case["subject_ref"], ADMIN_QUEUE)
    return {"id": correction_id, "status": ADMIN_QUEUE}


def admin_override(engine: Engine, correction_id: str, admin: str, accept: bool = True) -> dict:
    case = get_correction(engine, correction_id)
    if case is None:
        raise KeyError(correction_id)
    status = ADMIN_OVERRIDE if accept else AI_REJECTED
    with engine.begin() as conn:
        conn.execute(
            corrections.update()
            .where(corrections.c.id == correction_id)
            .values(status=status, admin_actor=admin, updated_at=datetime.now(timezone.utc))
        )
    if accept:
        _set_status(engine, case["layer"], case["subject_ref"], ADMIN_OVERRIDE)
    return {"id": correction_id, "status": status, "admin": admin}


def audit_coverage(engine: Engine, layer: str | None = None) -> dict:
    """% of AI-generated items that have been human-sampled."""
    reviewed = sa.or_(item_lifecycle.c.audit_sampled.is_(True),
                      item_lifecycle.c.status.in_((HUMAN_AUDITED, AI_ACCEPTED, ADMIN_OVERRIDE)))
    stmt = sa.select(item_lifecycle.c.status, sa.func.count().label("n"),
                     sa.func.sum(sa.case((reviewed, 1), else_=0)).label("sampled")).group_by(item_lifecycle.c.status)
    if layer:
        stmt = stmt.where(item_lifecycle.c.layer == layer)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    total = sum(int(r.n) for r in rows)
    sampled = sum(int(r.sampled or 0) for r in rows)
    by_status: dict[str, int] = {r.status: int(r.n) for r in rows}
    return {
        "layer": layer,
        "items": total,
        "sampled": sampled,
        "coverage": round(sampled / total, 4) if total else 0.0,
        "by_status": by_status,
    }
