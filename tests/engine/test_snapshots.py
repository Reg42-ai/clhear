# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Snapshots: a release's reference blueprints as files anyone can check and match.

The text below is written for these tests. A snapshot is made from a stored
release, verified file by file, matched against organisation profiles without a
database or a model, and carries the recipe to run its scope again.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from .test_live_run import _run, live  # noqa: F401  (fixture)

RULE = """Rule on model records

This text is written for CLHEAR's tests. It is not a law.

Section 1. Every firm shall keep a record of each model it uses.

Section 2. The management body shall approve the model record policy.

Section 3. Where a firm uses a model to decide on credit, it shall explain each refusal to the applicant.
"""

PROFILES = {
    "lender": {"name": "Lender", "attributes": {"jurisdictions": ["EU"], "roles": ["firm", "management body"],
                                                "conditions": {"uses a model to decide on credit": True}}},
    "insurer": {"name": "Insurer", "attributes": {"jurisdictions": ["EU"], "roles": ["firm", "management body"],
                                                  "conditions": {"uses a model to decide on credit": False}}},
}


@pytest.fixture()
def released(live):  # noqa: F811
    client, _ = live
    client.post("/v1/sources", json={"key": "records", "adapter": "local_text", "kind": "regulation",
                                     "jurisdiction": "EU", "issuer": "Example authority", "locator": {"text": RULE}})
    client.post("/v1/scopes", json={"name": "models", "sources": ["records"]})
    for pid, body in PROFILES.items():
        client.put(f"/v1/profiles/{pid}", json=body)
    return client, _run(client, "models", list(PROFILES))


def _snapshot(tmp_path, release, **kwargs):
    from app.clhear import snapshot
    from app.clhear.runtime import engine

    options = {"licence": "CC-BY-4.0", "title": "Model records", "make_zip": True, **kwargs}
    return snapshot.create(engine(), release, tmp_path / "snap", **options)


def test_a_snapshot_verifies_and_carries_each_profile(released, tmp_path):
    from app.clhear import snapshot

    client, release = released
    made = _snapshot(tmp_path, release)
    assert made["profiles"] == ["insurer", "lender"]
    for where in (made["path"], made["zip"]):
        checked = snapshot.verify(where)
        assert checked["valid"], checked["problems"]
        assert checked["content_hash"] == made["content_hash"] and checked["licence"] == "CC-BY-4.0"
    manifest = json.loads((tmp_path / "snap" / "manifest.json").read_text())
    assert manifest["release"]["id"] == release and manifest["scope"]["source_keys"] == ["records"]
    assert {p["profile_id"]: p["obligations"]["not_applicable"] for p in manifest["profiles"]} == {"lender": 0, "insurer": 1}
    assert manifest["models"] and manifest["models"][0]["calls"] > 0  # the run's model is named
    assert manifest["sources"][0]["version"]["sha256"]  # the hash of the text that was read
    # The stored blueprint is the one the API serves, with the profile it is for.
    stored = json.loads((tmp_path / "snap" / "blueprints" / "lender.json").read_text())
    served = client.get(f"/v1/releases/{release}/blueprints/lender").json()
    assert stored == json.loads(json.dumps(served))
    assert stored["profile_attributes"]["conditions"] == {"uses a model to decide on credit": True}
    with zipfile.ZipFile(made["zip"]) as archive:
        assert sorted(archive.namelist())[0].startswith("snap/")


def test_a_profile_matches_when_every_verdict_is_the_same(released, tmp_path):
    from app.clhear import snapshot

    _, release = released
    made = _snapshot(tmp_path, release)
    same = snapshot.match(made["zip"], {"jurisdictions": ["EU"], "roles": ["firm", "management body"],
                                        "conditions": {"uses a model to decide on credit": False}})
    assert same["matches"] == ["insurer"]
    # An answer no duty depends on changes nothing.
    extra = snapshot.match(made["path"], {"jurisdictions": ["EU"],
                                          "roles": {"firm": True, "management body": True, "auditor": True},
                                          "conditions": {"uses a model to decide on credit": True}})
    assert extra["matches"] == ["lender"]
    # A question left open is a difference, and is named.
    open_ = snapshot.match(made["path"], {"jurisdictions": ["EU"]})
    assert open_["matches"] == []
    lender = next(r for r in open_["profiles"] if r["profile_id"] == "lender")
    assert {d["clause_ref"] for d in lender["differences"]} == {"sec-1", "sec-2", "sec-3"}
    assert {"roles": ["management body"]} in lender["unanswered"] and {"roles": ["firm"]} in lender["unanswered"]
    assert "No profile of this snapshot matches yours." in snapshot.describe_match(open_)
    with pytest.raises(snapshot.SnapshotError):
        snapshot.match(made["path"], {"turnover": "large"})  # not a profile field


