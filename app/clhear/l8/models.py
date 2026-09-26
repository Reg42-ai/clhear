# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L8 — benchmarks and fills: schema (HLD v2 §4.8).

Tables (bi-temporal, shared columns attached — I2, I3) in schema ``l8_benchmarks``:

* ``fills`` (``FIL-``) — best-practice content for one L3 block slot: suggested
  policy text, procedure steps, a typology / item set with thresholds, a workflow
  definition, a role description. Each fill names its block, the obligations it
  traces to (with their text hashes — the drift basis), its provenance
  (agent | contributor | member_benchmark), maturity (draft | reviewed | endorsed),
  the jurisdictions and profile predicates it applies to, and its rubric score.
* ``fill_reviews`` — expert rubric reviews (five criteria, 0–1) that move a fill
  from draft to reviewed to endorsed (≥ 85 %).
* ``benchmark_inputs`` — opt-in member observations (metric, value, cohort key),
  keyed by an HMAC of the member identity. Never served; only aggregated.
* ``benchmark_aggregates`` (``BMA-``) — k ≥ 5 cohort statistics with Laplace
  (differential-privacy) noise on numeric thresholds. The only member data that
  ever leaves L8, and only to members.
* ``members`` — who may read fills and benchmarks (HLD v2 I9: L8 is closed;
  fill *existence* and maturity are public metadata).

L8 is never blocked on contributions: agents draft fills from the blocks and
their obligations; contributors and member benchmarks refine them.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.clhear.platform.shared_schema import attach_shared_columns

L8_SCHEMA = "l8_benchmarks"

metadata = sa.MetaData()
Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")

FILL_KINDS = ("text", "numeric", "item_set", "workflow")
PROVENANCES = ("agent", "contributor", "member_benchmark")
MATURITIES = ("draft", "reviewed", "endorsed")
REVIEW_DECISIONS = ("endorse", "revise", "reject")
RUBRIC_CRITERIA = ("accuracy", "traceability", "specificity", "actionability", "jurisdiction_fit")
RUBRIC_MIN = 0.85
K_MIN = 5
DEFAULT_EPSILON = 1.0
METHOD_VERSION = "fills-v1"


