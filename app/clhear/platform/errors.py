# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Error tracking to self-hosted GlitchTip (HLD v2 §3 "Observability"; item 17).

GlitchTip speaks the Sentry protocol, so the standard SDK is used; ``SENTRY_DSN``
points at the GlitchTip service in ``infra/observability.tf``. Without a DSN this
module is inert. Events are scrubbed before they leave the process: no request
bodies, no cookies, no user emails — CLHEAR stores no adopter PII and the error
tracker must not become the place where it leaks (agnostic store, I5).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("clhear.errors")

_SCRUB_KEYS = {"cookie", "cookies", "authorization", "x-reg42-user", "x-api-key", "email", "session"}


def _scrub(event: dict, _hint) -> dict:
    req = event.get("request") or {}
    req.pop("data", None)
    req.pop("cookies", None)
    headers = req.get("headers") or {}
    for k in list(headers):
        if k.lower() in _SCRUB_KEYS:
            headers[k] = "[scrubbed]"
    user = event.get("user") or {}
    for k in ("email", "username", "ip_address"):
        user.pop(k, None)
    return event


def init(*, component: str) -> bool:
    """Wire the SDK when SENTRY_DSN is set. Returns True when tracking is on."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn or dsn == "CHANGEME":  # SSM placeholder until the GlitchTip project exists
        return False
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - optional at runtime
        log.warning("SENTRY_DSN set but sentry-sdk is not installed")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("CLHEAR_ENV", "prod"),
        release=os.environ.get("CLHEAR_RELEASE") or None,
        send_default_pii=False,
        before_send=_scrub,
        traces_sample_rate=0.0,  # errors only; metrics are Prometheus' job
        max_request_body_size="never",
    )
    sentry_sdk.set_tag("component", component)
    return True