def test_a_changed_or_added_file_fails_the_check(released, tmp_path):
    from app.clhear import snapshot

    _, release = released
    made = _snapshot(tmp_path, release, make_zip=False)
    blueprint = tmp_path / "snap" / "blueprints" / "lender.json"
    blueprint.write_text(blueprint.read_text().replace("Every firm", "Some firms"))
    (tmp_path / "snap" / "extra.json").write_text("{}")
    checked = snapshot.verify(made["path"])
    assert not checked["valid"]
    assert checked["problems"] == ["changed: blueprints/lender.json", "not listed: extra.json"]
    with pytest.raises(snapshot.SnapshotError):
        snapshot.match(made["path"], PROFILES["lender"]["attributes"])  # a snapshot that does not verify is not matched
    # The content hash covers the manifest's own fields too: a changed licence is found.
    other = _snapshot(tmp_path / "other", release, make_zip=False)
    manifest_file = tmp_path / "other" / "snap" / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["licence"] = "CC0-1.0"
    manifest_file.write_text(json.dumps(manifest))
    assert snapshot.verify(other["path"])["problems"] == ["the content hash does not match the listed files and the manifest"]


def test_the_same_release_gives_the_same_content(released, tmp_path):
    from app.clhear import snapshot

    _, release = released
    first = _snapshot(tmp_path / "a", release)
    second = _snapshot(tmp_path / "b", release)
    assert first["content_hash"] == second["content_hash"]
    assert snapshot.verify(first["zip"])["valid"] and snapshot.verify(second["zip"])["valid"]


def test_what_a_snapshot_refuses(released, tmp_path, monkeypatch):
    from app.clhear import hoststore, snapshot
    from app.clhear.runtime import engine

    client, release = released
    with pytest.raises(snapshot.SnapshotError, match="licence"):
        _snapshot(tmp_path / "a", release, licence="")
    with pytest.raises(snapshot.SnapshotError, match="no blueprint"):
        _snapshot(tmp_path / "b", release, profiles=["nobody"])
    with pytest.raises(snapshot.SnapshotError, match="unknown release"):
        _snapshot(tmp_path / "c", "rel_missing")
    # A text that may not be republished stays out of a published snapshot.
    hoststore.upsert_source(engine(), "records", {"licence": "restricted"})
    restricted = _run(client, "models", list(PROFILES))
    with pytest.raises(snapshot.SnapshotError, match="may not be republished"):
        _snapshot(tmp_path / "d", restricted)
    kept = _snapshot(tmp_path / "e", restricted, licence=snapshot.NOT_FOR_PUBLICATION)
    assert json.loads((tmp_path / "e" / "snap" / "manifest.json").read_text())["restricted_sources"] == ["records"]
    assert kept["profiles"]


PENALISED = """Rule on model records and their penalties

This text is written for CLHEAR's tests. It is not a law.

Section 1. Every firm shall keep a record of each model it uses.

Section 2. A firm that fails to comply with section 1 is liable to a fine not exceeding EUR 10 000.
"""

GUIDE = """Guidance on model records

This text is written for CLHEAR's tests. It is not guidance of any authority.

1. A firm should name an owner for each model record and date each review.
"""


def test_a_snapshot_carries_layers_7_and_8_as_the_run_left_them(live, tmp_path):  # noqa: F811
    from app.clhear import snapshot
    from app.clhear.runtime import engine

    client, _ = live
    client.post("/v1/sources", json={"key": "records", "adapter": "local_text", "kind": "regulation",
                                     "jurisdiction": "EU", "locator": {"text": PENALISED}})
    client.post("/v1/sources", json={"key": "guide", "adapter": "local_text", "kind": "guidance", "binding": False,
                                     "jurisdiction": "EU", "locator": {"text": GUIDE}})
    client.post("/v1/scopes", json={"name": "models", "sources": ["records", "guide"]})
    client.put("/v1/profiles/firm", json={"attributes": {"jurisdictions": ["EU"], "roles": ["firm"]}})
    release = _run(client, "models", ["firm"])
    made = snapshot.create(engine(), release, tmp_path / "snap", licence="CC-BY-4.0", make_zip=True)
    assert snapshot.verify(made["zip"])["valid"]
    folder = tmp_path / "snap"
    obligations = {ob["clause_ref"]: ob["obligation_id"]
                   for ob in json.loads((folder / "applicability.json").read_text())["obligations"]}
    risk = json.loads((folder / "risk.json").read_text())
    assert risk["release"] == release and risk["method"]["method_version"]
    (penalty,) = risk["penalties"]
    assert (penalty["clause_ref"], penalty["penalty_type"], penalty["amount"], penalty["unit"]) == ("sec-2", "fine", 10000.0, "EUR")
    quoted = [q for q in penalty["evidence"].values() if q]
    assert quoted and all(q["quote"] in PENALISED for q in quoted)  # the clause's own words
    assert [o["obligation_id"] for o in penalty["obligations"]] == [obligations["sec-1"]]
    assert {s["obligation_id"] for s in risk["scores"]} == set(obligations.values())
    scored = {s["obligation_id"]: s for s in risk["scores"]}
    assert scored[obligations["sec-1"]]["dimensions"]["stated_penalty"] > 0
    practices = json.loads((folder / "practices.json").read_text())["practices"]
    assert practices and all(p["source"]["source_key"] == "guide" and p["quote"] in " ".join(GUIDE.split())
                             for p in practices)
    readme = (folder / "README.md").read_text()
    assert "1 penalties the binding texts state" in readme and f"{len(practices)} practices" in readme
    # A later run rewrites the L7 and L8 tables; the snapshot of this release keeps what this run left.
    hoststore_scope = client.post("/v1/scopes", json={"name": "records-only", "sources": ["records"]})
    assert hoststore_scope.status_code == 201
    _run(client, "records-only", ["firm"])
    again = snapshot.create(engine(), release, tmp_path / "again", licence="CC-BY-4.0")
    assert again["content_hash"] == made["content_hash"]


