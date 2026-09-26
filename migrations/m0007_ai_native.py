# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""0007 — AI-native rails: router quality + decision columns, item lifecycle,
corrections, Eval Studio, AI ops ledger, GPU sessions, L4 license registry,
L7 narratives, L8 cohorts."""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.clhear.derived_models import license_types
from app.clhear.models import (
    ai_ops,
    cohorts,
    corrections,
    eval_tasks,
    eval_votes,
    gpu_sessions,
    item_lifecycle,
    risk_narratives,
    router_quality,
)


def _add_column(conn: Connection, table: str, column: str, ddl: str, schema: str | None = "l0_platform") -> None:
    dialect = conn.engine.dialect.name
    insp = inspect(conn)
    qualified = table if dialect == "sqlite" else (f"{schema}.{table}" if schema else table)
    inspect_table = table
    inspect_schema = None if dialect == "sqlite" else schema
    try:
        cols = {c["name"] for c in insp.get_columns(inspect_table, schema=inspect_schema)}
    except Exception:
        cols = set()
    if column in cols:
        return
    conn.execute(text(f"ALTER TABLE {qualified} ADD COLUMN {ddl}"))


def upgrade(conn: Connection) -> None:
    _add_column(conn, "llm_calls", "task_id", "task_id TEXT")
    _add_column(conn, "llm_calls", "tier", "tier TEXT")
    _add_column(conn, "llm_calls", "rejected_alternatives", "rejected_alternatives JSON")
    _add_column(conn, "llm_calls", "routing_reason", "routing_reason TEXT")
    _add_column(conn, "llm_calls", "quality_at_decision", "quality_at_decision NUMERIC(4, 3)")
    _add_column(conn, "runs", "reasoning", "reasoning TEXT")

    for table in (
        router_quality,
        item_lifecycle,
        corrections,
        eval_tasks,
        eval_votes,
        ai_ops,
        gpu_sessions,
        risk_narratives,
        cohorts,
        license_types,
    ):
        table.create(conn, checkfirst=True)
