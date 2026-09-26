# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Account-required access: who may reach which route.

* Public: landing, sign-in and sign-up, auth callbacks, terms, disclaimer,
  static files, security.txt, health and status.
* Web app: any verified, signed-in, non-suspended account.
* ``/v1``: a valid configured or self-issued key (``require_app`` decides).
* Review, write and admin routes: the reviewer and maintainer allowlists.

Source-licence rules on verbatim text are separate and unchanged: an account
that is not a reviewer sees only text whose licence permits public display.
"""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import Request

from app.clhear.settings import get_settings

PUBLIC_EXACT = frozenset({"/", "/signin", "/signup", "/terms", "/disclaimer", "/api/clhear/health", "/status",
                          "/status.json", "/.well-known/security.txt", "/security", "/favicon.ico"})
PUBLIC_PREFIXES = ("/auth/", "/static/")
# Account self-service writes; everything else that writes needs a reviewer.
ACCOUNT_WRITES = ("/keys", "/account/")
MAINTAINER_PREFIXES = ("/admin/", "/console", "/api/clhear/admin/")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def accounts_mode() -> bool:
    return get_settings().clhear_access_mode.strip().lower() == "accounts"


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def account_status(user: dict) -> str:
    """'active' unless a maintainer suspended the account (identity store)."""
    from app.clhear import identity
    from app.clhear.identity_models import account_profiles

    try:
        with identity.engine().connect() as conn:
            status = conn.execute(sa.select(account_profiles.c.status).where(
                account_profiles.c.user_id == user.get("id"))).scalar()
    except Exception:  # noqa: BLE001 — an unreachable identity store fails closed below
        return "unknown"
    return status or "active"


def signed_in_account(request: Request) -> dict | None:
    from app.clhear.accounts import current_user

    user = current_user(request)
    if not user:
        return None
    if user.get("provider") in {"cognito", "google"} and user.get("email_verified") is False:
        return None
    return user


def classify(request: Request) -> str:
    """'public' | 'v1' | 'maintainer' | 'reviewer' | 'account' for this request."""
    path, method = request.url.path, request.method.upper()
    if is_public(path):
        return "public"
    if path == "/v1" or path.startswith("/v1/"):
        return "v1"
    if path.startswith(MAINTAINER_PREFIXES):
        return "maintainer"
    if method not in SAFE_METHODS and not path.startswith(ACCOUNT_WRITES):
        return "reviewer"
    return "account"


def is_maintainer(user: dict | None) -> bool:
    return bool(user) and user.get("email", "").lower() in get_settings().maintainer_set
