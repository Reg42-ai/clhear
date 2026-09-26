# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""l0_platform schema (HLD §6.1) as SQLAlchemy Core metadata.

The metadata carries the `l0_platform` schema name; on SQLite (dev/tests) the
engine translates the schema away so the same statements run on both backends.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

L0_SCHEMA = "l0_platform"

metadata = sa.MetaData(schema=L0_SCHEMA)

Json = sa.JSON().with_variant(JSONB(), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


schema_migrations = sa.Table(
    "schema_migrations",
    metadata,
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Transactional OUTBOX; relay ships unrelayed rows to SQS and stamps relayed_at.
events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("event_id", sa.Uuid(as_uuid=False), nullable=False, unique=True, default=_uuid),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("payload", Json, nullable=False, default=dict),
    sa.Column("schema_version", sa.Integer, nullable=False, default=1),
    sa.Column("producer", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("relayed_at", sa.DateTime(timezone=True), nullable=True),
    # sent | audit_only | quarantined — how the relay disposed of the row (m0035)
    sa.Column("relay_disposition", sa.Text, nullable=True),
)

# ONE proposals table for all layers (HLD §7.1).
proposals = sa.Table(
    "proposals",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("draft", Json, nullable=False, default=dict),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('proposed','approved','rejected')", name="proposals_status_check"),
        nullable=False,
        default="proposed",
    ),
    sa.Column("approver", sa.Text, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

llm_calls = sa.Table(
    "llm_calls",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("fleet", sa.Text, nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("prompt_hash", sa.Text, nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False),
    sa.Column("output_tokens", sa.Integer, nullable=False),
    sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    # Inference-router audit (m0007): why this model, what was rejected.
    sa.Column("task_id", sa.Text, nullable=True),
    sa.Column("tier", sa.Text, nullable=True),
    sa.Column("rejected_alternatives", Json, nullable=True),
    sa.Column("routing_reason", sa.Text, nullable=True),
    sa.Column("quality_at_decision", sa.Numeric(4, 3), nullable=True),
)

eval_runs = sa.Table(
    "eval_runs",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("suite", sa.Text, nullable=False),
    sa.Column("source_key", sa.Text, nullable=True),
    sa.Column("release", sa.Text, nullable=True),
    sa.Column("scores", Json, nullable=False, default=dict),
    sa.Column("passed", sa.Boolean, nullable=False),
    sa.Column("ran_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Run ledger: every fleet run recorded (HLD principle 7 — replayability).
runs = sa.Table(
    "runs",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("fleet", sa.Text, nullable=False),
    sa.Column("trigger", sa.Text, nullable=False),
    sa.Column("inputs", Json, nullable=False, default=dict),
    sa.Column("outputs", Json, nullable=False, default=dict),
    sa.Column("duration_ms", sa.Integer, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("reasoning", sa.Text, nullable=True),
)

# Per-(task, model) quality — seeded from published benches, updated by Eval Studio.
router_quality = sa.Table(
    "router_quality",
    metadata,
    sa.Column("task_id", sa.Text, primary_key=True),
    sa.Column("model", sa.Text, primary_key=True),
    sa.Column("quality", sa.Numeric(4, 3), nullable=False),
    sa.Column("n_samples", sa.Integer, nullable=False, default=0),
    sa.Column("source", sa.Text, nullable=False, default="seed"),  # seed | eval_studio
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# Unified AI-native item lifecycle (correction loop lives here, not on per-layer checks).
item_lifecycle = sa.Table(
    "item_lifecycle",
    metadata,
    sa.Column("layer", sa.Text, primary_key=True),
    sa.Column("subject_ref", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False, default="ai_generated"),
    sa.Column("generated_by", sa.Text, nullable=False, default=""),
    sa.Column("routing_reason", sa.Text, nullable=False, default=""),
    sa.Column("audit_sampled", sa.Boolean, nullable=False, default=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("detail", Json, nullable=False, default=dict),
)

corrections = sa.Table(
    "corrections",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("filed_by", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False, default=""),
    sa.Column("status", sa.Text, nullable=False, default="correction_pending"),
    sa.Column("ai_verdict", sa.Text, nullable=True),  # accept | reject
    sa.Column("ai_rationale", sa.Text, nullable=False, default=""),
    sa.Column("second_reviewer", sa.Text, nullable=True),
    sa.Column("admin_actor", sa.Text, nullable=True),
    sa.Column("submission_id", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

eval_tasks = sa.Table(
    "eval_tasks",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False),
    sa.Column("prompt", sa.Text, nullable=False, default=""),
    sa.Column("ai_output", Json, nullable=False, default=dict),
    sa.Column("model", sa.Text, nullable=False, default=""),
    sa.Column("task_id", sa.Text, nullable=False, default=""),
    sa.Column("status", sa.Text, nullable=False, default="open"),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

eval_votes = sa.Table(
    "eval_votes",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("task_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("user_id", sa.Text, nullable=False),
    sa.Column("agrees", sa.Boolean, nullable=False),
    sa.Column("comment", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("task_id", "user_id", name="eval_votes_one_per_user"),
)

ai_ops = sa.Table(
    "ai_ops",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("layer", sa.Text, nullable=False, default=""),
    sa.Column("fleet", sa.Text, nullable=False, default=""),
    sa.Column("reasoning", sa.Text, nullable=False, default=""),
    sa.Column("detail", Json, nullable=False, default=dict),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

gpu_sessions = sa.Table(
    "gpu_sessions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("instance_id", sa.Text, nullable=False, default=""),
    sa.Column("instance_type", sa.Text, nullable=False, default="g6.xlarge"),
    sa.Column("status", sa.Text, nullable=False, default="launching"),
    sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("est_cost_usd", sa.Numeric(10, 4), nullable=True),
    sa.Column("detail", Json, nullable=False, default=dict),
)

risk_narratives = sa.Table(
    "risk_narratives",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("score_id", sa.Text, nullable=False),
    sa.Column("narrative", sa.Text, nullable=False, default=""),
    sa.Column("echoed_figures", Json, nullable=False, default=list),
    sa.Column("facts_used", Json, nullable=False, default=list),
    sa.Column("generated_by", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

cohorts = sa.Table(
    "cohorts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("label", sa.Text, nullable=False),
    sa.Column("n", sa.Integer, nullable=False, default=0),
    sa.Column("k_threshold", sa.Integer, nullable=False, default=5),
    sa.Column("synthetic", sa.Boolean, nullable=False, default=False),
    sa.Column("aggregates", Json, nullable=False, default=dict),
    sa.Column("published", sa.Boolean, nullable=False, default=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)

# HLD v2 I7: the query graph and the vector index are projections of the record.
# Every rebuild is logged here (append-only) so the status page can show when
# each projection was last rebuilt, from which release, and with what checksum.
graph_projections = sa.Table(
    "graph_projections",
    metadata,
    sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.Identity(), primary_key=True),
    sa.Column("backend", sa.Text, nullable=False),  # neo4j | local | pgvector | sqlite-vec
    sa.Column("trigger", sa.Text, nullable=False, default="nightly"),  # nightly | lazy | api | event
    sa.Column("release", sa.Text, nullable=False, default=""),
    sa.Column("status", sa.Text, nullable=False, default="succeeded"),  # succeeded | failed
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("duration_ms", sa.Integer, nullable=False, default=0),
    sa.Column("nodes", sa.Integer, nullable=False, default=0),
    sa.Column("edges", sa.Integer, nullable=False, default=0),
    sa.Column("checksum", sa.Text, nullable=False, default=""),
    sa.Column("detail", Json, nullable=False, default=dict),
)

# Human-accepted edits (HLD v2 I4, §8 item 11). Every decision a maintainer takes
# in the approval console that changes or vouches for a determination lands here
# with the basis it was taken on. The next derivation cycle must reproduce the
# edit (same basis → re-asserted) or escalate it (basis changed → new proposal);
# the ledger is append-only, statuses move forward only.
human_edits = sa.Table(
    "human_edits",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # EDT-000001
    sa.Column("layer", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),  # field_edit | validation | verdict
    sa.Column("table_name", sa.Text, nullable=False),
    sa.Column("subject_ref", sa.Text, nullable=False, index=True),  # row key (derivation key / id)
    sa.Column("field", sa.Text, nullable=False, default=""),
    sa.Column("before", Json, nullable=True),
    sa.Column("after", Json, nullable=True),
    sa.Column("basis_hash", sa.Text, nullable=False, default=""),  # text/inputs hash at acceptance
    sa.Column("proposal_id", sa.Text, nullable=True),
    sa.Column("accepted_by", sa.Text, nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    # accepted → reproduced (re-asserted on an unchanged basis) | escalated (basis changed;
    # proposal opened) | superseded (a later edit or decision replaced it)
    sa.Column("status", sa.Text, nullable=False, default="accepted"),
    sa.Column("checks", sa.Integer, nullable=False, default=0),
    sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_outcome", sa.Text, nullable=False, default=""),
    sa.Column("escalation_proposal_id", sa.Text, nullable=True),
)

# L7/L8 product tables carry the shared schema; ledgers above stay append-only records.
from app.clhear.platform.shared_schema import attach_shared_columns as _attach  # noqa: E402

_attach(risk_narratives, cohorts)
