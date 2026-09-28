# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""The same engine, three sectors: every record comes from the texts in scope.

The three texts below are written for these tests; none is a real law. Each
runs through the live path (API, worker, the Anthropic provider on a mock
transport). For each, the roles, conditions and measures in the blueprint are
words of that text, every quote holds against its clause, and what the text
does not support is reported as an evidence gap, not filled in.
"""
from __future__ import annotations

import json
import re

import pytest

from .test_live_run import _assert_quotes_hold, _run, live  # noqa: F401  (fixture)

HEALTH = """Illustrative health information rule

This text is written for CLHEAR's tests. It is not a law.

Section 1. In this rule, "covered entity" means a health plan or a health care provider that transmits health information in electronic form.

Section 2. A covered entity must designate a privacy official who is responsible for its privacy policies.

Section 3. Where a covered entity maintains electronic protected health information, it must encrypt that information at rest.

Section 4. A covered entity must notify affected individuals within 60 days where a breach of unsecured protected health information is discovered.

Section 5. A business associate must report to the covered entity any security incident of which it becomes aware.

Section 6. The Department shall publish guidance on the implementation of this rule.

Section 7. A covered entity must retain the documentation required by this rule for six years from the date of its creation.
"""

SAFETY = """Illustrative workplace safety rule

This text is written for CLHEAR's tests. It is not a law.

Rule 1. Every employer shall provide personal protective equipment to employees at no cost to them.

Rule 2. Where an employer operates powered industrial trucks, it shall ensure that each operator is trained and certified.

Rule 3. An employer shall keep a record of each work-related injury within seven calendar days of learning of it.

Rule 4. Employees shall use the protective equipment provided to them.

Rule 5. A recalling firm shall notify its customers of the recall without delay.

Rule 6. A person must not operate a crane unless the person holds a valid operator certificate issued under this rule.

Rule 7. The Secretary shall publish an annual report of workplace injuries.
"""

FINANCE = """Illustrative client money rule

This text is written for CLHEAR's tests. It is not a law.

Rule 1. A firm that holds client money must segregate it from the firm's own money.

Rule 2. An investment firm must keep records of all services and transactions for five years.

Rule 3. Where a firm provides investment advice to a retail client, it must assess the suitability of the advice.

