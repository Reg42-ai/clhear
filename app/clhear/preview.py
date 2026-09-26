# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Private, read-only UI preview over an existing L0 worker projection.

This module never migrates, seeds, exports or acquires source documents.
Lambda may supply its normally synchronized local snapshot; local preview uses
an explicit file or the same S3 reader synchronizer. Publisher permissions apply.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import sqlalchemy as sa
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from sqlalchemy.pool import NullPool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

from app.clhear.settings import get_settings

router = APIRouter()
_sync_states = {}
_sync_lock = threading.Lock()
MAX_OFFLINE_AGE_SECONDS = 24 * 60 * 60


class PreviewUnavailable(ValueError):
    """Safe configuration error; never include credentials or source text."""


def local_preview(settings=None) -> bool:
    settings = settings or get_settings()
    return settings.clhear_preview_mode and settings.clhear_public_base_url == "http://localhost:8000"


def snapshot_path(settings) -> Path:
    if settings.clhear_preview_snapshot_path:
        path = Path(settings.clhear_preview_snapshot_path).expanduser().resolve()
    else:
        url = sa.engine.make_url(settings.database_url)
        if url.drivername != "sqlite" or not url.database or url.database.startswith("file:"):
            raise PreviewUnavailable("Preview requires an existing local SQLite worker snapshot")
        path = Path(url.database).resolve()
    if not path.is_file():
        raise PreviewUnavailable("Preview snapshot is missing; obtain an authorized L0 worker projection first")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
        raise PreviewUnavailable("Preview requires a completed standalone worker snapshot, without a live write journal")
    return path


def _file_identity(path):
    value = path.stat()
    return {"size": value.st_size, "mtime_ns": value.st_mtime_ns, "inode": value.st_ino}


def _cached_etag(uri, path):
    """Reload hint only: a new process must still HEAD S3 before serving."""
    sidecar = Path(str(path) + ".preview.json")
    try:
        if sidecar.stat().st_size > 4096 or sidecar.stat().st_mode & 0o077:
            return ""
        saved = json.loads(sidecar.read_text())
        if saved.get("schema") == 1 and saved.get("uri") == uri and saved.get("file") == _file_identity(path):
            etag = saved.get("etag", "")
            return etag if isinstance(etag, str) and len(etag) <= 256 else ""
    except (OSError, ValueError, TypeError):
        pass
    return ""


def _remember_validated_snapshot(settings, manifest):
    """Keep an ETag only after manifest/evidence validation, never a TTL."""
    uri = settings.clhear_preview_snapshot_s3_uri
    if not uri:
        return
    path = snapshot_path(settings)
    state = _sync_states.get((uri, str(path)), {})
    if not state.get("etag") or state.get("failed"):
        return
    saved = {"schema": 1, "uri": uri, "etag": state["etag"], "file": _file_identity(path), "revision": manifest["revision"]}
    sidecar = Path(str(path) + ".preview.json")
    temp = Path(str(sidecar) + "." + uuid.uuid4().hex + ".tmp")
    try:
        if sidecar.is_file() and sidecar.stat().st_size <= 4096 and json.loads(sidecar.read_text()) == saved:
            return
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(saved, output, sort_keys=True)
        os.replace(temp, sidecar)
    except (OSError, ValueError, TypeError):
        # Cache acceleration is optional; a future reload safely downloads again.
        pass
    finally:
        temp.unlink(missing_ok=True)


def refresh_snapshot(settings=None, *, s3_client=None, now=None) -> dict:
    """Use the existing viewer synchronizer, staging its TTL until success."""
    settings = settings or get_settings()
    uri = settings.clhear_preview_snapshot_s3_uri
    if not uri:
        return {}
    from app.clhear.snapshot_sync import sync_snapshot
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/") or parsed.query or parsed.fragment:
        raise PreviewUnavailable("Preview requires a valid private S3 snapshot URI")
    if not settings.clhear_preview_snapshot_path:
        raise PreviewUnavailable("A refreshed local preview requires an explicit private cache path")
    path = Path(settings.clhear_preview_snapshot_path).expanduser().resolve()
    with _sync_lock:
        state = _sync_states.setdefault((uri, str(path)), {"etag": _cached_etag(uri, path), "checked": 0.0, "failed": False})
        pending = dict(state)
        try:
            replaced = sync_snapshot(uri, pending, local_path=str(path), s3_client=s3_client, now=now,
                                     force=state["failed"] or not path.exists())
            if not path.is_file():
                raise FileNotFoundError("snapshot unavailable")
            if replaced:
                os.chmod(path, 0o600)
                from app.clhear.db import dispose_engine
                dispose_engine()
            pending["failed"] = False
            state.update(pending)
        except Exception as exc:
            state["failed"] = True
            raise PreviewUnavailable("The current worker snapshot could not be checked; preview is unavailable") from exc
        return dict(state)


