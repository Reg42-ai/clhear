# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Host HTTP API. A run is inserted here and executed by the worker."""
from __future__ import annotations


from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.clhear.contribution import ContributionProposal, ProposalRejected, parse_proposal
from app.clhear.l1.adapters import PUBLISHER_ADAPTER_CLASSES
from app.clhear.l1.scopes import SCOPE_ENV
from app.clhear.service_auth import require_token
from app.clhear.settings import get_settings

_SOURCE_KINDS = ("law", "regulation", "standard", "guidance", "form", "agreement", "enforcement")


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


def _engine():
    from app.clhear.runtime import engine

    return engine()


def _attribute_keys() -> set[str]:
    from app.clhear import curated

    return {item["key"] for item in curated.load("l4_attribute_schema")}


def _check_attributes(attributes: dict) -> None:
    if not isinstance(attributes, dict):
        raise HTTPException(status_code=422, detail="attributes must be an object")
    unknown = sorted(set(attributes) - _attribute_keys())
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown profile field {unknown}. "
                "A profile may include jurisdictions, authorisations, products, customer_base, "
                "channels, data_footprint, crypto_services, financial_entity_dora."
            ),
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
        from app.clhear import hoststore
        from app.clhear.l1.fleet import adapter_for

        from app.clhear.l1.models import CLAUSE_TYPES

        entry = hoststore.registry_entries(_engine(), [key])
        if not entry:
            raise HTTPException(status_code=404, detail="source not found")
        try:
            fetched = adapter_for(entry[0]).fetch()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {str(exc)[:480]}") from exc
        if fetched is None:
            return {"stored": False, "version": None, "nodes": 0, "clauses": 0, "bytes": 0, "preview": []}
        tree = getattr(fetched, "tree", None) or []
        walked = [node for root in tree for node in root.walk()]
        clause_nodes = [node for node in walked if node.node_type in CLAUSE_TYPES and node.ref]
        nbytes = sum(len(artifact.content or b"") for artifact in fetched.artifacts)
        preview = [{"clause_ref": node.ref, "text": " ".join(node.subtree_text().split())[:200]}
                   for node in clause_nodes[:8]]
        return {"stored": False, "version": fetched.version_label, "nodes": len(walked),
                "clauses": len(clause_nodes), "bytes": nbytes, "preview": preview}

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

    @application.put("/v1/profiles/{profile_id}")
    def put_profile(profile_id: str, body: ProfileBody) -> dict:
        from app.clhear import hoststore
        from app.clhear.l4.validate import create_profile

        _check_attributes(body.attributes)
        handle = _engine()
        stored = create_profile(handle, body.attributes, name=body.name, source="api", allow_invalid=True)
        row = hoststore.put_profile(
            handle, profile_id, name=body.name or stored.get("name") or profile_id,
            attributes=body.attributes, engine_id=stored.get("id"),
        )
        out = _profile_out(row)
        out["status"] = stored.get("status")
        out["engine_id"] = stored.get("id")
        validity = stored.get("validity") if isinstance(stored.get("validity"), dict) else {}
        out["validation"] = {"valid": stored.get("status") == "valid", "errors": validity.get("errors") or [],
                             "warnings": validity.get("warnings") or []}
        return out

    @application.get("/v1/profile-schema")
    def profile_schema() -> dict:
        from app.clhear.l4.validate import profile_schema as schema

        with _engine().connect() as conn:
            return {"fields": schema(conn)}

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


def _release_or_404(release_id: str) -> dict:
    from app.clhear import hoststore

    row = hoststore.get_release(_engine(), release_id)
    if row is None:
        raise HTTPException(status_code=404, detail="release not found")
    return row


def _public_blueprint(composition: dict, *, profile_id: str | None = None) -> dict:
    return {
        "blueprint_id": composition.get("blueprint_id"),
        "profile_id": profile_id or composition.get("profile_id"),
        "items": composition.get("items") or [],
        "coverage": composition.get("coverage") or [],
        "minimality": composition.get("minimality") or {},
        "coverage_summary": composition.get("coverage_summary") or {},
        "not_applicable": composition.get("not_applicable") or [],
        "scope": composition.get("scope"),
        **({"sample": True} if composition.get("sample") else {}),
        "engine_version": composition.get("engine_version"),
    }


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