Rule 4. The competent authority shall publish a register of authorised firms.
"""

CASES = {
    "health": {
        "text": HEALTH, "jurisdiction": "US", "authority": "sec-6",
        "roles": {"covered entity", "business associate"},
        "facts": {"maintains electronic protected health information"},
        "undefined": {"business associate"},
        "answers": {"jurisdictions": ["US"], "roles": ["covered entity"],
                    "conditions": {"maintains electronic protected health information": True}},
        "applies": {"sec-2", "sec-3", "sec-4", "sec-7"}, "open": {"sec-5"},
    },
    "safety": {
        "text": SAFETY, "jurisdiction": "", "authority": "rule-7",
        "roles": {"employer", "employee", "recalling firm"},
        "facts": {"operates powered industrial trucks", "holds a valid operator certificate issued under this rule"},
        "undefined": {"employer", "employee", "recalling firm"},
        "answers": {"roles": {"employer": True, "employee": False, "recalling firm": False},
                    "conditions": {"operates powered industrial trucks": False,
                                   "holds a valid operator certificate issued under this rule": True}},
        "applies": {"rule-1", "rule-3"}, "open": set(),
    },
    "finance": {
        "text": FINANCE, "jurisdiction": "UK", "authority": "rule-4",
        "roles": {"firm", "investment firm"},
        "facts": {"holds client money", "provides investment advice to a retail client"},
        "undefined": {"firm", "investment firm"},
        "answers": {"jurisdictions": ["UK"], "roles": ["firm"], "conditions": {"holds client money": True}},
        "applies": {"rule-1"}, "open": {"rule-2", "rule-3"},
    },
}


@pytest.mark.parametrize("sector", sorted(CASES))
def test_each_sector_gets_a_blueprint_from_its_own_words(live, sector):  # noqa: F811
    case = CASES[sector]
    client, _ = live
    client.post("/v1/sources", json={"key": sector, "adapter": "local_text", "kind": "regulation",
                                     "jurisdiction": case["jurisdiction"], "locator": {"text": case["text"]}})
    client.post("/v1/scopes", json={"name": sector, "sources": [sector]})
    client.put(f"/v1/profiles/{sector}-org", json={"attributes": case["answers"]})
    client.put(f"/v1/profiles/{sector}-blank", json={"attributes": {}})
    release = _run(client, sector, [f"{sector}-org", f"{sector}-blank"])

    asked = client.get("/v1/profile-schema", params={"scope": sector}).json()["questions"]
    assert {r["role"] for r in asked["roles"]} == case["roles"]
    assert {c["fact"] for c in asked["conditions"]} == case["facts"]
    folded = " ".join(case["text"].lower().split())
    for question in asked["roles"] + asked["conditions"]:  # every question is the text's own words
        assert question["quotes"] and all(" ".join(q["quote"].lower().split()) in folded for q in question["quotes"])

    blueprint = client.get(f"/v1/releases/{release}/blueprints/{sector}-org").json()
    refs = {c["clause_ref"] for c in blueprint["coverage"]}
    assert case["applies"] <= refs and case["authority"] not in refs
    assert {u["clause_ref"] for u in blueprint["undetermined"]} == case["open"]
    assert all(c["state"] == "covered" for c in blueprint["coverage"])
    _assert_quotes_hold(blueprint)
    for item in blueprint["items"]:  # every measure is named in the text's words
        assert not re.search(r"\b(?:procedure|programme|framework|policy)\b", item["name"], re.I) or \
            item["name"].lower().split()[-1] in folded
    gaps = {(g["kind"], g["subject"]) for g in blueprint["evidence_gaps"]}
    assert ("no_licence_types", "licences") in gaps and ("no_enforcement_sources", "L7") in gaps
    assert {s.removeprefix("role:") for k, s in gaps if k == "role_undefined"} == case["undefined"]

    blank = client.get(f"/v1/releases/{release}/blueprints/{sector}-blank").json()
    assert blank["open_questions"], "an empty profile leaves the text's questions open"
    assert all(q["evidence"] for q in blank["open_questions"])
    lineage = client.get(f"/v1/releases/{release}").json()["lineage"]
    assert lineage["unanchored"] == [] and lineage["rows"] == lineage["anchored"] > 0


def test_a_measure_name_with_words_not_in_the_text_is_rejected(live, monkeypatch):  # noqa: F811
    from . import scripted_model

    def inventive(prompt, system=None, model=None):
        if (system or "").startswith("You design controls"):
            rows = list(scripted_model._OBLIGATION_LINE.finditer(prompt))
            return json.dumps({"measures": [{"name": "Enterprise governance dashboard", "kind": "System",
                                             "kind_quote": "dashboard",
                                             "satisfies": [{"source_key": r.group("key"), "refs": [r.group("ref")]}
                                                           for r in rows]}]})
        return scripted_model.respond(prompt, system, model)

    from . import test_live_run

    monkeypatch.setattr(test_live_run, "respond", inventive)
    client, _ = live
    client.post("/v1/sources", json={"key": "safety", "adapter": "local_text", "locator": {"text": SAFETY}})
    client.post("/v1/scopes", json={"name": "safety", "sources": ["safety"]})
    client.put("/v1/profiles/co", json={"attributes": CASES["safety"]["answers"]})
    release = _run(client, "safety", ["co"])
    blueprint = client.get(f"/v1/releases/{release}/blueprints/co").json()
    assert all("dashboard" not in item["name"].lower() for item in blueprint["items"])
    rejected = [g for g in blueprint["evidence_gaps"] if g["kind"] == "measure_name_rejected"]
    assert rejected and "dashboard" in rejected[0]["detail"]["words_not_in_text"]
    assert {c["clause_ref"] for c in blueprint["coverage"] if c["state"] == "covered"} >= {"rule-1", "rule-3"}


def _code_without_prose(path) -> str:
    """The module's code: docstrings and comments removed, string literals kept."""
    import ast
    import io
    import tokenize

    source = path.read_text()
    docstrings = set()
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            docstrings.add(body[0].lineno)
    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT or (tok.type == tokenize.STRING and tok.start[0] in docstrings):
            continue
        kept.append(tok.string)
    return " ".join(kept)


def test_the_engine_carries_no_sector_vocabulary():
    """No list, cue or default in the derivation code names a sector's things."""
    from pathlib import Path

    core = [p for layer in ("l2", "l3", "l4", "l5", "l6") for p in Path(f"app/clhear/{layer}").glob("*.py")]
    words = re.compile(r"client money|investment (?:firm|adviser)|e-?money|crypto-?asset|broker-dealer|MLRO|"
                       r"own funds|payment institution|retail client|financial entit|protected health|"
                       r"covered entit|patient|employer", re.I)
    found = {str(p): sorted(set(words.findall(code))) for p in core if words.search(code := _code_without_prose(p))}
    assert found == {}
    import app.clhear.curated as curated

    assert not hasattr(curated, "seed") and not hasattr(curated, "seed_concepts")