def _check(col: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{col} in ('" + "','".join(values) + "')", name=name)


fills = sa.Table(
    "fills",
    metadata,
    # (id, version) is the key: a reviewed or re-derived fill keeps its FIL- id and
    # the superseded version stays on the record with valid_to closed (I2).
    sa.Column("id", sa.Text, primary_key=True),  # FIL-000001
    sa.Column("version", sa.Integer, primary_key=True, nullable=False, default=1, server_default="1"),
    sa.Column("block_id", sa.Text, nullable=False, index=True),  # BLK-
    sa.Column("slot", sa.Text, nullable=False, default=""),  # characteristic key or free slot name
    sa.Column("kind", sa.Text, _check("kind", FILL_KINDS, "fills_kind_check"), nullable=False, default="text"),
    sa.Column("title", sa.Text, nullable=False, default=""),
    # text: {text} · numeric: {value, unit, range:[lo,hi], basis} · item_set: {items:[{name, threshold?, note?}]}
    # workflow: {steps:[{order, name, role, output}]}
    sa.Column("content", Json, nullable=False, default=dict),
    sa.Column("provenance", sa.Text, _check("provenance", PROVENANCES, "fills_provenance_check"), nullable=False, default="agent"),
    sa.Column("maturity", sa.Text, _check("maturity", MATURITIES, "fills_maturity_check"), nullable=False, default="draft"),
    sa.Column("jurisdictions", Json, nullable=False, default=list),
    sa.Column("predicates", Json, nullable=False, default=dict),  # profile predicates the fill applies to
    sa.Column("obligation_ids", Json, nullable=False, default=list),  # traceability (I3)
    sa.Column("basis_hash", sa.Text, nullable=False, default=""),  # sha256 over block + obligation text hashes (drift)
    sa.Column("source_refs", Json, nullable=False, default=list),  # L1 guidance / contribution / benchmark refs
    sa.Column("rubric_score", sa.Numeric(4, 3, asdecimal=False), nullable=True),
    sa.Column("contributor", sa.Text, nullable=False, default=""),  # handle (never an email)
    sa.Column("contribution_id", sa.Text, nullable=True),
    sa.Column("model", sa.Text, nullable=False, default=""),  # model that drafted it, or "deterministic"
    sa.Column("drift", Json, nullable=True),  # {detected_at, reason, previous_basis_hash} when re-derived after drift
    sa.Column("status", sa.Text, nullable=False, default="current"),  # current | superseded
    schema=L8_SCHEMA,
)

fill_reviews = sa.Table(
    "fill_reviews",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("fill_id", sa.Text, nullable=False, index=True),
    sa.Column("reviewer", sa.Text, nullable=False),
    sa.Column("rubric", Json, nullable=False, default=dict),  # {criterion: 0..1}
    sa.Column("score", sa.Numeric(4, 3, asdecimal=False), nullable=False, default=0.0),
    sa.Column("decision", sa.Text, _check("decision", REVIEW_DECISIONS, "fill_reviews_decision_check"), nullable=False),
    sa.Column("note", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L8_SCHEMA,
)

benchmark_inputs = sa.Table(
    "benchmark_inputs",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("member_hash", sa.Text, nullable=False, index=True),  # HMAC(member id) — never the identity
    sa.Column("cohort_key", sa.Text, nullable=False, index=True),  # e.g. "UK|payments|retail"
    sa.Column("metric", sa.Text, nullable=False, index=True),  # e.g. "cdd_refresh_days"
    sa.Column("block_id", sa.Text, nullable=True),
    sa.Column("value", sa.Numeric(18, 6, asdecimal=False), nullable=False),
    sa.Column("unit", sa.Text, nullable=False, default=""),
    sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L8_SCHEMA,
)

benchmark_aggregates = sa.Table(
    "benchmark_aggregates",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # BMA-000001
    sa.Column("version", sa.Integer, primary_key=True, nullable=False, default=1, server_default="1"),
    sa.Column("cohort_key", sa.Text, nullable=False, index=True),
    sa.Column("metric", sa.Text, nullable=False, index=True),
    sa.Column("block_id", sa.Text, nullable=True),
    sa.Column("n", sa.Integer, nullable=False, default=0),
    sa.Column("k_threshold", sa.Integer, nullable=False, default=K_MIN),
    sa.Column("epsilon", sa.Numeric(6, 3, asdecimal=False), nullable=False, default=DEFAULT_EPSILON),
    sa.Column("statistics", Json, nullable=False, default=dict),  # {mean, p50, p90, min_bucket, max_bucket} — noised
    sa.Column("noise", Json, nullable=False, default=dict),  # {mechanism: laplace, scale, sensitivity}
    sa.Column("unit", sa.Text, nullable=False, default=""),
    sa.Column("release", sa.Text, nullable=False, default=""),
    sa.Column("status", sa.Text, nullable=False, default="current"),  # current | superseded
    schema=L8_SCHEMA,
)

members = sa.Table(
    "members",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("email", sa.Text, nullable=False, index=True),
    sa.Column("user_id", sa.Text, nullable=False, index=True),
    sa.Column("org_label", sa.Text, nullable=False, default=""),  # display only; never joined to benchmark data
    sa.Column("plan", sa.Text, nullable=False, default="member"),  # member | instance | contributor_seat
    sa.Column("granted_by", sa.Text, nullable=False),
    sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    schema=L8_SCHEMA,
)

L8_TABLES = (fills, fill_reviews, benchmark_inputs, benchmark_aggregates, members)
attach_shared_columns(fills, benchmark_aggregates)
