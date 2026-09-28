# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Derived-layer tables (L2 obligations, L3/L5 curated catalog, L6 blueprints).

Layer schemas per HLD §2 / m0003 reservations. L2 rows are MACHINE-DERIVED
from L1 clauses (deterministic extractor, app/clhear/l2/extract.py) and carry
status `derived` until a maintainer promotes them to `validated`. L3/L5/L4
rows are CURATED policy content seeded from reviewed JSON and editable only
through the proposals queue. L6 blueprints are computed per request and
logged for replayability.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

L2_SCHEMA = "l2_obligations"
L3_SCHEMA = "l3_building_blocks"
L4_SCHEMA = "l4_profiles"
L5_SCHEMA = "l5_activities"
L6_SCHEMA = "l6_composer"

metadata = sa.MetaData()

Json = sa.JSON().with_variant(JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(sa.Integer, "sqlite")

# ------------------------------------------------------------------------ L2

obligations = sa.Table(
    "obligations",
    metadata,
    # Deterministic id: "OBL:{source_key}#{clause_ref}" — same inputs, same id.
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("clause_ref", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("statement", sa.Text, nullable=False, default=""),  # empty for restricted sources
    sa.Column("addressee", sa.Text, nullable=False, default=""),
    sa.Column("modality", sa.Text, nullable=False, default=""),  # must | must-not | shall | ...
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column("themes", Json, nullable=False, default=list),
    sa.Column("confidence", sa.Numeric(4, 3), nullable=False, default=0),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint(
            "status in ('derived','validated','rejected','stale')", name="obligations_status_check"
        ),
        nullable=False,
        default="derived",
    ),
    sa.Column("method", sa.Text, nullable=False, default="deterministic-v1"),
    sa.Column("text_hash", sa.Text, nullable=False),  # basis clause hash at derivation time
    sa.Column("source_version_label", sa.Text, nullable=False, default=""),
    sa.Column("derived_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("validated_by", sa.Text, nullable=True),
    sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
    # HLD v2 §4.2 registry fields. ``stable_id`` is the public ``OBL-000001``
    # identifier (I11, never reused); ``id`` stays the deterministic derivation
    # key so the same corpus always derives the same registry.
    sa.Column("stable_id", sa.Text, nullable=True, unique=True),
    sa.Column("determination", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("subject", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("action", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("condition", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("object", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("regulator", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("obligation_type", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("effective_from", sa.Date, nullable=True),
    sa.Column("effective_to", sa.Date, nullable=True),
    # Canonical obligation this row was deduplicated into (NULL = canonical itself).
    sa.Column("canonical_id", sa.Text, nullable=True),
    sa.Column("review_confidence", sa.Numeric(4, 3), nullable=True),
    schema=L2_SCHEMA,
)

# --------------------------------------------------------- L2 registry edges

OBLIGATION_TYPES = (
    "conduct", "disclosure", "reporting", "record_keeping", "governance",
    "prudential", "prohibition", "authorisation", "consumer_protection", "other",
)
ASSERT_STRENGTHS = ("explicit", "implied")
L2_CHANGE_KINDS = ("added", "updated", "revoked")
BLOCK_KINDS = ("System", "Document", "Role", "Configuration", "Process", "Workflow", "Asset", "Body")

asserts = sa.Table(
    "asserts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # AST-000001
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("clause_id", BigId, nullable=False, index=True),
    sa.Column("source_key", sa.Text, nullable=False, default=""),
    sa.Column("clause_ref", sa.Text, nullable=False, default=""),
    sa.Column("span_start", sa.Integer, nullable=True),  # offsets into clauses.text
    sa.Column("span_end", sa.Integer, nullable=True),
    sa.Column(
        "strength",
        sa.Text,
        sa.CheckConstraint("strength in ('explicit','implied')", name="asserts_strength_check"),
        nullable=False,
        default="explicit",
    ),
    sa.Column("text_hash", sa.Text, nullable=False, default=""),
    schema=L2_SCHEMA,
)

equivalences = sa.Table(
    "equivalences",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # EQV-000001
    sa.Column("obligation_a", sa.Text, nullable=False, index=True),
    sa.Column("obligation_b", sa.Text, nullable=False, index=True),
    sa.Column("basis", sa.Text, nullable=False, default="lexical"),  # lexical | concept | model | human
    sa.Column("concept_id", sa.Text, nullable=True),
    sa.Column("similarity", sa.Numeric(4, 3), nullable=True),
    sa.Column("method", sa.Text, nullable=False, default=""),
    sa.UniqueConstraint("obligation_a", "obligation_b", name="equivalences_pair_unique"),
    schema=L2_SCHEMA,
)

supersessions = sa.Table(
    "supersessions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # SUP-000001
    sa.Column("old_obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("new_obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("cause_change_event_id", sa.Text, nullable=True),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("note", sa.Text, nullable=False, default=""),
    schema=L2_SCHEMA,
)

l2_change_events = sa.Table(
    "l2_change_events",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CHG-000001
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint("kind in ('added','updated','revoked')", name="l2_change_events_kind_check"),
        nullable=False,
    ),
    sa.Column("cause_clause_ids", Json, nullable=False, default=list),
    sa.Column("cause_l1_change_event_id", BigId, nullable=True, index=True),
    sa.Column("source_key", sa.Text, nullable=False, default=""),
    sa.Column("old_text_hash", sa.Text, nullable=False, default=""),
    sa.Column("new_text_hash", sa.Text, nullable=False, default=""),
    sa.Column("effective_date", sa.Date, nullable=True),
    sa.Column("effective_date_basis", sa.Text, nullable=False, default=""),
    sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("detail", Json, nullable=False, default=dict),
    schema=L2_SCHEMA,
)

obligation_reviews = sa.Table(
    "obligation_reviews",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("reviewer_kind", sa.Text, nullable=False, default="model"),  # model | expert
    sa.Column("reviewer", sa.Text, nullable=False, default=""),  # model id or panel member handle
    sa.Column(
        "verdict",
        sa.Text,
        sa.CheckConstraint("verdict in ('correct','incorrect','unsure')", name="obligation_reviews_verdict_check"),
        nullable=False,
    ),
    sa.Column("text_hash", sa.Text, nullable=False, default=""),  # basis hash the verdict was given on
    sa.Column("notes", sa.Text, nullable=False, default=""),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L2_SCHEMA,
)

# --------------------------------------------------------------------- L3/L5

blocks = sa.Table(
    "blocks",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("capability", sa.Text, nullable=False, default=""),
    sa.Column("evidence_artifacts", Json, nullable=False, default=list),
    # Selectors {source_key, refs[]} resolved to derived obligation ids at read time.
    sa.Column("satisfies", Json, nullable=False, default=list),
    sa.Column("implements_controls", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="curated"),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    # HLD v2 §4.3: fixed kind, purpose, harmonisation thread.
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint("kind in ('" + "','".join(BLOCK_KINDS) + "')", name="blocks_kind_check"),
        nullable=False,
        default="Process",
        server_default="Process",
    ),
    sa.Column("purpose", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("canonical_id", sa.Text, nullable=True),  # set on blocks harmonised into a canonical one
    schema=L3_SCHEMA,
)

# requires: obligation -> block, with the span of obligation text that motivates it.
requires = sa.Table(
    "requires",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # REQ-000001
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("block_id", sa.Text, nullable=False, index=True),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("rationale_start", sa.Integer, nullable=True),
    sa.Column("rationale_end", sa.Integer, nullable=True),
    sa.Column("method", sa.Text, nullable=False, default=""),  # curated-anchor | deterministic | llm
    sa.Column("obligation_text_hash", sa.Text, nullable=False, default=""),
    schema=L3_SCHEMA,
)

# characteristics: one row per (block, key) of the kind's fixed schema.
characteristics = sa.Table(
    "characteristics",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("block_id", sa.Text, nullable=False, index=True),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("value", sa.Text, nullable=False, default=""),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('backed','not_specified','unbacked')", name="characteristics_status_check"),
        nullable=False,
        default="not_specified",
    ),
    sa.Column("backing_obligation_id", sa.Text, nullable=True),
    sa.Column("backing_span", sa.Text, nullable=False, default=""),
    sa.Column("method", sa.Text, nullable=False, default=""),
    schema=L3_SCHEMA,
)

# l3_kinds: the schema registry served at /l3/kinds (seeded from l3.kinds).
l3_kinds = sa.Table(
    "l3_kinds",
    metadata,
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("fields", Json, nullable=False, default=list),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L3_SCHEMA,
)

activities = sa.Table(
    "activities",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("business_owner", sa.Text, nullable=False, default=""),
    # [{"anchor": {"source_key": ..., "refs": [...]}, "when": {attr: requirement}}]
    sa.Column("triggers", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="curated"),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    # HLD v2 §4.5: which side of the junction the activity sits on, and its action type
    # (business: onboarding, order_handling, ... / compliance: screen, monitor, report, ...).
    sa.Column(
        "side",
        sa.Text,
        sa.CheckConstraint("side in ('business','compliance')", name="activities_side_check"),
        nullable=False,
        default="compliance",
    ),
    sa.Column("action_type", sa.Text, nullable=False, default=""),
    sa.Column("canonical_id", sa.Text, nullable=True),
    schema=L5_SCHEMA,
)

# implies: an L4 product / service implies a business activity (IMP-000001).
implies = sa.Table(
    "implies",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("product_id", sa.Text, nullable=False, index=True),
    sa.Column("activity_id", sa.Text, nullable=False, index=True),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("method", sa.Text, nullable=False, default=""),  # curated | deterministic | llm
    schema=L5_SCHEMA,
)

# operates: a compliance activity operates an L3 block (OPR-000001), reached
# through the obligations the activity implements.
operates = sa.Table(
    "operates",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("activity_id", sa.Text, nullable=False, index=True),
    sa.Column("block_id", sa.Text, nullable=False, index=True),
    sa.Column("obligation_refs", Json, nullable=False, default=list),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("method", sa.Text, nullable=False, default=""),
    schema=L5_SCHEMA,
)

# mitigates: a compliance activity governs a business activity (MIT-000001);
# the edge is lit by the obligations both sides share.
mitigates = sa.Table(
    "mitigates",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("compliance_activity_id", sa.Text, nullable=False, index=True),
    sa.Column("business_activity_id", sa.Text, nullable=False, index=True),
    sa.Column("obligation_refs", Json, nullable=False, default=list),
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("method", sa.Text, nullable=False, default=""),
    schema=L5_SCHEMA,
)

# ------------------------------------------------------------------------ L4

attribute_schema = sa.Table(
    "attribute_schema",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("type", sa.Text, nullable=False),  # list | bool | text
    sa.Column("description", sa.Text, nullable=False, default=""),
    # Why the attribute exists: the obligation anchors whose scope reads it.
    sa.Column("read_by", Json, nullable=False, default=list),
    schema=L4_SCHEMA,
)

sample_profiles = sa.Table(
    "sample_profiles",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("attributes", Json, nullable=False, default=dict),
    sa.Column("activities", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="sample"),
    schema=L4_SCHEMA,
)

# Grounded license registry: every row quotes a retrieved L1 clause. The model
# never invents a permission type from general knowledge (L4 closed-world RAG).
license_types = sa.Table(
    "license_types",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # LIC:{jurisdiction}:{slug}
    sa.Column("jurisdiction", sa.Text, nullable=False, index=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("issuing_regime", sa.Text, nullable=False, default=""),
    sa.Column("clause_anchors", Json, nullable=False, default=list),  # [{source_key, ref, text_hash}]
    sa.Column("status", sa.Text, nullable=False, default="ai_generated"),
    sa.Column("generated_by", sa.Text, nullable=False, default=""),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L4_SCHEMA,
)

# HLD v2 §4.4 — the profile permutation space. Ontology rows are built from
# regulator registers / permission taxonomies (with register provenance);
# profiles are validated attribute sets; permits / applies_to / validity_rules
# are the edges that make "no impossible permutation" checkable.

licences = sa.Table(
    "licences",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # LIC:<jurisdiction>:<slug>
    sa.Column("jurisdiction", sa.Text, nullable=False, index=True),
    sa.Column("regulator", sa.Text, nullable=False, default=""),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("regime", sa.Text, nullable=False, default=""),  # instrument creating the authorisation
    sa.Column("register", sa.Text, nullable=False, default=""),  # register key (fca_register, esma_registers, sec_registers)
    sa.Column("register_url", sa.Text, nullable=False, default=""),
    sa.Column("register_ref", sa.Text, nullable=False, default=""),  # permission / activity code in the register
    sa.Column("aliases", Json, nullable=False, default=list),
    sa.Column("clause_anchors", Json, nullable=False, default=list),  # [{source_key, ref}]
    sa.Column("status", sa.Text, nullable=False, default="derived"),
    sa.Column("canonical_id", sa.Text, nullable=True),
    schema=L4_SCHEMA,
)

products_services = sa.Table(
    "products_services",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # PRD:<slug>
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("category", sa.Text, nullable=False, default="service"),  # product | service
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("aliases", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="derived"),
    schema=L4_SCHEMA,
)

client_types = sa.Table(
    "client_types",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CLT:<slug>
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("aliases", Json, nullable=False, default=list),
    sa.Column("clause_anchors", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="derived"),
    schema=L4_SCHEMA,
)

channels = sa.Table(
    "channels",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # CHN:<slug>
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, default=""),
    sa.Column("aliases", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="derived"),
    schema=L4_SCHEMA,
)

profiles = sa.Table(
    "profiles",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # PRF-000001
    sa.Column("name", sa.Text, nullable=False, default=""),
    sa.Column("attributes", Json, nullable=False, default=dict),
    sa.Column("fingerprint", sa.Text, nullable=False, index=True),  # sha256 of the normalised attribute set
    sa.Column("validity", Json, nullable=False, default=dict),  # {valid, errors, warnings, checked_at, ontology_version}
    sa.Column("source", sa.Text, nullable=False, default="builder"),  # builder | sample | golden | api
    sa.Column("status", sa.Text, nullable=False, default="valid"),  # valid | invalid
    schema=L4_SCHEMA,
)

permits = sa.Table(
    "permits",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("licence_id", sa.Text, nullable=False, index=True),
    sa.Column("product_id", sa.Text, nullable=False, index=True),
    sa.Column("basis", sa.Text, nullable=False, default=""),
    sa.Column("clause_anchors", Json, nullable=False, default=list),
    schema=L4_SCHEMA,
)

applies_to = sa.Table(
    "applies_to",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # APL-000001
    sa.Column("obligation_id", sa.Text, nullable=False, index=True),
    sa.Column("predicate", Json, nullable=False, default=dict),  # {attribute: requirement} in the when_matches language
    sa.Column("basis", sa.Text, nullable=False, default=""),  # jurisdiction | subject | condition | llm
    sa.Column("rationale", sa.Text, nullable=False, default=""),
    sa.Column("method", sa.Text, nullable=False, default=""),
    sa.Column("obligation_text_hash", sa.Text, nullable=False, default=""),
    schema=L4_SCHEMA,
)

validity_rules = sa.Table(
    "validity_rules",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # VR:<slug>
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("jurisdiction", sa.Text, nullable=False, default="*"),
    # {"if": {attr: requirement}, "requires": {attr: requirement}} or {"if": ..., "forbids": {...}}
    sa.Column("rule", Json, nullable=False, default=dict),
    sa.Column("severity", sa.Text, nullable=False, default="error"),  # error | warning
    sa.Column("basis", sa.Text, nullable=False, default=""),
    sa.Column("clause_anchors", Json, nullable=False, default=list),
    sa.Column("status", sa.Text, nullable=False, default="derived"),
    schema=L4_SCHEMA,
)

# ------------------------------------------------------------------------ L6

blueprints = sa.Table(
    "blueprints",
    metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("requested_by", sa.Text, nullable=False, default=""),
    sa.Column("release", sa.Text, nullable=False, default=""),
    sa.Column("profile", Json, nullable=False, default=dict),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("engine_version", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    # HLD v2 §4.6: public stable id, the L4 profile it was composed for, the
    # fingerprint of (attributes, activities), the full composition and whether
    # a later composition for the same profile superseded it (never deleted).
    sa.Column("stable_id", sa.Text, nullable=True, index=True),  # BLU-000001
    sa.Column("profile_id", sa.Text, nullable=True, index=True),  # PRF-000001 when composed for a stored profile
    sa.Column("fingerprint", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("composition", Json, nullable=True),
    sa.Column("status", sa.Text, nullable=False, default="current", server_default="current"),  # current | superseded
    schema=L6_SCHEMA,
)

# blueprint_items: one block instance in one blueprint — characteristics
# resolved for that profile, the obligations it satisfies there, the
# compliance activities that operate it, and whether it is load-bearing.
blueprint_items = sa.Table(
    "blueprint_items",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # ITM-000001
    sa.Column("blueprint_id", sa.Text, nullable=False, index=True),  # BLU-
    sa.Column("block_id", sa.Text, nullable=False, index=True),
    sa.Column("kind", sa.Text, nullable=False, default=""),
    sa.Column("name", sa.Text, nullable=False, default=""),
    sa.Column(
        "basis",
        sa.Text,
        sa.CheckConstraint("basis in ('required','selected')", name="blueprint_items_basis_check"),
        nullable=False,
        default="selected",
    ),
    sa.Column("characteristics", Json, nullable=False, default=list),
    sa.Column("obligations_satisfied", Json, nullable=False, default=list),
    sa.Column("activities_operated", Json, nullable=False, default=list),
    sa.Column("load_bearing_for", Json, nullable=False, default=list),
    sa.Column("explanation", sa.Text, nullable=False, default=""),
    schema=L6_SCHEMA,
)

# minimality_proofs: per item — the obligations only it satisfies in the
# program, and the removal impact (what becomes a gap without it).
minimality_proofs = sa.Table(
    "minimality_proofs",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("blueprint_id", sa.Text, nullable=False, index=True),
    sa.Column("item_id", sa.Text, nullable=False, index=True),
    sa.Column("block_id", sa.Text, nullable=False, default=""),
    sa.Column("load_bearing_for", Json, nullable=False, default=list),
    sa.Column("removal_impact", Json, nullable=False, default=dict),
    sa.Column("redundant", sa.Boolean, nullable=False, default=False),
    schema=L6_SCHEMA,
)

# --------------------------------------------------- L2 concepts (m0006)
# A concept is ONE representative "CLHEAR obligation" consolidating clause-
# anchored obligations across jurisdictions. It never replaces them: it is a
# resolution overlay, parameterized by the profile's jurisdiction set.

concepts = sa.Table(
    "concepts",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),  # "CON:<slug>"
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("canonical_statement", sa.Text, nullable=False, default=""),
    sa.Column("themes", Json, nullable=False, default=list),
    sa.Column(
        "status",
        sa.Text,
        sa.CheckConstraint("status in ('proposed','curated','flagged')", name="concepts_status_check"),
        nullable=False,
        default="proposed",
    ),
    sa.Column("drafted_by", sa.Text, nullable=False, default="human"),  # human | gateway
    sa.Column("approved_by", sa.Text, nullable=True),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("flag_reason", sa.Text, nullable=False, default=""),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    schema=L2_SCHEMA,
)

concept_members = sa.Table(
    "concept_members",
    metadata,
    sa.Column("concept_id", sa.Text, nullable=False, primary_key=True),
    sa.Column("obligation_id", sa.Text, nullable=False, primary_key=True),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column(
        "role",
        sa.Text,
        sa.CheckConstraint("role in ('primary','supplementary')", name="concept_members_role_check"),
        nullable=False,
        default="primary",
    ),
    sa.Column("note", sa.Text, nullable=False, default=""),
    schema=L2_SCHEMA,
)

DERIVED_TABLES = (
    obligations, blocks, activities, attribute_schema, sample_profiles,
    blueprints, concepts, concept_members, license_types,
    asserts, equivalences, supersessions, l2_change_events, obligation_reviews,
    requires, characteristics,
    licences, products_services, client_types, channels, profiles, permits, applies_to, validity_rules,
    implies, operates, mitigates,
    blueprint_items, minimality_proofs,
)

from app.clhear.platform.shared_schema import attach_shared_columns as _attach  # noqa: E402

_attach(*DERIVED_TABLES)
