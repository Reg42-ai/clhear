# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Identity tables: account profiles, request usage and rate windows.

They live in their own ``identity`` schema so the web tier's database role
can be granted exactly these tables (plus ``community.users`` and
``community.api_keys``) and nothing that holds corpus text or derived layers.
"""
from __future__ import annotations

import sqlalchemy as sa

from app.clhear.community_models import BigId, Json

IDENTITY_SCHEMA = "identity"

metadata = sa.MetaData()

ACCOUNT_STATUSES = ("active", "suspended")

account_profiles = sa.Table(
    "account_profiles",
    metadata,
    sa.Column("user_id", sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column("email", sa.Text, nullable=False, index=True),
    sa.Column("name", sa.Text, nullable=False, default=""),
    sa.Column("organization", sa.Text, nullable=False, default=""),
    sa.Column("intended_use", sa.Text, nullable=False, default=""),
    sa.Column("terms_version", sa.Text, nullable=False, default=""),
    sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("provider", sa.Text, nullable=False, default="email"),
    sa.Column("email_verified", sa.Boolean, nullable=False, default=False),
    sa.Column("status", sa.Text, sa.CheckConstraint("status in ('active','suspended')", name="account_status_check"),
              nullable=False, default="active"),
    sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("suspended_reason", sa.Text, nullable=False, default=""),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    schema=IDENTITY_SCHEMA,
)

# One row per request. The route is the template ("/v1/releases/{release_id}"),
# never the raw URL, and the client address is stored only as a keyed hash.
api_usage = sa.Table(
    "api_usage",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("user_id", sa.Text, nullable=False, default="", index=True),
    sa.Column("key_id", sa.Text, nullable=False, default=""),
    sa.Column("app_id", sa.Text, nullable=False, default=""),
    sa.Column("method", sa.Text, nullable=False, default="GET"),
    sa.Column("route", sa.Text, nullable=False, default=""),
    sa.Column("layer", sa.Text, nullable=False, default=""),
    sa.Column("status", sa.Integer, nullable=False),
    sa.Column("latency_ms", sa.Integer, nullable=False, default=0),
    sa.Column("response_bytes", sa.BigInteger, nullable=False, default=0),
    sa.Column("ip_hash", sa.Text, nullable=False, default=""),
    sa.Column("user_agent", sa.Text, nullable=False, default=""),
    schema=IDENTITY_SCHEMA,
)

api_usage_daily = sa.Table(
    "api_usage_daily",
    metadata,
    sa.Column("day", sa.Date, primary_key=True),
    sa.Column("user_id", sa.Text, primary_key=True, default=""),
    sa.Column("key_id", sa.Text, primary_key=True, default=""),
    sa.Column("app_id", sa.Text, primary_key=True, default=""),
    sa.Column("layer", sa.Text, primary_key=True, default=""),
    sa.Column("route", sa.Text, primary_key=True, default=""),
    sa.Column("requests", sa.Integer, nullable=False, default=0),
    sa.Column("errors", sa.Integer, nullable=False, default=0),
    sa.Column("response_bytes", sa.BigInteger, nullable=False, default=0),
    sa.Column("latency_ms_total", sa.BigInteger, nullable=False, default=0),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    schema=IDENTITY_SCHEMA,
)

# Fixed-window counters shared by every web task (bucket = "key:<id>", "ip:<hash>", ...).
rate_windows = sa.Table(
    "rate_windows",
    metadata,
    sa.Column("bucket", sa.Text, primary_key=True),
    sa.Column("window_start", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("count", sa.Integer, nullable=False, default=0),
    schema=IDENTITY_SCHEMA,
)

maintainer_actions = sa.Table(
    "maintainer_actions",
    metadata,
    sa.Column("id", BigId, primary_key=True, autoincrement=True),
    sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),  # suspend | unsuspend | revoke_key
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("detail", Json, nullable=False, default=dict),
    schema=IDENTITY_SCHEMA,
)

TABLES = (account_profiles, api_usage, api_usage_daily, rate_windows, maintainer_actions)