def validate_configuration(settings) -> None:
    if not settings.clhear_restricted_access or settings.clhear_auth_debug:
        raise PreviewUnavailable("Preview requires restricted access and production authentication")
    if len(settings.clhear_session_secret.strip()) < 32 or not settings.clhear_reviewer_emails.strip():
        raise PreviewUnavailable("Preview requires a separate private session secret and explicit reviewer emails")
    base = urlparse(settings.clhear_public_base_url)
    if not local_preview(settings) and (base.scheme != "https" or not base.hostname or base.username or base.password):
        raise PreviewUnavailable("Hosted preview requires HTTPS; local preview uses http://localhost:8000")
    cognito = all((settings.clhear_cognito_region, settings.clhear_cognito_user_pool_id,
                   settings.clhear_cognito_client_id, settings.clhear_cognito_domain))
    google = bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)
    if not cognito and not google:
        raise PreviewUnavailable("Preview requires configured Cognito or Google sign-in; debug and email links are disabled")
    refresh_snapshot(settings)
    snapshot_path(settings)


def _manifest(engine) -> dict:
    from app.clhear.l1.viewer_snapshot import read_viewer_state

    try:
        value = read_viewer_state(engine)
        if (value.get("status") != "available" or value.get("kind") != "candidate_viewer"
                or value.get("viewer_snapshot") is not True or value.get("accepted_release") is not False
                or value.get("audience") != "restricted-reviewers"
                or value.get("source_environment") != "authoritative_postgresql"
                or value.get("database_backend") != "postgresql"):
            raise ValueError("not a worker projection")
        uuid.UUID(value["revision"])
        stamp = datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
            raise ValueError("invalid snapshot timestamp")
        from app.clhear.l1.viewer_snapshot import _required_tables
        with engine.connect() as conn:
            _required_tables(conn, historical_manifest=value)
        return value
    except Exception as exc:
        raise PreviewUnavailable("Preview requires a valid authoritative L0 worker snapshot with its evidence tables") from exc


def make_preview_engine(settings):
    validate_configuration(settings)
    from app.clhear.db import all_schemas

    path = snapshot_path(settings)
    # mode=ro fails instead of creating a missing file. query_only also blocks
    # temporary/attached-database writes. NullPool follows atomic file refreshes.
    engine = sa.create_engine("sqlite:///file:" + quote(str(path), safe="/") + "?mode=ro&uri=true",
        poolclass=NullPool, execution_options={"clhear_preview_readonly": True,
            "schema_translate_map": {schema: None for schema in all_schemas()}})

    @sa.event.listens_for(engine, "connect")
    def readonly(connection, _record):
        connection.execute("PRAGMA query_only = ON")

    try:
        manifest = _manifest(engine)
        _remember_validated_snapshot(settings, manifest)
    except Exception:
        engine.dispose()
        raise
    return engine


def preview_status() -> dict:
    from app.clhear.db import get_engine

    settings = get_settings()
    synchronization = refresh_snapshot(settings)
    value = _manifest(get_engine())
    _remember_validated_snapshot(settings, value)
    generated = datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00"))
    age = max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
    offline = local_preview() and not settings.clhear_preview_snapshot_s3_uri
    if offline and age > MAX_OFFLINE_AGE_SECONDS:
        raise PreviewUnavailable("Offline snapshot is older than 24 hours; use a newly authorized worker snapshot")
    mode = "offline_snapshot" if offline else ("refreshed_snapshot" if settings.clhear_preview_snapshot_s3_uri else "hosted_snapshot")
    return {"preview": True, "read_only": True, "mode": mode,
            "revision": value["revision"], "snapshot_generated_at": value["generated_at"],
            "snapshot_age_seconds": age,
            "snapshot_checked_at": datetime.fromtimestamp(synchronization["checked"], timezone.utc).isoformat() if synchronization.get("checked") else None,
            "snapshot_refresh_max_age_seconds": 300 if settings.clhear_preview_snapshot_s3_uri else None,
            "source_environment": value["source_environment"], "worker_job_id": value.get("worker_job_id"),
            "publisher_checks_live": False, "accepted_release": False,
            "notice": "Snapshot evidence only. Publisher checks and worker jobs do not run here. "
                      "Permission expiry is enforced; publisher-grant revocations require a newer worker snapshot. "
                      "Internal-use overrides also require the current worker-published access control on each read."}


