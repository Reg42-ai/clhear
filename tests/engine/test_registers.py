# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Official registers (source kind ``register``) give L4 its licence types.

The rule and the registers are written for these tests; none is real. A register
lists entities and the licence each holds, as labelled fields or as a table.
Every licence type it names is quoted from an entry and becomes a permitted value
of the profile's ``licences``; a register is never read for obligations.
"""
from __future__ import annotations

import importlib

import sqlalchemy as sa

from .test_advisor import cli  # noqa: F401  (fixture)
from .test_live_run import _run, live  # noqa: F401  (fixture)

RULE = """Harbour keeping rule

This text is written for CLHEAR's tests. It is not a law.

Rule 1. A keeper shall light the harbour lantern every evening at sunset.

Rule 2. A keeper shall record each lighting in a lantern log.
"""

KEEPERS = """Public register of lantern keepers

This register is written for CLHEAR's tests. It is not a real register.

Entry 1. Keeper: North Quay Lights. Licence type: harbour lantern keeper licence. Status: licensed.

Entry 2. Keeper: South Quay Lights. Licence type: coastal beacon keeper licence. Status: licensed.

Entry 3. Keeper: West Pier Lamps. Licence type: harbour lantern keeper licence. Licence number: 41. Status: suspended.
"""

BEACONS = """Register of beacon permits

This register is written for CLHEAR's tests. It is not a real register.

