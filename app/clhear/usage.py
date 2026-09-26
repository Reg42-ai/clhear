# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Who uses CLHEAR data, which layers, and how much.

Every request is recorded — reads included — with the account, key and app
that made it, the route template (never the raw URL), the layer it served, its
status, latency and size, a keyed hash of the client address and a truncated
user agent. Rows are written in batches off the request path. Daily rollups
answer "who uses which layers and how much" without scanning raw rows.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import queue
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from app.clhear import identity
from app.clhear.identity_models import account_profiles, api_usage, api_usage_daily, maintainer_actions

log = logging.getLogger("clhear.usage")

_LAYER_IN_PATH = re.compile(r"(?:^|/)(l[1-8])(?:/|$)", re.I)
_queue: "queue.Queue[dict]" = queue.Queue(maxsize=50_000)
_worker: threading.Thread | None = None
_writing = threading.Lock()
BATCH = 500


def layer_of(path: str) -> str:
    match = _LAYER_IN_PATH.search(path or "")
    return match.group(1).upper() if match else ""


def ip_hash(address: str) -> str:
    from app.clhear.settings import get_settings

    secret = (get_settings().clhear_session_secret or "clhear").encode()
    return hmac.new(secret, (address or "").encode(), hashlib.sha256).hexdigest()[:32]


def record(row: dict) -> None:
    """Queue one request for the batch writer; never blocks or raises on the request path."""
    try:
        _queue.put_nowait(row)
    except queue.Full:
        log.warning("usage queue full; dropping one request record")
    _ensure_worker()


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_drain_forever, name="usage-writer", daemon=True)
        _worker.start()


def flush(max_rows: int = BATCH) -> int:
    """Write up to ``max_rows`` queued records; a batch the writer thread already took commits first."""
    with _writing:
        return _write_batch(max_rows)


def _write_batch(max_rows: int) -> int:
    rows = []
    while len(rows) < max_rows:
        try:
            rows.append(_queue.get_nowait())
        except queue.Empty:
            break
    if not rows:
        return 0
    try:
        with identity.engine().begin() as conn:
            conn.execute(api_usage.insert(), rows)
            seen = {}
            for row in rows:
                if row.get("user_id"):
                    seen[row["user_id"]] = max(seen.get(row["user_id"], row["at"]), row["at"])
            for user_id, at in seen.items():
                conn.execute(account_profiles.update().where(account_profiles.c.user_id == user_id).values(last_seen_at=at))
    except Exception:  # noqa: BLE001 — usage must never take the site down
        log.exception("usage batch of %s rows not written", len(rows))
    return len(rows)


def discard_pending() -> int:
    """Test hook: drop queued records without writing them."""
    dropped = 0
    with _writing:
        while True:
            try:
                _queue.get_nowait()
            except queue.Empty:
                return dropped
            dropped += 1


def _drain_forever() -> None:
    while True:
        time.sleep(2)
        while flush():
            pass


def rollup(day: date, engine=None) -> int:
    """Recompute one day's rollups from raw rows (idempotent)."""
    engine = engine or identity.engine()
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    keys = (api_usage.c.user_id, api_usage.c.key_id, api_usage.c.app_id, api_usage.c.layer, api_usage.c.route)
    with engine.begin() as conn:
        rows = conn.execute(sa.select(*keys, sa.func.count().label("requests"),
                                      sa.func.sum(sa.case((api_usage.c.status >= 400, 1), else_=0)).label("errors"),
                                      sa.func.sum(api_usage.c.response_bytes).label("bytes"),
                                      sa.func.sum(api_usage.c.latency_ms).label("latency"),
                                      sa.func.max(api_usage.c.at).label("last"))
                            .where(api_usage.c.at >= start, api_usage.c.at < end).group_by(*keys)).mappings().all()
        if conn.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        values = [{
            "day": day, "user_id": r["user_id"], "key_id": r["key_id"], "app_id": r["app_id"], "layer": r["layer"],
            "route": r["route"], "requests": int(r["requests"]), "errors": int(r["errors"] or 0),
            "response_bytes": int(r["bytes"] or 0), "latency_ms_total": int(r["latency"] or 0), "last_seen_at": r["last"],
        } for r in rows]
        totals = ("requests", "errors", "response_bytes", "latency_ms_total", "last_seen_at")
        for offset in range(0, len(values), 1000):
            stmt = insert(api_usage_daily).values(values[offset:offset + 1000])
            conn.execute(stmt.on_conflict_do_update(index_elements=[c for c in api_usage_daily.primary_key.columns],
                                                    set_={name: stmt.excluded[name] for name in totals}))
    return len(rows)


