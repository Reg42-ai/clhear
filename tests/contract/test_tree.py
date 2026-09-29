# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
from pathlib import Path

from app.clhear.denylist import scan
from app.clhear.openapi_doc import SPEC, render


def test_denylist_is_clean():
    assert scan(Path.cwd()) == []


def test_openapi_file_matches_the_app():
    assert SPEC.read_text(encoding="utf-8") == render()


def test_no_regulatory_scope_is_shipped():
    assert not Path("app/clhear/l1/scopes.json").exists()
    registry = Path("app/clhear/l1/source_registry.py").read_text(encoding="utf-8")
    assert "S: list[dict] = []" in registry
    shipped = list(Path("scopes").glob("*")) if Path("scopes").is_dir() else []
    assert shipped == []


def test_the_schema_revision_is_the_latest_migration():
    """The release manifest reads ``schema_revision_to`` from the settings: it must name the newest migration."""
    from app.clhear.settings import get_settings

    latest = max(int(p.name[1:5]) for p in Path("migrations").glob("m[0-9][0-9][0-9][0-9]_*.py"))
    assert get_settings().clhear_schema_revision == f"{latest:04d}"
