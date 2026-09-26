# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""AI operations audit feed — every automated decision with a reasoning field."""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import ai_ops, llm_calls, runs


def record(
    engine: Engine,
    *,
    kind: str,
    reasoning: str,
    layer: str = "",
    fleet: str = "",
    detail: dict | None = None,
) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            ai_ops.insert().values(
                kind=kind,
                layer=layer,
                fleet=fleet,
                reasoning=reasoning,
                detail=detail or {},
            )
        )
        return int(result.inserted_primary_key[0])


def list_ops(
    engine: Engine,
    *,
    layer: str | None = None,
    kind: str | None = None,
    fleet: str | None = None,
    limit: int = 80,
) -> list[dict]:
    stmt = sa.select(ai_ops).order_by(ai_ops.c.id.desc()).limit(limit)
    if layer:
        stmt = stmt.where(ai_ops.c.layer == layer)
    if kind:
        stmt = stmt.where(ai_ops.c.kind == kind)
    if fleet:
        stmt = stmt.where(ai_ops.c.fleet == fleet)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [
        {
            "id": row.id,
            "ts": str(row.created_at),
            "kind": row.kind,
            "layer": row.layer,
            "fleet": row.fleet,
            "reasoning": row.reasoning,
            "detail": row.detail if isinstance(row.detail, dict) else {},
        }
        for row in rows
    ]


def activity_items(engine: Engine, *, layer: str | None = None, limit: int = 80) -> list[dict]:
    """Shape AI-ops rows like the existing /activity feed items."""
    items = []
    for op in list_ops(engine, layer=layer, limit=limit):
        items.append(
            {
                "ts": op["ts"],
                "type": "ai_op",
                "actor": op["fleet"] or "router",
                "status": "info",
                "source_key": "",
                "summary": op["reasoning"],
                "kind": op["kind"],
                "layer": op["layer"],
                "reasoning": op["reasoning"],
                "details": op["detail"],
                "links": {"ops": f"#/ops?kind={op['kind']}"},
            }
        )
    return items


def dashboard(engine: Engine) -> dict:
    """Live counts for /how fleet pipeline + AI Operations header."""
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        calls = int(conn.execute(sa.select(sa.func.count()).select_from(llm_calls)).scalar() or 0)
        ops_n = int(conn.execute(sa.select(sa.func.count()).select_from(ai_ops)).scalar() or 0)
        run_n = int(conn.execute(sa.select(sa.func.count()).select_from(runs)).scalar() or 0)
        from app.clhear.platform.gateway import PREMIUM_MODELS

        frontier = float(
            conn.execute(
                sa.select(sa.func.coalesce(sa.func.sum(llm_calls.c.cost_usd), 0)).where(
                    sa.or_(llm_calls.c.model.in_(sorted(PREMIUM_MODELS)), llm_calls.c.tier == "frontier")
                )
            ).scalar()
            or 0
        )
        by_tier = {
            row.tier or "unset": row.n
            for row in conn.execute(
                sa.select(llm_calls.c.tier, sa.func.count().label("n")).group_by(llm_calls.c.tier)
            )
        }
        by_model = {
            row.model: row.n
            for row in conn.execute(
                sa.select(llm_calls.c.model, sa.func.count().label("n")).group_by(llm_calls.c.model)
            )
        }
    return {
        "as_of": now.isoformat(),
        "llm_calls": calls,
        "ai_ops": ops_n,
        "runs": run_n,
        "frontier_spend_usd": round(frontier, 4),
        "premium_spend_usd": round(frontier, 4),
        "calls_by_tier": by_tier,
        "calls_by_task_class": by_tier,
        "calls_by_model": by_model,
        "inference": {"provider": "reg42-infer", "hosting": "aws-bedrock"},
    }
