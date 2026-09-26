# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Eval Studio — sampled human-vs-AI benchmark.

Stratified tasks per layer. Human answers vs AI output → agreement score per
layer and per (task, model), published on /how and fed back into the router
quality table. Disagreements open correction cases.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import obligations
from app.clhear.governance import file_correction, mark_audited
from app.clhear.models import eval_tasks, eval_votes, router_quality
from app.clhear.platform.router import upsert_quality

log = logging.getLogger("clhear.eval_studio")

PROMPTS = {
    "l2.obligation": "Is this a real obligation, correctly anchored to its clause?",
    "l2.concept": "Is this concept membership right (same duty, no invented members)?",
    "l3.block": "Is this block breakdown complete and are satisfies anchors real?",
    "l5.activity": "Does this activity mapping make sense for the obligation it cites?",
    "l7.narrative": "Is this risk narrative sound (numbers match the score)?",
}


def _insert_task(engine: Engine, layer: str, kind: str, subject_ref: str, prompt: str, ai_output: dict, model: str, task_id: str) -> str:
    tid = str(uuid.uuid4())
    with engine.begin() as conn:
        exists = conn.execute(
            sa.select(eval_tasks.c.id)
            .where(eval_tasks.c.subject_ref == subject_ref)
            .where(eval_tasks.c.kind == kind)
            .where(eval_tasks.c.status == "open")
        ).first()
        if exists:
            return str(exists.id)
        conn.execute(
            eval_tasks.insert().values(
                id=tid, layer=layer, kind=kind, subject_ref=subject_ref,
                prompt=prompt, ai_output=ai_output, model=model, task_id=task_id, status="open",
            )
        )
    return tid


def sample_tasks(engine: Engine, per_layer: int = 4) -> dict:
    """Stratified sample of live items into Eval Studio."""
    created = 0
    with engine.connect() as conn:
        obs = conn.execute(
            sa.select(obligations).where(obligations.c.status.in_(("derived", "validated"))).limit(80)
        ).all()
        blks = conn.execute(sa.select(blocks_t).limit(40)).all()
        acts = conn.execute(sa.select(activities_t).limit(40)).all()
    for row in obs[:per_layer]:
        _insert_task(
            engine, "L2", "l2.obligation", row.id, PROMPTS["l2.obligation"],
            {"title": row.title, "statement": row.statement, "method": row.method},
            row.method, "l2.duty_triage" if row.method == "duty-triage-v1" else "l2.extract",
        )
        created += 1
    from app.clhear.l2.concepts import list_concepts

    for c in list_concepts(engine)[:per_layer]:
        _insert_task(
            engine, "L2", "l2.concept", c["id"], PROMPTS["l2.concept"],
            {"name": c["name"], "members": [m["obligation_id"] for m in c.get("members", [])]},
            c.get("drafted_by") or "", "l2.consolidate",
        )
        created += 1
    for row in blks[:per_layer]:
        _insert_task(
            engine, "L3", "l3.block", row.id, PROMPTS["l3.block"],
            {"name": row.name, "satisfies": row.satisfies},
            "", "l3.block_generate",
        )
        created += 1
    for row in acts[:per_layer]:
        _insert_task(
            engine, "L5", "l5.activity", row.id, PROMPTS["l5.activity"],
            {"name": row.name, "triggers": row.triggers},
            "", "l5.activity_map",
        )
        created += 1
    from app.clhear.models import risk_narratives

    with engine.connect() as conn:
        nars = conn.execute(sa.select(risk_narratives).limit(per_layer)).all()
    for row in nars:
        _insert_task(
            engine, "L7", "l7.narrative", row.id, PROMPTS["l7.narrative"],
            {"narrative": row.narrative, "facts_used": row.facts_used},
            row.generated_by, "l7.narrative",
        )
        created += 1
    return {"opened": created}


def list_open(engine: Engine, user_id: str | None = None, limit: int = 20) -> list[dict]:
    voted: set[str] = set()
    if user_id:
        with engine.connect() as conn:
            voted = {
                str(r.task_id)
                for r in conn.execute(sa.select(eval_votes.c.task_id).where(eval_votes.c.user_id == user_id))
            }
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_tasks).where(eval_tasks.c.status == "open").order_by(eval_tasks.c.created_at.desc()).limit(limit * 2)
        ).all()
    out = []
    for row in rows:
        if str(row.id) in voted:
            continue
        out.append({
            "id": str(row.id),
            "layer": row.layer,
            "kind": row.kind,
            "subject_ref": row.subject_ref,
            "prompt": row.prompt,
            "ai_output": row.ai_output if isinstance(row.ai_output, dict) else {},
            "model": row.model,
            "task_id": row.task_id,
        })
        if len(out) >= limit:
            break
    return out


def record_vote(engine: Engine, *, task_id: str, user_id: str, agrees: bool, comment: str = "") -> dict:
    with engine.begin() as conn:
        task = conn.execute(sa.select(eval_tasks).where(eval_tasks.c.id == task_id)).first()
        if task is None:
            raise KeyError(task_id)
        existing = conn.execute(
            sa.select(eval_votes.c.id).where(eval_votes.c.task_id == task_id).where(eval_votes.c.user_id == user_id)
        ).first()
        if existing:
            conn.execute(
                eval_votes.update().where(eval_votes.c.id == existing.id).values(agrees=agrees, comment=comment[:1000])
            )
        else:
            conn.execute(
                eval_votes.insert().values(task_id=task_id, user_id=user_id, agrees=agrees, comment=comment[:1000])
            )
    if agrees:
        try:
            mark_audited(engine, task.layer, task.subject_ref)
        except Exception:
            log.exception("mark_audited failed")
    else:
        file_correction(
            engine, layer=task.layer, subject_ref=task.subject_ref,
            filed_by=user_id, body=comment or "Eval Studio disagreement",
        )
    _refresh_agreement(engine, task.task_id, task.model)
    return {"task_id": task_id, "agrees": agrees}


def _refresh_agreement(engine: Engine, router_task: str, model: str) -> None:
    scores = agreement_scores(engine)
    key = (router_task, model)
    # Per (task, model) from votes on tasks with that pair.
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_tasks, eval_votes.c.agrees)
            .join(eval_votes, eval_votes.c.task_id == eval_tasks.c.id)
            .where(eval_tasks.c.task_id == router_task)
            .where(eval_tasks.c.model == model)
        ).all()
    if not rows:
        return
    n = len(rows)
    agree = sum(1 for r in rows if r.agrees)
    quality = agree / n
    upsert_quality(engine, router_task, model, quality, n, source="eval_studio")
    scores  # published via agreement_scores()


def agreement_scores(engine: Engine) -> dict:
    """AI-vs-human agreement per layer and per (task, model)."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                eval_tasks.c.layer, eval_tasks.c.task_id, eval_tasks.c.model, eval_votes.c.agrees
            ).join(eval_votes, eval_votes.c.task_id == eval_tasks.c.id)
        ).all()
    per_layer: dict[str, list[bool]] = {}
    per_pair: dict[str, list[bool]] = {}
    for r in rows:
        per_layer.setdefault(r.layer, []).append(bool(r.agrees))
        per_pair.setdefault(f"{r.task_id}|{r.model}", []).append(bool(r.agrees))

    def _pack(groups: dict[str, list[bool]]) -> dict:
        out = {}
        for k, vs in groups.items():
            out[k] = {"n": len(vs), "agree": sum(vs), "score": round(sum(vs) / len(vs), 3) if vs else 0.0}
        return out

    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "by_layer": _pack(per_layer),
        "by_task_model": _pack(per_pair),
        "votes": len(rows),
    }