Holder | Permit category | Status
East Mole Beacons | Class A | active
Harbour Wall Lights | Class B | active
"""


def test_licence_types_are_read_from_register_entries(live):  # noqa: F811
    from app.clhear.derived_models import license_types, obligations
    from app.clhear.evidence import check, clause_rows
    from app.clhear.runtime import engine

    client, _ = live
    client.post("/v1/sources", json={"key": "rule", "adapter": "local_text", "kind": "regulation",
                                     "locator": {"text": RULE}})
    for key, text in (("keepers", KEEPERS), ("beacons", BEACONS)):
        created = client.post("/v1/sources", json={"key": key, "adapter": "local_text", "kind": "register",
                                                   "issuer": "Example Harbour Authority", "locator": {"text": text}})
        assert created.status_code == 201, created.text
    client.post("/v1/scopes", json={"name": "harbour", "sources": ["rule", "keepers", "beacons"]})
    client.put("/v1/profiles/keeper", json={"attributes": {"roles": ["keeper"],
                                                           "licences": ["harbour lantern keeper licence"]}})
    release = _run(client, "harbour", ["keeper"])

    schema = client.get("/v1/profile-schema", params={"scope": "harbour"}).json()
    licences = {lic["name"]: lic for lic in schema["questions"]["licences"]}
    assert set(licences) == {"harbour lantern keeper licence", "coastal beacon keeper licence",
                             "Permit category Class A", "Permit category Class B"}
    assert {lic["basis"] for lic in licences.values()} == {"register"}
    assert [a["ref"] for a in licences["harbour lantern keeper licence"]["quotes"]] == ["p3", "p5"]  # entries 1 and 3
    field = next(f for f in schema["fields"] if f["key"] == "licences")
    assert field["permitted_values"] == sorted(licences)
    assert "You hold 'coastal beacon keeper licence'" in {c["name"] for c in schema["candidates"]}

    with engine().connect() as conn:
        rows = conn.execute(sa.select(license_types.c.name, license_types.c.evidence)
                            .where(license_types.c.generated_by == "l4.registers")).all()
        entries = [q for r in rows for q in r.evidence["entries"]]
        held = clause_rows(conn, [q["clause_id"] for q in entries])
        from_registers = conn.execute(sa.select(obligations.c.id).where(
            obligations.c.source_key.in_(("keepers", "beacons")))).all()
    assert entries and all(check(q, held) is None for q in entries)  # every quote is the entry's own words
    assert {q["quote"] for q in entries} >= {"harbour lantern keeper licence", "Permit category", "Class A"}
    assert "41" not in {q["quote"] for q in entries}  # a licence number is not a licence type
    assert from_registers == []  # a register states no obligation

    blueprint = client.get(f"/v1/releases/{release}/blueprints/keeper").json()
    assert "no_licence_types" not in {g["kind"] for g in blueprint["evidence_gaps"]}
    assert all(w["code"] != "licence_not_in_texts" for w in blueprint["profile_warnings"])
    lineage = client.get(f"/v1/releases/{release}").json()["lineage"]
    assert lineage["unanchored"] == [] and lineage["rows"] == lineage["anchored"] > 0


def test_register_is_a_source_kind_and_the_advice_names_it(live, cli, capsys):  # noqa: F811
    from app.clhear.advisor import advice_for

    client, _ = live
    refused = client.post("/v1/sources", json={"key": "x", "adapter": "local_text", "kind": "registry",
                                               "locator": {"text": KEEPERS}})
    assert refused.status_code == 422
    run, root = cli
    (root / "keepers.txt").write_text(KEEPERS, encoding="utf-8")
    assert run("sources", "add", "keepers", "--text-file", str(root / "keepers.txt"), "--kind", "register") == 0
    assert "kind register" in capsys.readouterr().out
    advice = advice_for("no_licence_types", issuers=["Example Harbour Authority"])
    register = [item for item in advice["add"] if item["register_as"] == "register"]
    assert register and register[0]["published_by"] == "Example Harbour Authority"


def test_a_source_registered_again_as_enforcement_states_no_obligations(live):  # noqa: F811
    client, _ = live
    client.post("/v1/sources", json={"key": "rule", "adapter": "local_text", "kind": "regulation",
                                     "locator": {"text": RULE}})
    client.post("/v1/scopes", json={"name": "harbour", "sources": ["rule"]})
    client.put("/v1/profiles/keeper", json={"attributes": {"roles": ["keeper"]}})
    release = _run(client, "harbour", ["keeper"])
    assert client.get(f"/v1/releases/{release}/blueprints/keeper").json()["coverage"]

    client.post("/v1/sources", json={"key": "rule", "adapter": "local_text", "kind": "enforcement",
                                     "locator": {"text": RULE}})
    release = _run(client, "harbour", ["keeper"])
    blueprint = client.get(f"/v1/releases/{release}/blueprints/keeper").json()
    assert blueprint["coverage"] == []  # the obligations read from it before went stale
    assert "no_duties" in {g["kind"] for g in blueprint["evidence_gaps"]}


def test_the_migration_admits_register_in_a_database_from_before(install):
    from app.clhear.l1.models import family_members, source_families, sources
    from app.clhear.runtime import engine

    def ddl(conn, name):
        return conn.exec_driver_sql(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{name}'").scalar()

    with engine().begin() as conn:
        # The sources table as 0018 left it: no 'register' in the kind check.
        old = ddl(conn, "sources").replace(",'register'", "").replace("CREATE TABLE sources", "CREATE TABLE sources__old", 1)
        conn.exec_driver_sql(old)
        conn.exec_driver_sql("DROP TABLE sources")
        conn.exec_driver_sql("ALTER TABLE sources__old RENAME TO sources")
        assert "'register'" not in ddl(conn, "sources")
        family = conn.execute(source_families.insert().values(key="declared", name="declared", scope_charter={})
                              .returning(source_families.c.id)).scalar_one()
        for key, kind in (("actions", "enforcement"), ("rule", "regulation")):
            sid = conn.execute(sources.insert().values(family_id=family, key=key, name=key, kind=kind)
                               .returning(sources.c.id)).scalar_one()
            conn.execute(family_members.insert().values(family_id=family, source_id=sid, relation="root",
                                                        tier="binding", status="active", added_via="manual"))

        importlib.import_module("migrations.m0044_register_source_kind").upgrade(conn)

        assert "'register'" in ddl(conn, "sources")
        conn.execute(sources.insert().values(family_id=family, key="keepers", name="keepers", kind="register"))
        assert conn.execute(sa.select(sa.func.count()).select_from(sources)).scalar() == 3
        tiers = dict(conn.execute(sa.select(sources.c.key, family_members.c.tier)
                                  .join(family_members, family_members.c.source_id == sources.c.id)).all())
        assert tiers == {"actions": "informative", "rule": "binding"}
        for child in ("source_versions", "family_members"):  # still pointing at sources, not at a copy
            assert "sources__" not in ddl(conn, child) and "REFERENCES" in ddl(conn, child).upper()
