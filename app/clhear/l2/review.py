# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 reviewers (HLD v2 §4.2): second-model check + confidence, and the
expert-panel verdicts that feed the precision gate.

Weekly second-model pass: for every live obligation whose current basis hash
has no review, a *different* model (task ``l2.review`` on the judge ladder)
reads the clause and the determination and returns correct / incorrect /
unsure with a confidence. Verdicts are appended to ``obligation_reviews``;
``obligations.review_confidence`` holds the latest. Incorrect or
low-confidence (< LOW_CONFIDENCE_THRESHOLDS["L2"]) verdicts open a proposal
for the approval console (I4: HITL by exception).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import asserts, obligation_reviews, obligations
from app.clhear.l1.models import clauses
from app.clhear.platform import record
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l2.review")

MAX_PER_RUN = 200
VERDICTS = ("correct", "incorrect", "unsure")


def _unreviewed(conn, limit: int) -> list[dict]:
    from app.clhear.l1.scopes import in_scope

    reviewed = {
        (r.obligation_id, r.text_hash)
        for r in conn.execute(sa.select(obligation_reviews.c.obligation_id, obligation_reviews.c.text_hash))
    }
    out = []
    for ob in conn.execute(
        sa.select(obligations).where(obligations.c.status.in_(("derived", "validated"))).order_by(obligations.c.id)
    ).mappings():
        if not in_scope(ob["source_key"]) or (ob["id"], ob["text_hash"]) in reviewed:
            continue
        clause = conn.execute(
            sa.select(clauses.c.id, clauses.c.text)
            .join(asserts, asserts.c.clause_id == clauses.c.id)
            .where(asserts.c.obligation_id == ob["id"])
            .where(asserts.c.valid_to.is_(None))
            .order_by(asserts.c.id)
            .limit(1)
        ).first()
        out.append({**dict(ob), "clause_text": clause.text if clause else ob["statement"]})
        if len(out) >= limit:
            break
    return out


def _store(conn, ob: dict, *, reviewer_kind: str, reviewer: str, verdict: str, confidence: float | None, notes: str) -> None:
    conn.execute(
        obligation_reviews.insert().values(
            obligation_id=ob["id"], reviewer_kind=reviewer_kind, reviewer=reviewer, verdict=verdict,
            confidence=confidence, text_hash=ob["text_hash"], notes=notes[:1000],
            derived_by=f"l2.review.{reviewer_kind}", reviewed_at=datetime.now(timezone.utc),
        )
    )
    conn.execute(
        obligations.update().where(obligations.c.id == ob["id"]).values(review_confidence=confidence)
    )
    if verdict == "incorrect" or record.needs_human("L2", confidence):
        from app.clhear.platform.proposals import create_proposal

        create_proposal(
            conn, layer="L2", kind="l2_review", subject_ref=ob["stable_id"] or ob["id"],
            draft={"obligation_id": ob["stable_id"] or ob["id"], "derivation_key": ob["id"], "verdict": verdict,
                   "confidence": confidence, "reviewer": reviewer, "notes": notes[:500],
                   "determination": ob["determination"] or ob["statement"]},
            rationale=f"{reviewer_kind} review {verdict} (confidence {confidence}) — below L2 threshold or incorrect",
            confidence=confidence,
        )


