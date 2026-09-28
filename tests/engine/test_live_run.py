# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The live path end to end: the HTTP API, the worker, and the Anthropic
provider talking to a mock transport that answers like a careful model."""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from .scripted_model import respond
from .test_l2_l3 import PRIVACY

SECURITY_STANDARD = """Information security baseline

Section 1. Every organisation shall appoint a person responsible for information security.

Section 2. Every organisation shall review user access rights at least once a year.

Section 3. Staff should complete security awareness training when they join.
"""

OTHER_RULE = """Section 1. Every operator shall keep a register of complaints received.
"""


def _answer(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    system = body.get("system") or ""
    prompt = body["messages"][0]["content"]
    text = respond(prompt, system=system if isinstance(system, str) else "")
    return httpx2.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 20}})


@pytest.fixture()
def live(install, monkeypatch):
    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    real = anthropic.Anthropic

    def mocked(**kwargs):
        kwargs["http_client"] = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(_answer))
        return real(**kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", mocked)
    events = []
    monkeypatch.setattr("app.clhear.notify._post", lambda url, raw, headers: events.append(headers["X-CLHEAR-Event"]))
    from app.clhear.runtime import reset

    reset()
    from app.clhear.api import app

    with TestClient(app) as client:
        client.post("/v1/webhooks", json={"url": "http://127.0.0.1:9/hook", "secret": "s"})
        yield client, events


def _run(client, scope, profiles):
    from app.clhear.runner import work_once
    from app.clhear.runtime import engine

    queued = client.post("/v1/runs", json={"scope": scope, "profiles": profiles}).json()
    work_once(engine())
    run = client.get(f"/v1/runs/{queued['run_id']}").json()
    assert run["status"] == "succeeded", run
    return run["release_id"]


def test_a_live_run_produces_a_traceable_blueprint(live):
    client, events = live
    client.post("/v1/sources", json={"key": "privacy", "adapter": "local_text", "jurisdiction": "EU",
                                     "name": "Privacy regulation", "locator": {"text": PRIVACY}})
    client.post("/v1/sources", json={"key": "baseline", "adapter": "local_text", "name": "Security baseline",
                                     "locator": {"text": SECURITY_STANDARD}})
    preview = client.post("/v1/sources/baseline/test-fetch").json()
    assert preview["clauses"] >= 3 and preview["preview"][1]["clause_ref"] == "sec-1"
    client.post("/v1/scopes", json={"name": "program", "sources": ["privacy", "baseline"]})
    client.put("/v1/profiles/eu-co", json={"name": "EU company",
                                           "attributes": {"jurisdictions": ["EU"], "roles": ["controller"]}})
    client.put("/v1/profiles/us-co", json={"name": "US company",
                                           "attributes": {"jurisdictions": ["US"], "roles": ["controller"]}})
    release = _run(client, "program", ["eu-co", "us-co"])

    eu = client.get(f"/v1/releases/{release}/blueprints/eu-co").json()
    refs = {(c["source_key"], c["clause_ref"]) for c in eu["coverage"]}
    assert {("privacy", "art-32/1"), ("privacy", "art-5/1/a"), ("privacy", "art-5/1/b"),
            ("baseline", "sec-1"), ("baseline", "sec-2")} <= refs
    duties = {c["clause_ref"]: c["duty"] for c in eu["coverage"]}
    assert duties["art-5/1/b"].startswith("Personal data shall be kept in a form")
    assert ("privacy", "art-58/1") not in refs  # an authority's power is not the company's duty
    assert all(c["state"] == "covered" for c in eu["coverage"])
    assert all(c["duty"] for c in eu["coverage"])
    assert eu["minimality"]["checked"] and eu["minimality"]["minimal"]
    assert eu["items"] and all(item["explanation"] for item in eu["items"])
    assert eu["not_applicable"] == [] and eu["undetermined"] == []
    assert eu["scope"]["source_keys"] == ["baseline", "privacy"]
    _assert_quotes_hold(eu)

    us = client.get(f"/v1/releases/{release}/blueprints/us-co").json()
    assert {c["source_key"] for c in us["coverage"]} == {"baseline"}
    assert {n["source_key"] for n in us["not_applicable"]} == {"privacy"}
    assert us["not_applicable"][0]["because"][0]["requires"] == {"jurisdictions": "EU"}

    assert {"run.started", "run.finished", "release.published", "blueprint.changed"} <= set(events)
    assert "source.failed" not in events
    released = client.get(f"/v1/releases/{release}").json()
    assert released["layers"]["L2"]["model_calls"]["failed"] == 0
    assert released["lineage"]["checked"] and released["lineage"]["unanchored"] == []
    assert released["lineage"]["rows"] == released["lineage"]["anchored"] > 0
    assert released["layers"]["L7"]["built"] is False  # no enforcement source in scope
    kinds = {g["kind"] for g in eu["evidence_gaps"]}
    assert {"no_enforcement_sources", "no_licence_types"} <= kinds


def test_runs_over_different_scopes_do_not_mix(live):
    client, _ = live
    client.post("/v1/sources", json={"key": "baseline", "adapter": "local_text", "locator": {"text": SECURITY_STANDARD}})
    client.post("/v1/sources", json={"key": "complaints", "adapter": "local_text", "locator": {"text": OTHER_RULE}})
    client.post("/v1/scopes", json={"name": "security", "sources": ["baseline"]})
    client.post("/v1/scopes", json={"name": "complaints", "sources": ["complaints"]})
    client.put("/v1/profiles/co", json={"attributes": {"jurisdictions": ["EU"], "roles": ["operator"]}})
    client.put("/v1/profiles/twin", json={"attributes": {"jurisdictions": ["EU"], "roles": ["operator"]}})
    first = _run(client, "security", ["co"])
    second = _run(client, "complaints", ["co", "twin"])

    assert {c["source_key"] for c in client.get(f"/v1/releases/{first}/blueprints/co").json()["coverage"]} == {"baseline"}
    for profile in ("co", "twin"):
        coverage = client.get(f"/v1/releases/{second}/blueprints/{profile}").json()["coverage"]
        assert {c["source_key"] for c in coverage} == {"complaints"}


def test_a_scope_with_no_readable_text_fails_with_the_reason(live):
    client, events = live
    client.post("/v1/sources", json={"key": "gone", "adapter": "local_text", "locator": {"path": "missing.txt"}})
    client.post("/v1/scopes", json={"name": "empty", "sources": ["gone"]})
    client.put("/v1/profiles/co", json={"attributes": {}})
    from app.clhear.runner import work_once
    from app.clhear.runtime import engine

    queued = client.post("/v1/runs", json={"scope": "empty", "profiles": ["co"]}).json()
    with pytest.raises(RuntimeError):
        work_once(engine())
    run = client.get(f"/v1/runs/{queued['run_id']}").json()
    assert run["status"] == "failed" and "gone" in run["error"]
    assert "source.failed" in events and "run.failed" in events


def test_the_walkthrough_example(live):
    from pathlib import Path

    client, _ = live
    text = Path("examples/security-baseline/baseline.txt").read_text()
    profiles = json.loads(Path("examples/security-baseline/profiles.json").read_text())
    client.post("/v1/sources", json={"key": "baseline", "adapter": "local_text", "jurisdiction": "EU",
                                     "locator": {"text": text}})
    client.post("/v1/scopes", json={"name": "security-baseline", "sources": ["baseline"]})
    for pid, body in profiles.items():
        client.put(f"/v1/profiles/{pid}", json=body)
    release = _run(client, "security-baseline", list(profiles))

    startup = client.get(f"/v1/releases/{release}/blueprints/payments-startup").json()
    lab = client.get(f"/v1/releases/{release}/blueprints/research-lab").json()
    startup_refs = {c["clause_ref"] for c in startup["coverage"]}
    assert {"sec-1", "sec-2", "sec-3", "sec-4/a", "sec-4/b", "sec-4/c", "sec-5", "sec-6"} <= startup_refs
    assert "sec-8" not in startup_refs  # the supervisory authority's duty, not the organisation's
    assert "sec-5" not in {c["clause_ref"] for c in lab["coverage"]}
    assert [n["clause_ref"] for n in lab["not_applicable"]] == ["sec-5"]
    because = lab["not_applicable"][0]["because"][0]
    assert because["requires"]["fact"] == "processes personal data" and because["requires"]["expect"] is True
    assert because["evidence"]["condition"][0]["quote"] == "Where an organisation processes personal data"
    _assert_quotes_hold(startup)


def _assert_quotes_hold(blueprint):
    """Every quote a blueprint shows is the clause's own text at its offsets."""
    import sqlalchemy as sa

    from app.clhear.l1.models import clauses
    from app.clhear.runtime import engine

    quotes = [c["evidence"] for c in blueprint["coverage"]]
    quotes += [q for it in blueprint["items"] for q in it["evidence"].get("name") or []]
    quotes += [q for it in blueprint["items"] for ch in it["characteristics"] for q in ch["evidence"]]
    assert quotes and all(quotes)
    with engine().connect() as conn:
        text = dict(conn.execute(sa.select(clauses.c.id, clauses.c.text)).all())
    for q in quotes:
        assert text[q["clause_id"]][q["start"]:q["end"]] == q["quote"]
