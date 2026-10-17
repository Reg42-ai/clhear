# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Host HTTP API. A run is inserted here and executed by the worker."""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.clhear.contribution import ContributionProposal, ProposalRejected, parse_proposal
from app.clhear.l1.adapters import PUBLISHER_ADAPTER_CLASSES
from app.clhear.l1.models import SOURCE_KINDS as _SOURCE_KINDS
from app.clhear.l1.scopes import SCOPE_ENV
from app.clhear.service_auth import require_token
from app.clhear.settings import get_settings


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: str
    locator: dict = Field(default_factory=dict)
    schedule: str = ""
    licence: str = "open"
    enabled: bool = True
    name: str = ""
    kind: str = "guidance"
    jurisdiction: str = ""
    issuer: str = ""
    reference: str = ""
    binding: bool | None = None


class SourceCreate(SourceBody):
    key: str


class ScopeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label: str = ""
    sources: list[str]


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    attributes: dict


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    profiles: list[str] = Field(default_factory=list)


class WebhookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    secret: str


class SnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    licence: str = Field(description="The licence the snapshot is published under, or 'not-for-publication'")
    title: str = ""
    notice: str = ""
    profiles: list[str] = Field(default_factory=list, description="Some of the release's profiles; empty means all")


class SnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: str = Field(description="The snapshot .zip, base64-encoded")


class SnapshotMatchBody(SnapshotFile):
    attributes: dict = Field(description="The organisation profile to match: jurisdictions, roles, conditions, licences")


# A snapshot quotes clauses and lists obligations; it is small next to the texts it reads.
MAX_SNAPSHOT_BYTES = 25 * 1024 * 1024


def _engine():
    from app.clhear.runtime import engine

    return engine()


def _check_attributes(attributes: dict) -> None:
    from app.clhear.l4.validate import validate

    if not isinstance(attributes, dict):
        raise HTTPException(status_code=422, detail="attributes must be an object")
    errors = validate(None, attributes)["errors"]
    if errors:
        raise HTTPException(
            status_code=422,
            detail=" ".join(e["message"] for e in errors)
            + " A profile may include jurisdictions, roles, conditions and licences.",
        )


def _iso(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _source_out(row: dict) -> dict:
    locator = row["locator"] if isinstance(row["locator"], dict) else {}
    return {
        "key": row["key"],
        "adapter": row["adapter"],
        "locator": locator,
        "schedule": row["schedule"],
        "licence": row["licence"],
        "enabled": bool(row["enabled"]),
        "name": row["name"],
        "kind": row["kind"],
        "jurisdiction": row["jurisdiction"],
        "issuer": row["issuer"],
        "reference": row.get("reference") or "",
        "binding": row.get("binding"),
        "updated_at": _iso(row["updated_at"]),
    }


def _profile_out(row: dict) -> dict:
    attributes = row["attributes"] if isinstance(row["attributes"], dict) else {}
    return {
        "profile_id": row["profile_id"],
        "name": row["name"],
        "attributes": attributes,
        "engine_id": row.get("engine_id"),
        "updated_at": _iso(row["updated_at"]),
    }


def _run_out(row: dict) -> dict:
    return {
        "run_id": row["run_id"],
        "scope": row["scope"],
        "profiles": list(row["profiles"] or []),
        "status": row["status"],
        "release_id": row.get("release_id"),
        "error": row.get("error") or "",
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row.get("started_at")),
        "finished_at": _iso(row.get("finished_at")),
    }


