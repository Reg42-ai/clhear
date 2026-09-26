# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Service-token authentication for the host API.

Several tokens are accepted so a host can rotate. Comparison is constant time.
Requests are unauthenticated only when the server is bound to a loopback address.
"""
from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Header, HTTPException

from app.clhear.settings import get_settings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def bind_host() -> str:
    return (get_settings().clhear_bind_host or "").strip()


def auth_required() -> bool:
    return bind_host() not in LOOPBACK


def tokens() -> list[str]:
    settings = get_settings()
    found = [part.strip() for part in settings.clhear_service_tokens.split(",") if part.strip()]
    path = settings.clhear_service_token_file.strip()
    if path:
        file = Path(path)
        if file.is_file():
            found.extend(line.strip() for line in file.read_text(encoding="utf-8").splitlines() if line.strip())
    return found


def _equals(presented: str, expected: str) -> bool:
    if len(presented) != len(expected):
        return False
    return hmac.compare_digest(presented, expected)


def require_token(authorization: str | None = Header(default=None)) -> str:
    if not auth_required():
        return ""
    configured = tokens()
    if not configured:
        raise HTTPException(status_code=503, detail="service token is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    presented = authorization.removeprefix("Bearer ").strip()
    if not any(_equals(presented, token) for token in configured):
        raise HTTPException(status_code=401, detail="bearer token rejected")
    return presented