def review_obligations(engine: Engine, llm, limit: int = MAX_PER_RUN) -> dict:
    """Second-model pass over unreviewed live obligations."""
    reviewed = escalated = failed = 0
    with engine.connect() as conn:
        batch = _unreviewed(conn, limit)
    for ob in batch:
        prompt = (
            "You are an independent reviewer. Given a regulatory clause and an obligation a system derived from it, "
            "judge whether the obligation is a CORRECT reading of the clause: same addressee, same duty, no invented "
            "conditions or scope. Reply JSON only: "
            '{"verdict": "correct"|"incorrect"|"unsure", "confidence": 0.0-1.0, "reason": "one sentence"}.\n\n'
            f"CLAUSE ({ob['source_key']} {ob['clause_ref']}):\n{(ob['clause_text'] or '')[:3000]}\n\n"
            f"OBLIGATION ({ob['stable_id'] or ob['id']}):\n{ob['determination'] or ob['statement']}\n"
            f"subject: {ob['subject']}\naction: {ob['action']}\ncondition: {ob['condition']}\n"
        )
        try:
            result = complete(
                llm, "l2.review", prompt=prompt,
                system="You judge derivations against their source text. JSON only.",
                required_keys=["verdict", "confidence"], max_tokens=300,
            )
            parsed = parse_json_object(result.text)
            verdict = str(parsed.get("verdict", "unsure")).lower()
            if verdict not in VERDICTS:
                verdict = "unsure"
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
            confidence = max(0.0, min(1.0, confidence))
            notes = str(parsed.get("reason", ""))
            model = result.model
        except Exception as exc:
            log.exception("l2 review failed for %s", ob["id"])
            failed += 1
            continue
        with engine.begin() as conn:
            _store(conn, ob, reviewer_kind="model", reviewer=model, verdict=verdict, confidence=confidence, notes=notes)
        reviewed += 1
        if verdict == "incorrect" or record.needs_human("L2", confidence):
            escalated += 1
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L2", fleet="l2.review",
            reasoning=f"Second-model review: {reviewed} obligations judged, {escalated} escalated to the console",
            detail={"reviewed": reviewed, "escalated": escalated, "failed": failed},
        )
    except Exception:
        log.exception("review ai_ops failed")
    return {"reviewed": reviewed, "escalated": escalated, "failed": failed}


def record_expert_review(engine: Engine, obligation_ref: str, *, reviewer: str, verdict: str, notes: str = "") -> dict:
    """Expert-panel verdict (quarterly panel / Eval Studio). Counts as
    confidence 1.0 for correct, 0.0 for incorrect, None for unsure."""
    from app.clhear.l2.registry import resolve_obligation_id

    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    with engine.begin() as conn:
        oid = resolve_obligation_id(conn, obligation_ref)
        if oid is None:
            raise KeyError(obligation_ref)
        ob = conn.execute(sa.select(obligations).where(obligations.c.id == oid)).mappings().one()
        confidence = {"correct": 1.0, "incorrect": 0.0, "unsure": None}[verdict]
        _store(conn, dict(ob), reviewer_kind="expert", reviewer=reviewer, verdict=verdict, confidence=confidence, notes=notes)
    return {"obligation_id": ob["stable_id"] or oid, "verdict": verdict, "reviewer": reviewer}


def precision(engine: Engine, *, current_only: bool = True) -> dict:
    """Share of reviewed obligations judged correct (latest verdict per
    reviewer kind and obligation; expert verdicts override model verdicts)."""
    with engine.connect() as conn:
        hashes = {r.id: r.text_hash for r in conn.execute(sa.select(obligations.c.id, obligations.c.text_hash)
                                                          .where(obligations.c.status.in_(("derived", "validated"))))}
        rows = conn.execute(sa.select(obligation_reviews).order_by(obligation_reviews.c.id)).mappings().all()
    latest: dict[str, dict] = {}
    for r in rows:
        if r["obligation_id"] not in hashes:
            continue
        if current_only and r["text_hash"] != hashes[r["obligation_id"]]:
            continue
        prev = latest.get(r["obligation_id"])
        if prev is None or r["reviewer_kind"] == "expert" or prev["reviewer_kind"] != "expert":
            latest[r["obligation_id"]] = dict(r)
    judged = [v for v in latest.values() if v["verdict"] != "unsure"]
    correct = sum(1 for v in judged if v["verdict"] == "correct")
    return {
        "reviewed": len(latest),
        "judged": len(judged),
        "correct": correct,
        "incorrect": len(judged) - correct,
        "unsure": len(latest) - len(judged),
        "precision": round(correct / len(judged), 4) if judged else None,
        "by_kind": {
            k: sum(1 for v in latest.values() if v["reviewer_kind"] == k) for k in ("model", "expert")
        },
    }


__all__ = ["precision", "record_expert_review", "review_obligations"]
