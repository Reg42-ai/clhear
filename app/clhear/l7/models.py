# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L7 — risk and priority scoring: schema and the published method (HLD v2 §4.7).

Tables (all bi-temporal, shared columns attached — I2, I3):

* ``enforcement_events`` (``ENF-``) — one public enforcement outcome read from an
  L1 enforcement source (FCA final notices, SEC enforcement actions): regulator, date, respondent, amount, kind, the provisions the notice
  cites verbatim, and the L1 clause it was read from.
* ``enforcement_links`` — event → obligation edges (the linker's output) with the
  citation that justified each link, the method and a confidence.
* ``risk_scores`` (``RSK-``) — per obligation or blueprint item: the seven published
  dimensions, the composite, the band, the calibration set the likelihood was
  fitted on and the evidence (event ids, counts, amounts) the score rests on.
* ``stated_penalties`` (``PEN-``) — a penalty a binding text states for a breach:
  its type and the maximum stated, quoted from the clause (``l7.penalties``).
* ``penalty_links`` — penalty → obligation edges: the obligations whose provisions
  the penalty clause refers to, with the words that refer to them.
* ``risk_calibrations`` — append-only: every held-out-year calibration run with
  its Brier score, the base-rate baseline and the reliability table — the
  number the method page publishes.

The method is not a black box: :data:`WEIGHTS` are the published dimension
weights, :data:`METHOD_VERSION` names them, and every score carries them.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.clhear.platform.shared_schema import attach_shared_columns

L7_SCHEMA = "l7_risk"

metadata = sa.MetaData()
Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")

METHOD_VERSION = "risk-v3"

# Published dimension weights (sum to 1). Changing them is a new METHOD_VERSION.
# Enforcement events (history, likelihood, financial and reputational impact) are the
# stronger input; a penalty the texts state is a separate, lighter one.
WEIGHTS: dict[str, float] = {
    "enforcement_history": 0.25,
    "likelihood": 0.20,
    "financial_impact": 0.15,
    "reputational_impact": 0.10,
    "operational_impact": 0.10,
    "regulatory_attention": 0.10,
    "stated_penalty": 0.10,
}
DIMENSIONS: tuple[str, ...] = tuple(WEIGHTS)

DIMENSION_NOTES: dict[str, str] = {
    "enforcement_history": "Recency-weighted count of public enforcement outcomes linked to the obligation "
                           "(half-life 3 years), scaled against the busiest obligation in the corpus.",
    "likelihood": "Calibrated probability that at least one enforcement outcome linked to the obligation is "
                  "published in the next year — a logistic map of history and attention fitted on past years and "
                  "scored on a held-out year (Brier published).",
    "financial_impact": "Log-scaled total and largest penalty across linked outcomes, against the corpus maximum.",
    "reputational_impact": "Share of linked outcomes that named individuals, imposed prohibitions or public censure.",
    "operational_impact": "How much of a program the obligation touches: L3 blocks it requires and L5 activities it "
                          "triggers, scaled against the corpus maximum.",
    "regulatory_attention": "L2 change events on the obligation's source in the last two years plus thematic "
                            "enforcement volume by the same regulator, scaled.",
    "stated_penalty": "The most severe penalty the binding texts in scope state for breaching the obligation "
                      "(imprisonment, then disqualification or revocation, suspension, fine or penalty, with the "
                      "log-scaled monetary maximum), scaled; present even with no enforcement source in scope.",
}

BANDS: tuple[tuple[str, float], ...] = (("critical", 0.75), ("high", 0.55), ("medium", 0.35), ("low", 0.0))

EVENT_KINDS = ("fine", "censure", "prohibition", "restitution", "undertaking", "suspension", "other")
LINK_METHODS = ("citation", "instrument", "llm", "human")
SUBJECT_KINDS = ("obligation", "item")

enforcement_events = sa.Table(
    "enforcement_events",
    metadata,
    # (id, version) is the key: a re-derived notice keeps its ENF- id (I11) and the
    # superseded version stays on the record with valid_to closed (I2).
    sa.Column("id", sa.Text, primary_key=True),  # ENF-000001
    sa.Column("version", sa.Integer, primary_key=True, nullable=False, default=1, server_default="1"),
    sa.Column("regulator", sa.Text, nullable=False, default=""),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column("source_key", sa.Text, nullable=False, index=True),  # the L1 enforcement source
    sa.Column("clause_ref", sa.Text, nullable=False, default=""),  # the notice's ref in that source
    sa.Column("clause_id", BigId, nullable=True),
    sa.Column("notice_ref", sa.Text, nullable=False, default=""),  # regulator's own reference when printed
    sa.Column("title", sa.Text, nullable=False, default=""),
    sa.Column("respondent", sa.Text, nullable=False, default=""),
    sa.Column(
        "respondent_type",
        sa.Text,
        sa.CheckConstraint("respondent_type in ('firm','individual','unknown')", name="enforcement_respondent_check"),
        nullable=False,
        default="unknown",
    ),
    sa.Column("decided_on", sa.Date, nullable=True),
    sa.Column("amount", sa.Numeric(18, 2, asdecimal=False), nullable=True),
    sa.Column("currency", sa.Text, nullable=False, default=""),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint("kind in ('fine','censure','prohibition','restitution','undertaking','suspension','other')",
                           name="enforcement_kind_check"),
        nullable=False,
        default="other",
    ),
    sa.Column("cited_refs", Json, nullable=False, default=list),  # provisions the notice names, verbatim
    sa.Column("summary", sa.Text, nullable=False, default=""),
    sa.Column("url", sa.Text, nullable=False, default=""),
    sa.Column("text_hash", sa.Text, nullable=False, default=""),
    schema=L7_SCHEMA,
)