@router.get("/api/clhear/preview")
def status() -> dict:
    return preview_status()


# Deliberately exclude GET endpoints that create records (such as eval studio's
# sample_tasks), model calls and exports. New surfaces need an explicit review.
_READ_FUNCTIONS = {
    "app.clhear.l1.routes": {"viewer_snapshot_state", "l1_inventory", "l1_publishers", "l1_cycles", "l1_workflow", "list_sources",
                            "source_document", "node_inspector", "source_clauses", "source_evals", "source_inventory",
                            "source_detail", "meta", "recent_changes", "search_clauses", "activity", "fleet_board",
                            "latest_job", "job_detail", "run_detail", "sources_explorer", "l1_browser"},
    "app.clhear.layer_routes": {"layers_index", "layer_detail", "layer_lineage", "stack_home", "legal_meta",
                               "disclaimer_page", "terms_page", "theme_css"},
    "app.clhear.routes": {"health", "list_proposals", "review_console"},
    "app.clhear.ai_routes": {"router_state", "ops_feed", "team", "corrections", "how_live"},
    "app.clhear.accounts": {"me", "cognito_start", "cognito_callback", "google_start", "google_callback",
                            "sso_start", "sso_domains"},
    "app.clhear.review_access": {"sign_in"}, "app.clhear.preview": {"status"},
}


class PreviewMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, routes):
        super().__init__(app)
        self.routes = routes

    async def dispatch(self, request, call_next):
        if local_preview() and request.headers.get("host", "").lower() not in {"localhost:8000", "127.0.0.1:8000"}:
            return JSONResponse({"detail": "Local preview accepts loopback hosts only"}, status_code=400)
        allowed = False
        for route in self.routes:
            match, _ = route.matches(request.scope)
            if match == Match.FULL:
                endpoint = getattr(route, "endpoint", None)
                module, name = getattr(endpoint, "__module__", ""), getattr(endpoint, "__name__", "")
                allowed = request.method in {"GET", "HEAD"} and name in _READ_FUNCTIONS.get(module, set())
                allowed = allowed or (request.method == "POST" and module == "app.clhear.accounts" and name == "logout")
                break
        if not allowed:
            return JSONResponse({"detail": "Read-only preview: this operation is unavailable. Use CLHEAR's workers for changes."},
                                status_code=405, headers={"Cache-Control": "private, no-store", "X-CLHEAR-Preview": "read-only"})
        try:
            evidence = preview_status()
        except PreviewUnavailable:
            return JSONResponse({"detail": "The worker snapshot is unavailable; preview cannot serve an empty or unverified corpus."},
                                status_code=503, headers={"Cache-Control": "private, no-store"})
        response = await call_next(request)
        if response.status_code == 200 and response.headers.get("content-type", "").startswith("text/html"):
            body = b"".join([part async for part in response.body_iterator]).decode("utf-8")
            message = ("Read-only preview · " + ("offline snapshot" if evidence["mode"] == "offline_snapshot" else "worker snapshot")
                       + " from " + evidence["snapshot_generated_at"] + " · "
                       + str(evidence["snapshot_age_seconds"] // 60) + " min old. No live publisher checks or worker actions.")
            banner = ('<aside role="note" aria-label="Preview status" style="position:relative;z-index:1000;'
                      'padding:12px 20px;background:#fff4ce;color:#493800;border-bottom:2px solid #8a6800;'
                      'font:14px/1.5 system-ui">' + html.escape(message) + '</aside>')
            body = re.sub(r"(<body\b[^>]*>)", lambda m: m[1] + banner, body, count=1, flags=re.I)
            replacement = Response(body, status_code=response.status_code, background=response.background)
            replacement.raw_headers = [(key, value) for key, value in response.raw_headers
                                       if key.lower() not in {b"content-length", b"etag"}]
            replacement.headers["Content-Length"] = str(len(body.encode("utf-8")))
            response = replacement
        response.headers["X-CLHEAR-Preview"] = "read-only"
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["X-CLHEAR-Preview-Snapshot-At"] = evidence["snapshot_generated_at"]
        return response
