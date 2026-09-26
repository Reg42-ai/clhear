# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Community schema: contributor accounts, submissions ("cases"), and
validation votes on derived obligations.

Participation is the credibility engine (build plan): anyone can join with
email / Google / Apple, open cases (missing data, corrections, new sources,
output validation, product suggestions), and confirm/dispute derived
obligations. Every submission mirrors into the l0 proposals queue so the
named-human gate stays singular.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

COMMUNITY_SCHEMA = "community"

metadata = sa.MetaData()
Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _uuid() -> str:
    return str(uuid.uuid4())


users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("email", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False, default=""),
    sa.Column("provider", sa.Text, nullable=False, default="email"),  # email | google | apple
    sa.Column("provider_sub", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

SUBMISSION_KINDS = ("missing_data", "correction", "new_source", "output_validation", "product_suggestion")
SUBMISSION_STATUSES = ("new", "triaged", "accepted", "rejected", "shipped")

submissions = sa.Table(
    "submissions",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("kind", sa.Text, sa.CheckConstraint(f"kind in {SUBMISSION_KINDS}", name="submissions_kind_check"), nullable=False),
    sa.Column("target_layer", sa.Text, nullable=False, default=""),
    sa.Column("target_id", sa.Text, nullable=False, default=""),  # obligation id, source key, clause permalink…
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("body", sa.Text, nullable=False, default=""),
    sa.Column("evidence_url", sa.Text, nullable=False, default=""),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint(f"status in {SUBMISSION_STATUSES}", name="submissions_status_check"),
        nullable=False,
        default="new",
    ),
    sa.Column("submitter_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("proposal_id", sa.Uuid(as_uuid=False), nullable=True),  # l0 mirror
    sa.Column("resolution", sa.Text, nullable=False, default=""),
    sa.Column("decided_by", sa.Text, nullable=True),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=COMMUNITY_SCHEMA,
)

votes = sa.Table(
    "votes",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("obligation_id", sa.Text, nullable=False),
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("vote", sa.Text, sa.CheckConstraint("vote in ('confirm','dispute')", name="votes_vote_check"), nullable=False),
    sa.Column("comment", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("obligation_id", "user_id", name="votes_one_per_user"),
    schema=COMMUNITY_SCHEMA,
)

# HLD v2 §5 "Build on it": API keys a signed-in user issues in one click. The
# secret is shown once; only its sha256 is stored. Revocation sets revoked_at
# (I2: never deleted).
api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # KEY-000001
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False, index=True),
    sa.Column("app_id", sa.Text, nullable=False, unique=True),  # the X-App-Id the caller sends
    sa.Column("label", sa.Text, nullable=False, default=""),
    sa.Column("secret_hash", sa.Text, nullable=False),
    sa.Column("prefix", sa.Text, nullable=False, default=""),  # first 8 chars, for display
    sa.Column("scopes", Json, nullable=False, default=list),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

# HLD v2 §5 "Learn": progress on the learning path (tour completed, quiz
# passed, playground used). Anonymous learners carry a client-generated
# learner id; signed-in users carry their user id.
learning_progress = sa.Table(
    "learning_progress",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("learner_id", sa.Text, nullable=False, index=True),
    sa.Column("step_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False, default="tour"),  # tour | quiz | playground
    sa.Column("score", sa.Numeric(4, 3), nullable=True),
    sa.Column("detail", Json, nullable=False, default=dict),
    sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("learner_id", "step_id", name="learning_progress_one_per_step"),
    schema=COMMUNITY_SCHEMA,
)

# HLD v2 §5 "Watch": a watcher follows an L4 profile (their own or a public
# sample) so the digest can say what changed for profiles like theirs.
profile_watches = sa.Table(
    "profile_watches",
    metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True, default=_uuid),
    sa.Column("watcher_id", sa.Text, nullable=False, index=True),
    sa.Column("profile_id", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("watcher_id", "profile_id", name="profile_watches_watcher_profile"),
    schema=COMMUNITY_SCHEMA,
)

# --------------------------------------------------------------------------
# HLD v2 §6 — contributor model (I12: contributions never write directly).
#
# Roles: Reader (anyone, implicit) · Contributor (signed CLA) · Reviewer
# (verified professional; two accept a contribution) · Maintainer (per
# cluster) · Steering. Roles are granted rows with a validity window, never
# deleted (I2). The CLA signature ledger is append-only; a new CLA version means
# a new signature.

ROLES = ("reader", "contributor", "reviewer", "maintainer", "steering")
CLA_VERSION = "1.0"

CONTRIBUTION_KINDS = (
    "correction",  # a field of an obligation / block is wrong
    "missing_source",  # an instrument the corpus lacks
    "equivalence",  # two obligations in different jurisdictions say the same thing
    "characteristic",  # a block characteristic (cadence, approver, retention…)
    "ontology_entry",  # a licence / product / client type / channel for L4
    "fill",  # L8 fill (policy language, procedure, typology set)
    "translation",  # translation of an obligation or block
    "golden_case",  # an evals case for a layer suite
    "evidence_template",  # conformance evidence template
    "enforcement_link",  # an enforcement event → obligation link
)
# submitted → checks_failed | checked → rederived → accepted | rejected → released
CONTRIBUTION_STATUSES = ("submitted", "checks_failed", "checked", "rederived", "accepted", "rejected", "released")
CONTRIBUTION_CHANNELS = ("web", "api", "pr")
REVIEW_DECISIONS = ("accept", "reject", "request_changes")

cla_signatures = sa.Table(
    "cla_signatures",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False, index=True),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("cla_version", sa.Text, nullable=False),
    sa.Column("text_hash", sa.Text, nullable=False),  # sha256 of the CLA text signed
    sa.Column("patent_grant_acknowledged", sa.Boolean, nullable=False, default=True),
    sa.Column("signed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("user_id", "cla_version", name="cla_one_signature_per_version"),
    schema=COMMUNITY_SCHEMA,
)

roles = sa.Table(
    "roles",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False, index=True),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("role", sa.Text, sa.CheckConstraint(f"role in {ROLES}", name="roles_role_check"), nullable=False),
    sa.Column("granted_by", sa.Text, nullable=False),
    sa.Column("note", sa.Text, nullable=False, default=""),
    sa.Column("granted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

contributions = sa.Table(
    "contributions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CON-000001
    sa.Column(
        "kind", sa.Text,
        sa.CheckConstraint(f"kind in {CONTRIBUTION_KINDS}", name="contributions_kind_check"), nullable=False,
    ),
    sa.Column(
        "channel", sa.Text,
        sa.CheckConstraint(f"channel in {CONTRIBUTION_CHANNELS}", name="contributions_channel_check"),
        nullable=False, default="web",
    ),
    sa.Column("layer", sa.Text, nullable=False, default=""),
    sa.Column("target_ref", sa.Text, nullable=False, default="", index=True),  # OBL-…, BLK-…, ENF-…, URL…
    sa.Column("field", sa.Text, nullable=False, default=""),
    sa.Column("proposed", Json, nullable=False, default=dict),
    sa.Column("evidence", Json, nullable=False, default=list),  # [{url, quote}]
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("content_hash", sa.Text, nullable=False, index=True),  # duplicate detection
    sa.Column("contributor_id", sa.Uuid(as_uuid=False), nullable=False, index=True),
    sa.Column("contributor_email", sa.Text, nullable=False),
    sa.Column(
        "status", sa.Text,
        sa.CheckConstraint(f"status in {CONTRIBUTION_STATUSES}", name="contributions_status_check"),
        nullable=False, default="submitted",
    ),
    sa.Column("checks", Json, nullable=False, default=list),  # [{check, ok, detail}]
    sa.Column("rederivation", Json, nullable=True),  # {agreement, reasons, derived, method, at}
    sa.Column("proposal_id", sa.Text, nullable=True),  # l0 mirror (console visibility)
    sa.Column("applied", Json, nullable=True),  # what the record path wrote on acceptance
    sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("released_in", sa.Text, nullable=True),
    sa.Column("impact", Json, nullable=True),  # {blueprints_changed, obligations_changed, …}
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

contribution_reviews = sa.Table(
    "contribution_reviews",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("contribution_id", sa.Text, nullable=False, index=True),
    sa.Column("reviewer_id", sa.Uuid(as_uuid=False), nullable=False),
    sa.Column("reviewer_email", sa.Text, nullable=False),
    sa.Column(
        "decision", sa.Text,
        sa.CheckConstraint(f"decision in {REVIEW_DECISIONS}", name="contribution_reviews_decision_check"),
        nullable=False,
    ),
    sa.Column("note", sa.Text, nullable=False, default=""),
    sa.Column("rederivation_seen", sa.Text, nullable=False, default=""),  # agreement the reviewer saw
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.UniqueConstraint("contribution_id", "reviewer_id", name="contribution_reviews_one_per_reviewer"),
    schema=COMMUNITY_SCHEMA,
)

contributor_notifications = sa.Table(
    "contributor_notifications",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False, index=True),
    sa.Column("contribution_id", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),  # checks_failed | rederived | accepted | rejected | released
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("payload", Json, nullable=False, default=dict),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    schema=COMMUNITY_SCHEMA,
)

CONTRIBUTION_TABLES = (cla_signatures, roles, contributions, contribution_reviews, contributor_notifications)
COMMUNITY_TABLES = (users, submissions, votes, api_keys, learning_progress, profile_watches, *CONTRIBUTION_TABLES)