def usage_for(user_id: str, *, days: int = 30, engine=None) -> dict:
    engine = engine or identity.engine()
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(sa.select(api_usage_daily.c.day, api_usage_daily.c.layer, api_usage_daily.c.key_id,
                                      sa.func.sum(api_usage_daily.c.requests).label("requests"),
                                      sa.func.sum(api_usage_daily.c.response_bytes).label("bytes"))
                            .where(api_usage_daily.c.user_id == user_id, api_usage_daily.c.day >= since)
                            .group_by(api_usage_daily.c.day, api_usage_daily.c.layer, api_usage_daily.c.key_id)
                            .order_by(api_usage_daily.c.day)).mappings().all()
    return {"user_id": user_id, "days": days, "rows": [
        {"day": str(r["day"]), "layer": r["layer"], "key_id": r["key_id"], "requests": int(r["requests"]),
         "bytes": int(r["bytes"] or 0)} for r in rows]}


def overview(*, days: int = 30, engine=None) -> dict:
    """Maintainer view: each account, the layers it used, its volume and when it was last seen."""
    engine = engine or identity.engine()
    since = datetime.now(timezone.utc).date() - timedelta(days=days)
    with engine.connect() as conn:
        per = conn.execute(sa.select(api_usage_daily.c.user_id, api_usage_daily.c.app_id, api_usage_daily.c.layer,
                                     sa.func.sum(api_usage_daily.c.requests).label("requests"),
                                     sa.func.sum(api_usage_daily.c.response_bytes).label("bytes"),
                                     sa.func.max(api_usage_daily.c.last_seen_at).label("last"))
                           .where(api_usage_daily.c.day >= since)
                           .group_by(api_usage_daily.c.user_id, api_usage_daily.c.app_id, api_usage_daily.c.layer)).mappings().all()
        profiles = {r["user_id"]: dict(r) for r in conn.execute(sa.select(account_profiles)).mappings()}
    accounts: dict[str, dict] = {}
    for r in per:
        who = r["user_id"] or f"app:{r['app_id']}"
        entry = accounts.setdefault(who, {"account": who, "layers": {}, "requests": 0, "bytes": 0, "last_seen_at": None})
        entry["layers"][r["layer"] or "-"] = entry["layers"].get(r["layer"] or "-", 0) + int(r["requests"])
        entry["requests"] += int(r["requests"])
        entry["bytes"] += int(r["bytes"] or 0)
        last = r["last"]
        if last is not None and (entry["last_seen_at"] is None or str(last) > entry["last_seen_at"]):
            entry["last_seen_at"] = str(last)
        profile = profiles.get(r["user_id"])
        if profile:
            entry.update(email=profile["email"], organization=profile["organization"], status=profile["status"])
    return {"days": days, "accounts": sorted(accounts.values(), key=lambda a: -a["requests"])}


def note_action(actor: str, action: str, subject: str, detail: dict | None = None, engine=None) -> None:
    engine = engine or identity.engine()
    with engine.begin() as conn:
        conn.execute(maintainer_actions.insert().values(actor=actor, action=action, subject=subject, detail=detail or {}))
