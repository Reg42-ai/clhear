# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""HMAC webhooks. Delivery failures are recorded and do not fail the run."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Callable

from sqlalchemy.engine import Engine

from app.clhear import hoststore

EVENTS = (
    "run.started",
    "run.finished",
    "run.failed",
    "source.failed",
    "release.published",
    "blueprint.changed",
)


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(url: str, body: bytes, headers: dict) -> None:
    import httpx

    httpx.post(url, content=body, headers=headers, timeout=5.0).raise_for_status()


def emit(engine: Engine, event: str, payload: dict, *, sender: Callable[[str, bytes, dict], None] | None = None) -> dict:
    if event not in EVENTS:
        raise ValueError(f"unknown event {event}")
    body = {"event_id": "evt_" + uuid.uuid4().hex, "event": event, **payload}
    raw = json.dumps(body, default=str).encode("utf-8")
    send = sender or _post
    delivered = 0
    for hook in hoststore.webhook_secrets(engine):
        headers = {
            "Content-Type": "application/json",
            "X-CLHEAR-Event": event,
            "X-CLHEAR-Signature": sign(hook["secret"], raw),
        }
        try:
            send(hook["url"], raw, headers)
            delivered += 1
        except Exception:
            continue
    return {"event_id": body["event_id"], "delivered": delivered, "body": body}
