# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""HLD v2 §5 "Build on it": API keys in one click, SDK stubs, public evals.

A signed-in user issues a key; the secret is shown once and only its sha256
is stored (``community.api_keys``). Callers send it exactly like the
statically configured app keys (``X-App-Id`` + ``Authorization: Bearer``), so
``app_auth.require_app`` accepts both. Revocation sets ``revoked_at`` — the
row is never deleted (I2) and the key id (KEY-) is never reused (I11).

The agnostic blueprint itself needs no key at all (I9 — open by mode); keys
exist for rate-limited programmatic access and for the members-only modes
that later items add.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from app.clhear import identity
from app.clhear.accounts import current_user, require_user
from app.clhear.community_models import api_keys
from app.clhear.db import get_engine
from app.clhear.platform import gates
from app.clhear.platform.ids import next_id

router = APIRouter(tags=["build"])
WEB_DIR = Path(__file__).resolve().parent / "web"
SDK_DIR = Path(__file__).resolve().parents[2] / "export" / "clhear" / "sdks"

# Every public layer is readable with a self-issued key; write scopes come
# from the contribution flow (item 13), not from here.
DEFAULT_SCOPES = ("read:l1", "read:l2", "read:l3", "read:l4", "read:l5", "read:l6", "read:blueprints")
MAX_ACTIVE_KEYS = 5


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _public(row) -> dict:
    return {
        "id": row["id"], "app_id": row["app_id"], "label": row["label"], "prefix": row["prefix"],
        "scopes": list(row["scopes"] or []), "created_at": _iso(row["created_at"]),
        "last_used_at": _iso(row["last_used_at"]), "revoked_at": _iso(row["revoked_at"]),
        "active": row["revoked_at"] is None,
    }


def issue(engine: Engine, user: dict, label: str = "", scopes=DEFAULT_SCOPES) -> dict:
    """Create a key for ``user``. Returns the public row plus ``secret`` (shown once)."""
    scopes = [s for s in scopes if s in DEFAULT_SCOPES] or list(DEFAULT_SCOPES)
    secret = "clh_" + secrets.token_urlsafe(32)
    app_id = "key-" + secrets.token_hex(6)
    with engine.begin() as conn:
        active = conn.execute(
            sa.select(sa.func.count()).select_from(api_keys)
            .where(api_keys.c.user_id == user["id"]).where(api_keys.c.revoked_at.is_(None))
        ).scalar_one()
        if active >= MAX_ACTIVE_KEYS:
            raise ValueError(f"at most {MAX_ACTIVE_KEYS} active keys per account — revoke one first")
        key_id = next_id(conn, "KEY")
        conn.execute(api_keys.insert().values(
            id=key_id, user_id=user["id"], app_id=app_id, label=label.strip()[:80] or "default",
            secret_hash=_hash(secret), prefix=secret[:8], scopes=scopes,
        ))
        row = conn.execute(sa.select(api_keys).where(api_keys.c.id == key_id)).mappings().one()
    return {**_public(row), "secret": secret}


def list_for(engine: Engine, user_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(sa.select(api_keys).where(api_keys.c.user_id == user_id).order_by(api_keys.c.created_at, api_keys.c.id)).mappings().all()
    return [_public(r) for r in rows]


def revoke(engine: Engine, user_id: str, key_id: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(sa.select(api_keys).where(api_keys.c.id == key_id).where(api_keys.c.user_id == user_id)).mappings().first()
        if row is None:
            raise LookupError(key_id)
        if row["revoked_at"] is None:
            conn.execute(api_keys.update().where(api_keys.c.id == key_id).values(revoked_at=datetime.now(timezone.utc)))
        row = conn.execute(sa.select(api_keys).where(api_keys.c.id == key_id)).mappings().one()
    return _public(row)


def verify(engine: Engine, app_id: str, token: str) -> dict | None:
    """The app record for a self-issued key, or None. Touches ``last_used_at``."""
    if not app_id or not token or not app_id.startswith("key-"):
        return None
    digest = _hash(token)
    with engine.begin() as conn:
        row = conn.execute(sa.select(api_keys).where(api_keys.c.app_id == app_id)).mappings().first()
        if row is None or row["revoked_at"] is not None or not secrets.compare_digest(row["secret_hash"], digest):
            return None
        conn.execute(api_keys.update().where(api_keys.c.id == row["id"]).values(last_used_at=datetime.now(timezone.utc)))
    return {"app_id": app_id, "scopes": sorted(row["scopes"] or []), "user_id": row["user_id"], "key_id": row["id"]}


# --------------------------------------------------------------------------- SDKs


def sdk_index() -> list[dict]:
    """The SDK stubs shipped under export/clhear/sdks (item 13 publishes them)."""
    out = []
    for lang in ("python", "typescript"):
        folder = SDK_DIR / lang
        files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) if folder.exists() else []
        out.append({"language": lang, "path": f"export/clhear/sdks/{lang}", "files": files,
                    "install": {"python": "pip install clhear", "typescript": "npm install @clhear/sdk"}[lang]})
    return out


# --------------------------------------------------------------------------- routes


class KeyBody(BaseModel):
    label: str = Field(default="", max_length=80)
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))