def test_the_recipe_runs_the_scope_again(released, tmp_path, monkeypatch):
    from app.clhear import recipe

    client, release = released
    _snapshot(tmp_path, release)
    held = json.loads((tmp_path / "snap" / "recipe.json").read_text())
    assert held["scope"] == {"name": "models", "sources": ["records"]}
    assert {p["profile_id"] for p in held["profiles"]} == {"lender", "insurer"}
    assert "note" in held["sources"][0]  # pasted text is not in the recipe
    with pytest.raises(recipe.RecipeError, match="no text or path"):
        recipe.apply(_engine(), held)
    held["sources"][0]["locator"] = {"text": RULE}
    held["scope"]["name"] = "models-again"
    applied = recipe.apply(_engine(), held)
    assert applied["scope"] == "models-again" and all(v["valid"] for v in applied["profiles"].values())
    again = _run(client, "models-again", ["lender", "insurer"])
    assert client.get(f"/v1/releases/{again}/blueprints/lender").json()["coverage"]


def _engine():
    from app.clhear.runtime import engine

    return engine()


def test_the_api_makes_checks_and_matches_a_snapshot(released):
    import base64
    import io

    client, release = released
    made = client.post(f"/v1/releases/{release}/snapshot", json={"licence": "CC-BY-4.0", "title": "Model records"})
    assert made.status_code == 200, made.text
    assert made.headers["content-type"] == "application/zip"
    data = made.content
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(next(archive.read(n) for n in archive.namelist() if n.endswith("/manifest.json")))
    assert made.headers["x-content-hash"] == manifest["content_hash"]
    assert manifest["title"] == "Model records"
    # The rule states no penalty and no text in scope is guidance: the manifest says so too.
    assert {w.split(":")[0] for w in manifest["warnings"]} == {"L7 was not built", "L8 was not built"}
    # The same release and options give the same content hash.
    again = client.post(f"/v1/releases/{release}/snapshot", json={"licence": "CC-BY-4.0", "title": "Model records"})
    assert again.headers["x-content-hash"] == made.headers["x-content-hash"]
    encoded = base64.b64encode(data).decode()
    checked = client.post("/v1/snapshots/verify", json={"snapshot": encoded}).json()
    assert checked["valid"] and checked["content_hash"] == manifest["content_hash"]
    matched = client.post("/v1/snapshots/match", json={"snapshot": encoded, "attributes": {
        "jurisdictions": ["EU"], "roles": ["firm", "management body"],
        "conditions": {"uses a model to decide on credit": True}}}).json()
    assert matched["matches"] == ["lender"]
    one = client.post(f"/v1/releases/{release}/snapshot", json={"licence": "CC-BY-4.0", "profiles": ["insurer"]})
    with zipfile.ZipFile(io.BytesIO(one.content)) as archive:
        assert [n for n in archive.namelist() if "/blueprints/" in n] == [f"clhear-snapshot-{release}/blueprints/insurer.json"]


def test_what_the_snapshot_api_refuses(released):
    import base64

    client, release = released
    assert client.post("/v1/releases/rel_missing/snapshot", json={"licence": "CC-BY-4.0"}).status_code == 404
    assert client.post(f"/v1/releases/{release}/snapshot", json={"licence": ""}).status_code == 422
    assert client.post(f"/v1/releases/{release}/snapshot", json={"licence": "x", "profiles": ["nobody"]}).status_code == 422
    assert client.post(f"/v1/releases/{release}/snapshot", json={"licence": "x", "zip": True}).status_code == 422
    assert client.post("/v1/snapshots/verify", json={"snapshot": "not base64!"}).status_code == 422
    not_a_zip = base64.b64encode(b"plain text").decode()
    assert client.post("/v1/snapshots/verify", json={"snapshot": not_a_zip}).status_code == 422
    made = client.post(f"/v1/releases/{release}/snapshot", json={"licence": "CC-BY-4.0"}).content
    bad_profile = client.post("/v1/snapshots/match", json={"snapshot": base64.b64encode(made).decode(),
                                                          "attributes": {"turnover": "large"}})
    assert bad_profile.status_code == 422