enforcement_links = sa.Table(
    "enforcement_links",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("event_id", sa.Text, nullable=False, index=True),
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("citation", sa.Text, nullable=False, default=""),  # the text in the notice that justified the link
    sa.Column(
        "method",
        sa.Text,
        sa.CheckConstraint("method in ('citation','instrument','llm','human')", name="enforcement_links_method_check"),
        nullable=False,
        default="citation",
    ),
    sa.Column("event_text_hash", sa.Text, nullable=False, default=""),
    schema=L7_SCHEMA,
)

PENALTY_TYPES = ("imprisonment", "disqualification", "revocation", "suspension", "fine", "penalty")
PENALTY_LINK_METHODS = ("reference", "relative", "whole_text", "division")

stated_penalties = sa.Table(
    "stated_penalties",
    metadata,
    # (id, version) is the key, as for enforcement events: a penalty that reappears after
    # it was closed is a new version of the same PEN- id.
    sa.Column("id", sa.Text, primary_key=True),  # PEN-<hash of source, clause, type and clause text>
    sa.Column("version", sa.Integer, primary_key=True, nullable=False, default=1, server_default="1"),
    sa.Column("source_key", sa.Text, nullable=False, index=True),  # the binding L1 source that states it
    sa.Column("clause_ref", sa.Text, nullable=False, default=""),
    sa.Column("clause_id", BigId, nullable=True),
    sa.Column(
        "penalty_type",
        sa.Text,
        sa.CheckConstraint("penalty_type in (" + ",".join(f"'{t}'" for t in PENALTY_TYPES) + ")",
                           name="stated_penalties_type_check"),
        nullable=False,
    ),
    sa.Column("maximum", sa.Text, nullable=False, default=""),  # the maximum as the clause states it
    sa.Column("amount", sa.Numeric(18, 2, asdecimal=False), nullable=True),
    sa.Column("unit", sa.Text, nullable=False, default=""),  # units, a currency, years, months …
    sa.Column("evidence", Json, nullable=False, default=dict),  # quotes: the penalty words and the maximum
    sa.Column("text_hash", sa.Text, nullable=False, default=""),
    schema=L7_SCHEMA,
)

penalty_links = sa.Table(
    "penalty_links",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("penalty_id", sa.Text, nullable=False, index=True),
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column(
        "method",
        sa.Text,
        sa.CheckConstraint("method in (" + ",".join(f"'{m}'" for m in PENALTY_LINK_METHODS) + ")",
                           name="penalty_links_method_check"),
        nullable=False,
    ),
    sa.Column("via", Json, nullable=True),  # the quote in the penalty clause that refers to the obligation
    schema=L7_SCHEMA,
)

