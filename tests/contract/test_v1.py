# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Every /v1 path, exercised the way a client with only the OpenAPI file would."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.clhear.api import app
from app.clhear.runner import work_once
from app.clhear.runtime import engine


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'clhear.db'}")
    monkeypatch.setenv("CLHEAR_SCOPES_DIR", str(tmp_path / "scopes"))
    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "fake")
    monkeypatch.setenv("CLHEAR_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("CLHEAR_SERVICE_TOKENS", "")
    monkeypatch.setenv("CLHEAR_IMAGE_DIGEST", "sha256:abc")
    monkeypatch.delenv("CLHEAR_SOURCE_SCOPE", raising=False)
    from app.clhear.runtime import reset

    reset()
    with TestClient(app) as test_client:
        yield test_client


def test_openapi_paths_match_the_contract():
    spec = yaml.safe_load(Path("openapi/clhear-v1.yaml").read_text(encoding="utf-8"))
    expected = {
        "/v1/adapters",
        "/v1/blueprints/{blueprint_id}",
        "/v1/blueprints/{blueprint_id}/diff",
        "/v1/contributions/validate",
        "/v1/health",
        "/v1/profiles/{profile_id}",
        "/v1/releases/{release_id}",
        "/v1/releases/{release_id}/blueprints/{profile_id}",
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/logs",
        "/v1/scopes",
        "/v1/scopes/{name}",
        "/v1/sources",
        "/v1/sources/{key}",
        "/v1/sources/{key}/test-fetch",
        "/v1/version",
        "/v1/webhooks",
        "/v1/webhooks/{webhook_id}",
    }
    assert set(spec["paths"]) == expected
    assert "ContributionProposal" in spec["components"]["schemas"]


def test_client_can_create_a_source_profile_run_and_blueprint(client, monkeypatch):
    version = client.get("/v1/version")
    assert version.status_code == 200
    body = version.json()
    assert body["engine_version"] == "0.1.0"
    assert body["api_version"] == "v1"
    assert body["schema_revision"] == "0041"
    assert body["image_digest"] == "sha256:abc"
    assert client.get("/v1/health").json()["status"] == "ok"
    adapters = client.get("/v1/adapters").json()["adapters"]
    assert any(row["key"] == "eur_lex" for row in adapters)

    created = client.post("/v1/sources", json={
        "key": "example-source",
        "adapter": "local_text",
        "locator": {"text": "An organisation must keep a record of each decision and the reason for it."},
        "licence": "open",
        "name": "Example source",
        "kind": "guidance",
    })
    assert created.status_code == 201
    assert client.get("/v1/sources").status_code == 200
    assert client.get("/v1/sources/example-source").json()["key"] == "example-source"
    updated = client.put("/v1/sources/example-source", json={
        "adapter": "local_text",
        "locator": {"text": "An organisation must keep a record of each decision and the reason for it."},
        "licence": "open",
        "name": "Example source",
        "kind": "guidance",
        "issuer": "Example issuer",
    })
    assert updated.status_code == 200
    fetched = client.post("/v1/sources/example-source/test-fetch")
    assert fetched.status_code == 200
    assert fetched.json()["stored"] is False
    assert fetched.json()["nodes"] == 1

    spare = client.post("/v1/sources", json={"key": "spare-source", "adapter": "local_text", "locator": {"text": "spare"}, "kind": "guidance"})
    assert spare.status_code == 201
    assert client.delete("/v1/sources/spare-source").json()["deleted"] == "spare-source"

    scope = client.post("/v1/scopes", json={"name": "example-scope", "label": "Example scope", "sources": ["example-source"]})
    assert scope.status_code == 201
    assert client.get("/v1/scopes").status_code == 200
    assert client.get("/v1/scopes/example-scope").json()["sources"] == ["example-source"]

    profile = client.put("/v1/profiles/example-profile", json={
        "name": "Example profile",
        "attributes": {"jurisdictions": [], "channels": []},
    })
    assert profile.status_code == 200
    assert client.get("/v1/profiles/example-profile").json()["profile_id"] == "example-profile"
    rejected = client.put("/v1/profiles/example-profile", json={"name": "x", "attributes": {"owner": "someone"}})
    assert rejected.status_code == 422

    hook = client.post("/v1/webhooks", json={"url": "http://127.0.0.1:9/hook", "secret": "hook-secret"})
    assert hook.status_code == 201
    assert client.get("/v1/webhooks").status_code == 200

    captured = []

    def sender(url, raw, headers):
        captured.append((url, raw, headers))

    monkeypatch.setattr("app.clhear.notify._post", sender)
    queued = client.post("/v1/runs", json={"scope": "example-scope", "profiles": ["example-profile"]})
    assert queued.status_code == 202
    run_id = queued.json()["run_id"]
    assert queued.json()["status"] == "queued"
    worked = work_once(engine())
    assert worked["run"]["status"] == "succeeded"
    finished = client.get(f"/v1/runs/{run_id}")
    assert finished.json()["status"] == "succeeded"
    logs = client.get(f"/v1/runs/{run_id}/logs").json()["logs"]
    assert logs
    release_id = finished.json()["release_id"]
    release = client.get(f"/v1/releases/{release_id}")
    assert release.status_code == 200
    assert "L1" in release.json()["layers"]
    assert "L8" in release.json()["layers"]
    blueprint = client.get(f"/v1/releases/{release_id}/blueprints/example-profile")
    assert blueprint.status_code == 200
    payload = blueprint.json()
    assert payload["items"]
    assert payload["items"][0]["explanation"]
    assert payload["coverage"][0]["source_key"] == "example-source"
    assert payload["coverage"][0]["clause_ref"]
    assert payload["minimality"]["checked"] is True
    blueprint_id = payload["blueprint_id"]
    direct = client.get(f"/v1/blueprints/{blueprint_id}")
    assert direct.status_code == 200
    diff = client.get(f"/v1/blueprints/{blueprint_id}/diff", params={"against": blueprint_id})
    assert diff.status_code == 200
    assert diff.json()["summary"]["items_added"] == 0

    events = {headers["X-CLHEAR-Event"] for _, _, headers in captured}
    assert {"run.started", "run.finished", "release.published", "blueprint.changed"} <= events
    for _url, raw, headers in captured:
        body = json.loads(raw)
        assert body["event_id"].startswith("evt_")
        digest = hmac.new(b"hook-secret", raw, hashlib.sha256).hexdigest()
        assert headers["X-CLHEAR-Signature"] == f"sha256={digest}"
        assert headers["X-CLHEAR-Event"] == body["event"]

    assert client.delete(f"/v1/webhooks/{hook.json()['id']}").status_code == 200


def test_bearer_tokens_and_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'clhear.db'}")
    monkeypatch.setenv("CLHEAR_SCOPES_DIR", str(tmp_path / "scopes"))
    monkeypatch.setenv("CLHEAR_BIND_HOST", "10.1.2.3")
    token_file = tmp_path / "tokens"
    token_file.write_text("gamma\n", encoding="utf-8")
    monkeypatch.setenv("CLHEAR_SERVICE_TOKENS", "alpha,beta")
    monkeypatch.setenv("CLHEAR_SERVICE_TOKEN_FILE", str(token_file))
    from app.clhear.runtime import reset

    reset()
    with TestClient(app) as client:
        assert client.get("/v1/health").status_code == 401
        assert client.get("/v1/health", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/v1/health", headers={"Authorization": "Bearer beta"}).status_code == 200
        assert client.get("/v1/health", headers={"Authorization": "Bearer gamma"}).status_code == 200


def test_contribution_rejects_private_fields_and_does_not_call_out(client):
    source = Path("app/clhear/contribution.py").read_text(encoding="utf-8")
    for needle in ("httpx", "urlopen", "requests.", "urllib"):
        assert needle not in source
    ok = client.post("/v1/contributions/validate", json={
        "source_keys": ["example-source"],
        "adapter_definitions": [{"adapter": "local_text", "locator": {"url": "https://example.invalid/text"}}],
        "normalizer_rules": [{"name": "trim", "pattern": "  ", "replacement": " "}],
        "structural_elements": [{
            "element_id": "el-1",
            "obligation_clause_ref": "clause-1",
            "provenance_hash": "abc",
        }],
    })
    assert ok.status_code == 200
    hidden = client.post("/v1/contributions/validate", json={
        "source_keys": ["example-source"],
        "adapter_definitions": [{"adapter": "local_text", "config": {"token": "hidden"}}],
    })
    assert hidden.status_code == 422
    extra = client.post("/v1/contributions/validate", json={"source_keys": ["example-source"], "tenant_id": "t"})
    assert extra.status_code == 422
