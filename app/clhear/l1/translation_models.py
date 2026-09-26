# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Additive English-view records. Original corpus tables remain authoritative."""
import sqlalchemy as sa
from app.clhear.l1.models import metadata, BigId, Json, L1_SCHEMA

language_bindings = sa.Table(
    "l1_language_bindings", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id"), nullable=False, index=True),
    sa.Column("content_hash", sa.Text, nullable=False),
    sa.Column("document_key", sa.Text, nullable=False),
    sa.Column("language", sa.Text, nullable=False),
    sa.Column("authority", sa.Text, nullable=False),
    sa.Column("matches_original_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id")),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("approved", sa.Boolean, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
english_views = sa.Table(
    "l1_english_views", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("source_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id"), nullable=False, index=True),
    sa.Column("source_content_hash", sa.Text, nullable=False),
    sa.Column("language_binding_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.l1_language_bindings.id")),
    sa.Column("document_key", sa.Text, nullable=False, default=""),
    sa.Column("source_language", sa.Text, nullable=False, default=""),
    sa.Column("target_language", sa.Text, nullable=False, default="en"),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("english_version_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.source_versions.id")),
    sa.Column("english_content_hash", sa.Text),
    sa.Column("english_binding_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.l1_language_bindings.id")),
    sa.Column("input_manifest_hash", sa.Text, nullable=False, default=""),
    sa.Column("policy_version", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("job_id", sa.Text),
    sa.Column("permission_binding", Json, nullable=False, default=dict),
    sa.Column("summary", Json, nullable=False, default=dict),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
)
english_segments = sa.Table(
    "l1_english_segments", metadata,
    sa.Column("view_id", sa.Text, sa.ForeignKey(f"{L1_SCHEMA}.l1_english_views.id"), primary_key=True),
    sa.Column("segment_key", sa.Text, primary_key=True),
    sa.Column("ordering", sa.Integer, nullable=False),
    sa.Column("doc_node_id", BigId, sa.ForeignKey(f"{L1_SCHEMA}.doc_nodes.id"), nullable=False),
    sa.Column("field", sa.Text, nullable=False),
    sa.Column("input_hash", sa.Text, nullable=False),
    sa.Column("input_location", Json, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("text_hash", sa.Text, nullable=False),
    sa.Column("translation_provenance", Json, nullable=False),
    sa.Column("evaluation", Json, nullable=False),
)
TABLES = (language_bindings, english_views, english_segments)