risk_scores = sa.Table(
    "risk_scores",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # RSK-000001
    sa.Column(
        "subject_kind",
        sa.Text,
        sa.CheckConstraint("subject_kind in ('obligation','item')", name="risk_scores_subject_kind_check"),
        nullable=False,
        default="obligation",
    ),
    sa.Column("subject_ref", sa.Text, nullable=False, index=True),  # obligation derivation key or ITM- id
    sa.Column("blueprint_id", sa.Text, nullable=True, index=True),  # for items
    sa.Column("dimensions", Json, nullable=False, default=dict),  # {dimension: 0..1}
    sa.Column("weights", Json, nullable=False, default=dict),  # the published weights the composite used
    sa.Column("method_version", sa.Text, nullable=False, default=METHOD_VERSION),
    sa.Column("composite", sa.Numeric(5, 4, asdecimal=False), nullable=False, default=0.0),
    sa.Column("band", sa.Text, nullable=False, default="low"),
    sa.Column("calibration_set_ref", sa.Text, nullable=False, default=""),
    sa.Column("evidence", Json, nullable=False, default=dict),  # event ids, counts, amounts, change events, blocks
    sa.Column("status", sa.Text, nullable=False, default="current"),  # current | superseded
    schema=L7_SCHEMA,
)

risk_calibrations = sa.Table(
    "risk_calibrations",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CAL:<method>:<held-out year>:<utc stamp>
    sa.Column("method_version", sa.Text, nullable=False, default=METHOD_VERSION),
    sa.Column("held_out_year", sa.Integer, nullable=False),
    sa.Column("training_years", Json, nullable=False, default=list),
    sa.Column("n", sa.Integer, nullable=False, default=0),
    sa.Column("positives", sa.Integer, nullable=False, default=0),
    sa.Column("brier", sa.Numeric(7, 5, asdecimal=False), nullable=True),
    sa.Column("baseline_brier", sa.Numeric(7, 5, asdecimal=False), nullable=True),  # predict the base rate
    sa.Column("parameters", Json, nullable=False, default=dict),  # the fitted logistic (a, b, c)
    sa.Column("reliability", Json, nullable=False, default=list),  # [{bin, n, predicted, observed}]
    sa.Column("published", sa.Boolean, nullable=False, default=True),
    sa.Column("ran_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L7_SCHEMA,
)

L7_TABLES = (enforcement_events, enforcement_links, risk_scores, risk_calibrations, stated_penalties, penalty_links)
attach_shared_columns(enforcement_events, enforcement_links, risk_scores, stated_penalties, penalty_links)


def band_for(composite: float) -> str:
    for name, floor in BANDS:
        if composite >= floor:
            return name
    return "low"


def method() -> dict:
    """The published method: what the score is made of and how (HLD v2 L7 'not a black box')."""
    return {
        "method_version": METHOD_VERSION,
        "weights": dict(WEIGHTS),
        "dimensions": [{"key": k, "weight": WEIGHTS[k], "note": DIMENSION_NOTES[k]} for k in DIMENSIONS],
        "composite": "sum(weight_d * dimension_d) over the seven dimensions; every dimension is scaled 0..1 "
                     "against the corpus so the composite is comparable across jurisdictions",
        "bands": [{"band": b, "from": f} for b, f in BANDS],
        "likelihood": "P(enforcement linked to the obligation in the next 12 months) = sigmoid(a + b*history + "
                      "c*attention), (a, b, c) fitted by grid search on years before the held-out year and scored "
                      "on the held-out year; Brier score and the base-rate baseline are published per run",
        "event_kinds": list(EVENT_KINDS),
        "link_methods": list(LINK_METHODS),
        "openness": {"open": "base priorities, enforcement events and this method",
                     "closed": "instance priorities need the organization's Actual overlay (instance mode)"},
    }


__all__ = [
    "BANDS", "DIMENSIONS", "DIMENSION_NOTES", "EVENT_KINDS", "L7_SCHEMA", "L7_TABLES", "LINK_METHODS",
    "METHOD_VERSION", "PENALTY_LINK_METHODS", "PENALTY_TYPES", "SUBJECT_KINDS", "WEIGHTS", "band_for",
    "enforcement_events", "enforcement_links", "metadata", "method", "penalty_links", "risk_calibrations",
    "risk_scores", "stated_penalties",
]
