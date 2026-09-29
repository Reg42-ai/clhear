# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Host-declared sources, scopes, profiles, runs, releases, and webhooks.

One install, one database. These rows are how a consumer drives the engine.
They are not a regulatory catalog and they do not carry a firm's actual controls.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import L0_SCHEMA, Json, metadata

host_sources = sa.Table(
    "host_sources",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("adapter", sa.Text, nullable=False),
    sa.Column("locator", Json, nullable=False),
    sa.Column("schedule", sa.Text, nullable=False, default=""),
    sa.Column("licence", sa.Text, nullable=False, default="open"),
    sa.Column("enabled", sa.Boolean, nullable=False, default=True),
    sa.Column("name", sa.Text, nullable=False, default=""),
    sa.Column("kind", sa.Text, nullable=False, default="guidance"),
    sa.Column("jurisdiction", sa.Text, nullable=False, default=""),
    sa.Column("issuer", sa.Text, nullable=False, default=""),
    # The publisher's own reference for the text ("Act No. 12 of 2019", "2030/17"):
    # a clause that cites it by that reference resolves to this source.
    sa.Column("reference", sa.Text, nullable=False, default="", server_default=""),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    schema=L0_SCHEMA,
)

host_profiles = sa.Table(
    "host_profiles",
    metadata,
    sa.Column("profile_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False, default=""),
    sa.Column("attributes", Json, nullable=False),
    sa.Column("engine_id", sa.Text, nullable=True),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    schema=L0_SCHEMA,
)

host_runs = sa.Table(
    "host_runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("profiles", Json, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("release_id", sa.Text, nullable=True),
    sa.Column("error", sa.Text, nullable=False, default=""),
    sa.Column("logs", Json, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    schema=L0_SCHEMA,
)

host_releases = sa.Table(
    "host_releases",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("run_id", sa.Text, nullable=False),
    sa.Column("blueprints", Json, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    schema=L0_SCHEMA,
)

host_webhooks = sa.Table(
    "host_webhooks",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("url", sa.Text, nullable=False),
    sa.Column("secret", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    schema=L0_SCHEMA,
)

TABLES = (host_sources, host_profiles, host_runs, host_releases, host_webhooks)


def ensure(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in TABLES:
            table.create(conn, checkfirst=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(row) -> dict:
    return dict(row._mapping) if row is not None else {}


def upsert_source(engine: Engine, key: str, body: dict) -> dict:
    ensure(engine)
    current = get_source(engine, key)
    merged = {
        "adapter": body.get("adapter", (current or {}).get("adapter")),
        "locator": body.get("locator", (current or {}).get("locator") or {}),
        "schedule": body.get("schedule", (current or {}).get("schedule") or ""),
        "licence": body.get("licence", (current or {}).get("licence") or "open"),
        "enabled": body.get("enabled", (current or {}).get("enabled", True)),
        "name": body.get("name", (current or {}).get("name") or key),
        "kind": body.get("kind", (current or {}).get("kind") or "guidance"),
        "jurisdiction": body.get("jurisdiction", (current or {}).get("jurisdiction") or ""),
        "issuer": body.get("issuer", (current or {}).get("issuer") or ""),
        "reference": body.get("reference", (current or {}).get("reference") or ""),
    }
    if not merged["adapter"]:
        raise ValueError("adapter is required")
    if merged["licence"] not in {"open", "restricted"}:
        raise ValueError("licence must be open or restricted")
    with engine.begin() as conn:
        if current:
            conn.execute(host_sources.update().where(host_sources.c.key == key).values(**merged, updated_at=_now()))
        else:
            conn.execute(host_sources.insert().values(key=key, updated_at=_now(), **merged))
    return get_source(engine, key)


def get_source(engine: Engine, key: str) -> dict | None:
    ensure(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(host_sources).where(host_sources.c.key == key)).first()
    return _row(row) or None


def list_sources(engine: Engine) -> list[dict]:
    ensure(engine)
    with engine.connect() as conn:
        return [_row(row) for row in conn.execute(sa.select(host_sources).order_by(host_sources.c.key))]


def delete_source(engine: Engine, key: str) -> bool:
    ensure(engine)
    with engine.begin() as conn:
        result = conn.execute(host_sources.delete().where(host_sources.c.key == key))
    return bool(result.rowcount)


def registry_entries(engine: Engine, keys: list[str]) -> list[dict]:
    """Fleet-shaped declarations for the source keys a scope names."""
    wanted = set(keys)
    entries = []
    for row in list_sources(engine):
        if wanted and row["key"] not in wanted:
            continue
        locator = row["locator"] if isinstance(row["locator"], dict) else json.loads(row["locator"] or "{}")
        entries.append({
            "key": row["key"],
            "adapter": row["adapter"],
            "name": row["name"] or row["key"],
            "short_name": row["key"],
            "kind": row["kind"] or "guidance",
            "license": row["licence"] or "open",
            "jurisdiction": row["jurisdiction"] or "",
            "issuer": row["issuer"] or "",
            "family": "declared",
            "canonical_url": locator.get("url") or "",
            "fetch": locator,
            "enabled": bool(row["enabled"]),
            "topics": [],
            "publisher": row["issuer"] or "",
            "instrument": row.get("reference") or row["name"] or row["key"],
        })
    return entries


def put_profile(engine: Engine, profile_id: str, *, name: str, attributes: dict, engine_id: str | None = None) -> dict:
    ensure(engine)
    with engine.begin() as conn:
        existing = conn.execute(sa.select(host_profiles.c.profile_id).where(host_profiles.c.profile_id == profile_id)).first()
        values = {"name": name, "attributes": attributes, "engine_id": engine_id, "updated_at": _now()}
        if existing:
            conn.execute(host_profiles.update().where(host_profiles.c.profile_id == profile_id).values(**values))
        else:
            conn.execute(host_profiles.insert().values(profile_id=profile_id, **values))
    return get_profile(engine, profile_id)


def get_profile(engine: Engine, profile_id: str) -> dict | None:
    ensure(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(host_profiles).where(host_profiles.c.profile_id == profile_id)).first()
    return _row(row) or None


def set_profile_engine_id(engine: Engine, profile_id: str, engine_id: str) -> None:
    ensure(engine)
    with engine.begin() as conn:
        conn.execute(host_profiles.update().where(host_profiles.c.profile_id == profile_id).values(engine_id=engine_id, updated_at=_now()))


def create_run(engine: Engine, scope: str, profiles: list[str]) -> dict:
    ensure(engine)
    run_id = "run_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(host_runs.insert().values(
            run_id=run_id, scope=scope, profiles=list(profiles), status="queued",
            error="", logs=["queued"], created_at=_now(),
        ))
    return get_run(engine, run_id)


def get_run(engine: Engine, run_id: str) -> dict | None:
    ensure(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(host_runs).where(host_runs.c.run_id == run_id)).first()
    return _row(row) or None


def claim_run(engine: Engine) -> dict | None:
    ensure(engine)
    with engine.begin() as conn:
        row = conn.execute(
            sa.select(host_runs).where(host_runs.c.status == "queued").order_by(host_runs.c.created_at).limit(1)
        ).first()
        if row is None:
            return None
        run_id = row.run_id
        logs = list(row.logs or [])
        logs.append("started")
        conn.execute(host_runs.update().where(host_runs.c.run_id == run_id).values(
            status="running", started_at=_now(), logs=logs,
        ))
    return get_run(engine, run_id)


def append_log(engine: Engine, run_id: str, message: str) -> None:
    run = get_run(engine, run_id)
    if not run:
        return
    logs = list(run["logs"] or [])
    logs.append(message)
    with engine.begin() as conn:
        conn.execute(host_runs.update().where(host_runs.c.run_id == run_id).values(logs=logs))


def finish_run(engine: Engine, run_id: str, *, status: str, release_id: str | None = None, error: str = "") -> dict:
    run = get_run(engine, run_id)
    logs = list((run or {}).get("logs") or [])
    logs.append(status if not error else f"{status}: {error[:300]}")
    with engine.begin() as conn:
        conn.execute(host_runs.update().where(host_runs.c.run_id == run_id).values(
            status=status, release_id=release_id, error=error[:2000], logs=logs, finished_at=_now(),
        ))
    return get_run(engine, run_id)


def save_release(engine: Engine, *, scope: str, run_id: str, blueprints: dict) -> dict:
    ensure(engine)
    release_id = "rel_" + uuid.uuid4().hex[:16]
    with engine.begin() as conn:
        conn.execute(host_releases.insert().values(
            id=release_id, scope=scope, run_id=run_id, blueprints=blueprints, created_at=_now(),
        ))
    return get_release(engine, release_id)


def get_release(engine: Engine, release_id: str) -> dict | None:
    ensure(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(host_releases).where(host_releases.c.id == release_id)).first()
    return _row(row) or None


def add_webhook(engine: Engine, url: str, secret: str) -> dict:
    ensure(engine)
    webhook_id = "wh_" + uuid.uuid4().hex[:16]
    with engine.begin() as conn:
        conn.execute(host_webhooks.insert().values(id=webhook_id, url=url, secret=secret, created_at=_now()))
    return get_webhook(engine, webhook_id)


def get_webhook(engine: Engine, webhook_id: str) -> dict | None:
    ensure(engine)
    with engine.connect() as conn:
        row = conn.execute(sa.select(host_webhooks).where(host_webhooks.c.id == webhook_id)).first()
    return _row(row) or None


def list_webhooks(engine: Engine) -> list[dict]:
    ensure(engine)
    with engine.connect() as conn:
        rows = conn.execute(sa.select(host_webhooks).order_by(host_webhooks.c.created_at)).all()
    return [_public_webhook(_row(row)) for row in rows]


def _public_webhook(row: dict) -> dict:
    return {"id": row["id"], "url": row["url"], "created_at": row["created_at"]}


def delete_webhook(engine: Engine, webhook_id: str) -> bool:
    ensure(engine)
    with engine.begin() as conn:
        result = conn.execute(host_webhooks.delete().where(host_webhooks.c.id == webhook_id))
    return bool(result.rowcount)


def webhook_secrets(engine: Engine) -> list[dict]:
    ensure(engine)
    with engine.connect() as conn:
        return [_row(row) for row in conn.execute(sa.select(host_webhooks))]