def create_app() -> FastAPI:
    application = FastAPI(
        title="CLHEAR",
        version=get_settings().clhear_engine_version,
        description=(
            "Turn the texts you choose and a short description of an organisation into a compliance blueprint."
        ),
        dependencies=[Depends(require_token)],
    )

    @application.get("/v1/version")
    def version() -> dict:
        settings = get_settings()
        return {
            "engine_version": settings.clhear_engine_version,
            "api_version": settings.clhear_api_version,
            "schema_revision": settings.clhear_schema_revision,
            "image_digest": settings.clhear_image_digest,
        }

    @application.get("/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @application.get("/v1/adapters")
    def adapters() -> dict:
        from app.clhear.l1.adapters import SOURCE_ADAPTERS

        rows = [{**row, "kind": "general" if row["key"] in {"local_text", "url"} else "official"} for row in SOURCE_ADAPTERS]
        rows.extend({"key": key, "kind": "publisher", "reads": "A publisher-specific page grammar",
                     "locator": {"url": "https://... on that publisher's site"}}
                    for key in sorted(PUBLISHER_ADAPTER_CLASSES))
        return {"adapters": rows}

    @application.get("/v1/sources")
    def list_sources() -> dict:
        from app.clhear import hoststore

        return {"sources": [_source_out(row) for row in hoststore.list_sources(_engine())]}

    @application.post("/v1/sources", status_code=201)
    def create_source(body: SourceCreate) -> dict:
        return _write_source(body.key, body)

    @application.get("/v1/sources/{key}")
    def get_source(key: str) -> dict:
        from app.clhear import hoststore

        row = hoststore.get_source(_engine(), key)
        if row is None:
            raise HTTPException(status_code=404, detail="source not found")
        return _source_out(row)

    @application.put("/v1/sources/{key}")
    def put_source(key: str, body: SourceBody) -> dict:
        return _write_source(key, body)

    @application.delete("/v1/sources/{key}")
    def delete_source(key: str) -> dict:
        from app.clhear import hoststore

        if not hoststore.delete_source(_engine(), key):
            raise HTTPException(status_code=404, detail="source not found")
        return {"deleted": key}

    @application.post("/v1/sources/{key}/test-fetch")
    def test_fetch(key: str) -> dict:
        from app.clhear.first_run import preview

        try:
            return preview(_engine(), key)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="source not found") from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {str(exc)[:480]}") from exc

    @application.get("/v1/scopes")
    def list_scopes() -> dict:
        from app.clhear.l1 import scopes

        return {"scopes": [scopes.get(name) for name in scopes.names()]}

    @application.post("/v1/scopes", status_code=201)
    def create_scope(body: ScopeBody) -> dict:
        from app.clhear.l1 import scopes

        return scopes.put(body.name, body.sources, label=body.label)

    @application.get("/v1/scopes/{name}")
    def get_scope(name: str) -> dict:
        from app.clhear.l1 import scopes

        try:
            return scopes.get(name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/v1/scopes/{name}/advice")
    def scope_advice(name: str) -> dict:
        from app.clhear.advisor import advise, source_inventory
        from app.clhear.l1 import scopes

        try:
            keys = scopes.get(name)["sources"]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        with _engine().connect() as conn:
            return {"scope": name, "source_advice": advise(conn, name, keys),
                    "source_inventory": source_inventory(conn, name, keys)}

    @application.put("/v1/profiles/{profile_id}")
    def put_profile(profile_id: str, body: ProfileBody) -> dict:
        from app.clhear.first_run import put_profile

        _check_attributes(body.attributes)
        stored = put_profile(_engine(), profile_id, name=body.name, attributes=body.attributes)
        out = _profile_out(stored["row"])
        out.update(status=stored["status"], engine_id=stored["engine_id"], validation=stored["validation"])
        return out

    @application.get("/v1/profile-schema")
    def profile_schema(scope: str | None = Query(default=None)) -> dict:
        from app.clhear.l4.validate import profile_schema as schema

        with _engine().connect() as conn:
            try:
                return schema(conn, scope)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/v1/profiles/{profile_id}")
    def get_profile(profile_id: str) -> dict:
        from app.clhear import hoststore

        row = hoststore.get_profile(_engine(), profile_id)
        if row is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return _profile_out(row)

    @application.post("/v1/runs", status_code=202)
    def create_run(body: RunBody) -> dict:
        from app.clhear import hoststore
        from app.clhear.l1 import scopes

        try:
            scopes.get(body.scope)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        handle = _engine()
        for profile_id in body.profiles:
            if hoststore.get_profile(handle, profile_id) is None:
                raise HTTPException(status_code=404, detail=f"unknown profile {profile_id}")
        row = hoststore.create_run(handle, body.scope, body.profiles)
        return _run_out(row)

    @application.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        from app.clhear import hoststore

        row = hoststore.get_run(_engine(), run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _run_out(row)

    @application.get("/v1/runs/{run_id}/logs")
    def get_logs(run_id: str) -> dict:
        from app.clhear import hoststore

        row = hoststore.get_run(_engine(), run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"run_id": run_id, "logs": list(row.get("logs") or [])}

    @application.get("/v1/releases/{release_id}")
    def get_release(release_id: str) -> dict:
        row = _release_or_404(release_id)
        body = row["blueprints"] if isinstance(row["blueprints"], dict) else {}
        return {
            "release_id": row["id"],
            "scope": row["scope"],
            "run_id": row["run_id"],
            "layers": body.get("layers") or {},
            "sources": body.get("sources") or {},
            "failed_sources": body.get("failed_sources") or [],
            "lineage": body.get("lineage") or {},
            "source_advice": body.get("source_advice") or [],
            "profiles": sorted((body.get("profiles") or {}).keys()),
            "created_at": _iso(row["created_at"]),
        }

    @application.get("/v1/releases/{release_id}/blueprints/{profile_id}")
    def get_release_blueprint(release_id: str, profile_id: str) -> dict:
        row = _release_or_404(release_id)
        body = row["blueprints"] if isinstance(row["blueprints"], dict) else {}
        composition = (body.get("profiles") or {}).get(profile_id)
        if composition is None:
            raise HTTPException(status_code=404, detail="blueprint not found")
        return _public_blueprint(composition, profile_id=profile_id)

    @application.get("/v1/blueprints/{blueprint_id}")
    def get_blueprint(blueprint_id: str) -> dict:
        found = _find_blueprint(blueprint_id)
        if found is None:
            raise HTTPException(status_code=404, detail="blueprint not found")
        return found

    @application.get("/v1/blueprints/{blueprint_id}/diff")
    def diff_blueprint(blueprint_id: str, against: str = Query(...)) -> dict:
        from app.clhear.l6.diff import diff_compositions

        left = _find_blueprint(blueprint_id)
        right = _find_blueprint(against)
        if left is None or right is None:
            raise HTTPException(status_code=404, detail="blueprint not found")
        compared = diff_compositions(left, right)
        compared["blueprint_id"] = blueprint_id
        compared["against"] = against
        return compared

    @application.post("/v1/webhooks", status_code=201)
    def create_webhook(body: WebhookBody) -> dict:
        from app.clhear import hoststore

        row = hoststore.add_webhook(_engine(), body.url, body.secret)
        return {"id": row["id"], "url": row["url"], "secret": row["secret"], "created_at": _iso(row["created_at"])}

    @application.get("/v1/webhooks")
    def list_webhooks() -> dict:
        from app.clhear import hoststore

        rows = []
        for row in hoststore.list_webhooks(_engine()):
            rows.append({"id": row["id"], "url": row["url"], "created_at": _iso(row["created_at"])})
        return {"webhooks": rows}

    @application.delete("/v1/webhooks/{webhook_id}")
    def delete_webhook(webhook_id: str) -> dict:
        from app.clhear import hoststore

        if not hoststore.delete_webhook(_engine(), webhook_id):
            raise HTTPException(status_code=404, detail="webhook not found")
        return {"deleted": webhook_id}

    @application.post(
        "/v1/releases/{release_id}/snapshot",
        response_class=Response,
        responses={200: {"content": {"application/zip": {}},
                         "description": "The snapshot as a .zip. X-Content-Hash carries its content hash."}},
    )
    def create_snapshot(release_id: str, body: SnapshotBody) -> Response:
        """Write a stored release's reference blueprints as a snapshot and return it. Stores nothing."""
        import tempfile
        from pathlib import Path

        from app.clhear import snapshot

        _release_or_404(release_id)
        name = f"clhear-snapshot-{release_id}"
        with tempfile.TemporaryDirectory() as tmp:
            try:
                made = snapshot.create(_engine(), release_id, Path(tmp) / name, licence=body.licence,
                                       profiles=body.profiles or None, title=body.title, notice=body.notice,
                                       make_zip=True)
            except snapshot.SnapshotError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            data = Path(made["zip"]).read_bytes()
        return Response(content=data, media_type="application/zip",
                        headers={"X-Content-Hash": made["content_hash"],
                                 "Content-Disposition": f'attachment; filename="{name}.zip"'})

    @application.post("/v1/snapshots/verify")
    def verify_snapshot(body: SnapshotFile) -> dict:
        """Check that every file a snapshot lists is present with its hash. Reads the files only."""
        from app.clhear import snapshot

        with _snapshot_file(body.snapshot) as path:
            try:
                return snapshot.verify(path)
            except snapshot.SnapshotError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/v1/snapshots/match")
    def match_snapshot(body: SnapshotMatchBody) -> dict:
        """Whether an organisation profile gets the same verdicts as a profile of the snapshot.
        Reads the files only: no model call, nothing stored."""
        from app.clhear import snapshot

        with _snapshot_file(body.snapshot) as path:
            try:
                return snapshot.match(path, body.attributes)
            except snapshot.SnapshotError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/v1/contributions/validate", response_model=ContributionProposal)
    def validate_contribution(body: ContributionProposal) -> ContributionProposal:
        """Validate a contribution proposal. This service does not send it anywhere."""
        try:
            return parse_proposal(body.model_dump())
        except ProposalRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return application


def _write_source(key: str, body: SourceBody) -> dict:
    from app.clhear import hoststore

    if body.kind not in _SOURCE_KINDS:
        raise HTTPException(status_code=422, detail="kind is not a source kind")
    if body.licence not in {"open", "restricted"}:
        raise HTTPException(status_code=422, detail="licence must be open or restricted")
    try:
        row = hoststore.upsert_source(_engine(), key, body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _source_out(row)


@contextmanager
def _snapshot_file(encoded: str):
    """A base64 snapshot .zip as a temporary file, removed afterwards."""
    import base64
    import binascii
    import tempfile
    from pathlib import Path

    try:
        data = base64.b64decode(encoded or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="snapshot is not base64") from exc
    if not data:
        raise HTTPException(status_code=422, detail="snapshot is empty")
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail=f"a snapshot is at most {MAX_SNAPSHOT_BYTES} bytes")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.zip"
        path.write_bytes(data)
        yield path


def _release_or_404(release_id: str) -> dict:
    from app.clhear import hoststore

    row = hoststore.get_release(_engine(), release_id)
    if row is None:
        raise HTTPException(status_code=404, detail="release not found")
    return row


def _public_blueprint(composition: dict, *, profile_id: str | None = None) -> dict:
    from app.clhear.blueprint_view import public_blueprint

    return public_blueprint(composition, profile_id=profile_id)


def _find_blueprint(blueprint_id: str) -> dict | None:
    from app.clhear.l6 import composer

    handle = _engine()
    with handle.connect() as conn:
        stored = composer.get_blueprint(conn, blueprint_id)
    if stored and stored.get("composition"):
        composition = dict(stored["composition"])
        composition["blueprint_id"] = stored["blueprint_id"]
        return _public_blueprint(composition, profile_id=stored.get("profile_id"))
    with handle.connect() as conn:
        from app.clhear.hoststore import host_releases
        import sqlalchemy as sa

        rows = conn.execute(sa.select(host_releases)).all()
    for row in rows:
        body = row.blueprints if isinstance(row.blueprints, dict) else {}
        for host_id, composition in (body.get("profiles") or {}).items():
            if composition.get("blueprint_id") == blueprint_id:
                return _public_blueprint(composition, profile_id=host_id)
    return None


app = create_app()

# Re-exported so operators can see the scope variable the worker reads.
__all__ = ["SCOPE_ENV", "app", "create_app"]