@router.get("/build", response_class=HTMLResponse, include_in_schema=False)
def build_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "build.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/build/overview")
def overview(request: Request) -> dict:
    """What a developer gets without a key, with a key, and where the SDKs are."""
    user = current_user(request)
    return {
        "signed_in": user is not None,
        "open_without_key": [
            {"method": "POST", "path": "/v1/runs", "what": "agnostic blueprint for a described organisation"},
            {"method": "GET", "path": "/l6/blueprints/{id}/export?format=oscal", "what": "OSCAL system security plan"},
            {"method": "GET", "path": "/l6/export/oscal/components", "what": "OSCAL component definition (the L3 catalogue)"},
            {"method": "GET", "path": "/explore/node/{id}", "what": "any node: why, history, who else"},
            {"method": "GET", "path": "/watch/feed", "what": "public change feed (JSON) — /watch/feed.atom for Atom"},
            {"method": "GET", "path": "/evals/summary", "what": "published eval gates per layer"},
        ],
        "with_key": [
            {"method": "GET", "path": "/v1/layers", "what": "release-pinned layer catalogue"},
            {"method": "GET", "path": "/v1/releases/{release}/{layer}/snapshot", "what": "signed layer snapshots"},
            {"method": "POST", "path": "/v1/blueprint", "what": "blueprint composition pinned to a release"},
        ],
        "headers": {"X-App-Id": "key-…", "Authorization": "Bearer clh_…"},
        "scopes": list(DEFAULT_SCOPES),
        "max_active_keys": MAX_ACTIVE_KEYS,
        "sdks": sdk_index(),
        "interop": [
            {"format": "OSCAL 1.1 SSP", "path": "/l6/blueprints/{id}/export?format=oscal"},
            {"format": "OSCAL 1.1 component definition", "path": "/l6/export/oscal/components"},
            {"format": "JSON composition", "path": "/l6/blueprints/{id}/export?format=json"},
        ],
    }


@router.get("/keys")
def my_keys(user: dict = Depends(require_user)) -> dict:
    return {"keys": list_for(identity.engine(), user["id"]), "max_active_keys": MAX_ACTIVE_KEYS}


@router.post("/keys", status_code=201)
def create_key(body: KeyBody, user: dict = Depends(require_user)) -> dict:
    if not identity.writable():
        # A key written into the read-only snapshot copy would vanish with the task.
        raise HTTPException(status_code=503, detail="Key issuance is unavailable until the identity store is configured")
    try:
        return issue(identity.engine(), user, body.label, body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str, user: dict = Depends(require_user)) -> dict:
    try:
        return revoke(identity.engine(), user["id"], key_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown key {key_id}")


@router.get("/build/sdks")
def sdks() -> dict:
    return {"sdks": sdk_index()}


# --------------------------------------------------------------------------- public evals


@router.get("/evals", response_class=HTMLResponse, include_in_schema=False)
def evals_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "evals.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/evals/summary")
def evals_summary(release: str | None = None) -> dict:
    """The published summary table (I10) — never the eval store directly."""
    return gates.publish_summary(get_engine(), release)
