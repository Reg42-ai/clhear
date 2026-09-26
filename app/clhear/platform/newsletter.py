# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Change-digest newsletter hook (HLD v2 §6 community — beehiiv).

The public change feed (``watch.feed``) is rendered into a digest and handed to
beehiiv as a post — created as a *draft* by default so an editor sends it, or
``confirmed`` when ``CLHEAR_BEEHIIV_POST_STATUS=confirmed``. Readers subscribe
through ``subscribe(email)``.

Everything is inert when ``CLHEAR_BEEHIIV_API_KEY`` / ``CLHEAR_BEEHIIV_PUBLICATION_ID``
are empty: ``send_digest`` returns ``{"sent": False, "reason": ...}`` and never
raises, so the nightly stack keeps running without the integration.

Nothing here carries clause text — the digest lists titles, kinds, dates and
links back to the layer pages (I8).
"""
from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Protocol

from sqlalchemy.engine import Engine

from app.clhear.settings import get_settings

log = logging.getLogger("clhear.newsletter")

BEEHIIV_API = "https://api.beehiiv.com/v2"
DIGEST_DAYS = 7
MAX_ENTRIES = 40


class Transport(Protocol):
    def __call__(self, method: str, url: str, *, headers: dict, body: dict) -> dict: ...


def _http_transport(method: str, url: str, *, headers: dict, body: dict) -> dict:
    import httpx

    resp = httpx.request(method, url, headers=headers, json=body, timeout=20.0)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return {"status": resp.status_code, "data": data}


def configured() -> bool:
    s = get_settings()
    return bool(s.clhear_beehiiv_api_key and s.clhear_beehiiv_publication_id)


# --------------------------------------------------------------------------- digest


def _fmt_date(v) -> str:
    return str(v)[:10] if v else "—"


def compose_digest(engine: Engine, since: date | None = None, *, base_url: str | None = None) -> dict:
    """Render the change feed since ``since`` (default: last DIGEST_DAYS) as {subject, html, text, entries}."""
    from app.clhear import watch

    since = since or (datetime.now(timezone.utc).date() - timedelta(days=DIGEST_DAYS))
    base = (base_url or get_settings().clhear_public_base_url).rstrip("/")
    doc = watch.feed(engine, since=since, limit=MAX_ENTRIES)
    entries = doc["entries"]
    counts = doc.get("counts") or {}
    subject = f"CLHEAR changes since {since.isoformat()}: " + ", ".join(
        f"{n} {layer}" for layer, n in sorted(counts.items()) if n) if entries else f"CLHEAR: no changes since {since.isoformat()}"

    text_lines = [f"CLHEAR change digest — {len(entries)} changes since {since.isoformat()}", ""]
    html_parts = [f"<h1>CLHEAR change digest</h1><p>{len(entries)} changes since {since.isoformat()}.</p>"]
    for layer in ("L1", "L2", "L6"):
        rows = [e for e in entries if e.get("layer") == layer]
        if not rows:
            continue
        text_lines.append(f"## {layer} ({len(rows)})")
        html_parts.append(f"<h2>{layer} <small>({len(rows)})</small></h2><ul>")
        for e in rows:
            when = _fmt_date(e.get("effective_date") or e.get("detected_at"))
            link = f"{base}{e.get('href', '')}"
            text_lines.append(f"- {when} · {e.get('kind', '')} · {e.get('title', '')} · {link}")
            html_parts.append(
                f"<li><time>{html.escape(when)}</time> · <code>{html.escape(str(e.get('kind', '')))}</code> · "
                f"<a href=\"{html.escape(link, quote=True)}\">{html.escape(str(e.get('title', '')))}</a>"
                + (f" <em>({html.escape(str(e.get('jurisdiction')))})</em>" if e.get("jurisdiction") else "") + "</li>")
        html_parts.append("</ul>")
        text_lines.append("")
    footer = f"Full feed: {base}/watch · Atom: {base}/watch/feed.atom · Contribute: {base}/contribute"
    text_lines.append(footer)
    html_parts.append(f"<p>{html.escape(footer)}</p>")
    return {"subject": subject, "since": since.isoformat(), "count": len(entries), "counts": counts,
            "entries": entries, "html": "".join(html_parts), "text": "\n".join(text_lines)}


# --------------------------------------------------------------------------- beehiiv


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().clhear_beehiiv_api_key}", "Content-Type": "application/json"}


def send_digest(engine: Engine, since: date | None = None, *, transport: Callable | None = None,
                skip_empty: bool = True) -> dict:
    """Create the digest post on beehiiv. Inert (no raise) when not configured."""
    s = get_settings()
    if not configured():
        return {"sent": False, "reason": "beehiiv not configured (CLHEAR_BEEHIIV_API_KEY / CLHEAR_BEEHIIV_PUBLICATION_ID)"}
    digest = compose_digest(engine, since)
    if skip_empty and digest["count"] == 0:
        return {"sent": False, "reason": "no changes in the window", "since": digest["since"]}
    status = s.clhear_beehiiv_post_status if s.clhear_beehiiv_post_status in ("draft", "confirmed") else "draft"
    body = {"title": digest["subject"], "body_content": digest["html"], "status": status,
            "subtitle": f"{digest['count']} changes across " + ", ".join(k for k, v in sorted(digest["counts"].items()) if v)}
    url = f"{BEEHIIV_API}/publications/{s.clhear_beehiiv_publication_id}/posts"
    try:
        resp = (transport or _http_transport)("POST", url, headers=_headers(), body=body)
    except Exception as exc:  # network / 4xx — the nightly keeps going
        log.warning("beehiiv post failed: %s", exc)
        return {"sent": False, "reason": f"beehiiv error: {exc}"[:300], "since": digest["since"], "count": digest["count"]}
    data = resp.get("data") if isinstance(resp, dict) else None
    post = (data or {}).get("data") if isinstance(data, dict) else None
    return {"sent": True, "status": status, "since": digest["since"], "count": digest["count"],
            "post_id": (post or {}).get("id") if isinstance(post, dict) else None, "http_status": (resp or {}).get("status")}


def subscribe(email: str, *, transport: Callable | None = None, source: str = "clhear.org") -> dict:
    """Add a reader to the beehiiv publication."""
    s = get_settings()
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("email required")
    if not configured():
        return {"subscribed": False, "reason": "beehiiv not configured"}
    url = f"{BEEHIIV_API}/publications/{s.clhear_beehiiv_publication_id}/subscriptions"
    body = {"email": email, "reactivate_existing": True, "send_welcome_email": True, "utm_source": source}
    try:
        resp = (transport or _http_transport)("POST", url, headers=_headers(), body=body)
    except Exception as exc:
        log.warning("beehiiv subscribe failed: %s", exc)
        return {"subscribed": False, "reason": f"beehiiv error: {exc}"[:300]}
    data = (resp or {}).get("data") if isinstance(resp, dict) else None
    sub = (data or {}).get("data") if isinstance(data, dict) else None
    return {"subscribed": True, "email": email, "subscription_id": (sub or {}).get("id") if isinstance(sub, dict) else None,
            "status": (sub or {}).get("status") if isinstance(sub, dict) else None}


class FakeTransport:
    """Records beehiiv calls for tests."""

    def __init__(self, status: int = 201):
        self.calls: list[dict] = []
        self.status = status

    def __call__(self, method: str, url: str, *, headers: dict, body: dict) -> dict:
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": json.loads(json.dumps(body))})
        kind = "post" if url.endswith("/posts") else "sub"
        return {"status": self.status, "data": {"data": {"id": f"{kind}_{len(self.calls)}", "status": body.get("status", "active")}}}
