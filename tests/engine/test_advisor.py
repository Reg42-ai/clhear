# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Every layer that cannot produce records says which official sources to add, and a
user can go from their texts to a blueprint with the command line alone."""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from app.clhear.advisor import ADVICE, SOURCE_KINDS, advice_for

from .test_live_run import _answer

RULE = """Illustrative operator rule

This text is written for CLHEAR's tests. It is not a law.

Rule 1. In this rule, "operator" means a person who runs a registered site.

Rule 2. An operator shall keep a register of visitors for two years.

Rule 3. Where an operator stores visitor records electronically, it shall encrypt those records.

Rule 4. A site manager shall review the register at least once a month.

Rule 5. A person must not run a site unless the person holds a site registration certificate.
"""


@pytest.mark.parametrize("kind", sorted(ADVICE))
def test_every_gap_kind_says_what_to_add_and_how_to_register_it(kind):
    advice = advice_for(kind, field="review cadence", role="operator")
    assert advice["layer"].startswith("L") and advice["summary"] and advice["missing"]
    assert "{field}" not in json.dumps(advice) and "{role}" not in json.dumps(advice)  # placeholders filled
    for item in advice["add"]:
        assert item["register_as"] in SOURCE_KINDS and item["source"] and item["why"]
    if kind != "undetermined":
        assert advice["add"]


def test_the_advice_names_the_publisher_the_sources_declare():
    advice = advice_for("no_enforcement_sources", issuers=["Example Authority"])
    assert all(item["published_by"] == "Example Authority" for item in advice["add"])


@pytest.fixture()
def cli(install, monkeypatch):
    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    real = anthropic.Anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: real(**{
        **kw, "http_client": anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(_answer))}))
    from app.clhear.runtime import reset

    reset()
    (install / "rule.txt").write_text(RULE, encoding="utf-8")
    from app.clhear.cli import main

    def run(*argv) -> int:
        return main(list(argv))

    return run, install


def _out(capsys) -> str:
    return capsys.readouterr().out


def test_from_texts_to_a_blueprint_with_the_command_line(cli, capsys):
    run, root = cli
    assert run("sources", "add", "rule", "--text-file", str(root / "rule.txt"), "--kind", "regulation",
               "--jurisdiction", "US", "--issuer", "Example Authority") == 0
    assert run("sources", "test", "rule") == 0
    assert "rule-2" in capsys.readouterr().out
    assert run("scope", "create", "sites", "rule") == 0
    assert run("profile", "set", "blank") == 0
    _out(capsys)
    assert run("run", "--scope", "sites", "--profile-id", "blank") == 0
    ran = json.loads(_out(capsys))
    release = ran["release_id"]
    assert ran["lineage"]["unanchored"] == 0 and ran["lineage"]["rows"] == ran["lineage"]["anchored"] > 0

    assert run("profile", "questions", "--scope", "sites") == 0
    asked = capsys.readouterr().out
    assert "operator" in asked and "site manager" in asked and "stores visitor records electronically" in asked
    assert "Profiles you can start from" in asked and "You are 'operator'" in asked

    assert run("profile", "questions", "--scope", "sites", "--json") == 0
    schema = json.loads(capsys.readouterr().out)
    folded = " ".join(RULE.lower().split())
    for candidate in schema["candidates"]:  # only what the text names
        for role in candidate["attributes"].get("roles") or []:
            assert role in folded

    assert run("blueprint", "show", "--release", release, "--profile", "blank") == 0
    shown = capsys.readouterr().out
    assert "Open questions" in shown and "Sources to add" in shown and "enforcement" in shown

    assert run("profile", "set", "site-co", "--jurisdiction", "US", "--role", "operator", "--not-role", "site manager",
               "--condition", "stores visitor records electronically=true",
               "--condition", "holds a site registration certificate=true") == 0
    _out(capsys)
    assert run("run", "--scope", "sites", "--profile-id", "site-co") == 0
    release = json.loads(_out(capsys))["release_id"]
    assert run("blueprint", "show", "--release", release, "--profile", "site-co") == 0
    shown = capsys.readouterr().out
    assert "rule-2" in shown and "rule-3" in shown and "Open questions" not in shown
    assert "rule-5" in shown and "Not applicable" in shown  # holds the certificate: the prohibition does not bite

    assert run("sources", "advise", "--scope", "sites", "--json") == 0
    advice = {a["gap"]: a for a in json.loads(capsys.readouterr().out)}
    assert {"no_enforcement_sources", "no_licence_types"} <= set(advice)
    assert advice["no_enforcement_sources"]["add"][0]["published_by"] == "Example Authority"
    assert advice["no_enforcement_sources"]["add"][0]["register_as"] == "enforcement"


def test_a_scope_without_duties_is_told_to_add_the_binding_text(cli, capsys):
    run, root = cli
    (root / "index.txt").write_text("Table of contents\n\nPart 1. General provisions\n\nPart 2. Records\n",
                                    encoding="utf-8")
    run("sources", "add", "index", "--text-file", str(root / "index.txt"))
    run("scope", "create", "empty", "index")
    run("profile", "set", "co")
    run("run", "--scope", "empty", "--profile-id", "co")
    capsys.readouterr()
    assert run("sources", "advise", "--scope", "empty", "--json") == 0
    advice = {a["gap"]: a for a in json.loads(capsys.readouterr().out)}
    assert advice["no_duties"]["layer"] == "L2"
    assert {i["register_as"] for i in advice["no_duties"]["add"]} <= {"law", "regulation"}
