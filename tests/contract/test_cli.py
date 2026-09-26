# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.clhear.cli import main


@pytest.fixture()
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'clhear.db'}")
    monkeypatch.setenv("CLHEAR_SCOPES_DIR", str(tmp_path / "scopes"))
    monkeypatch.setenv("CLHEAR_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("CLHEAR_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("CLHEAR_SOURCE_SCOPE", raising=False)
    from app.clhear.runtime import reset

    reset()
    return tmp_path


def test_doctor_blocks_a_live_run_without_a_provider(offline, capsys):
    assert main(["doctor"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["live_run"] == "blocked"
    assert payload["database"] == "ok"


def test_init_doctor_and_quickstart_write_sample_layers(offline, monkeypatch, capsys):
    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "fake")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    assert main(["init"]) == 0
    assert Path(capsys.readouterr().out.strip()).is_dir()
    assert main(["doctor"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["provider"] == "fake"
    assert doctor["live_run"] == "blocked"
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "v0.1.0"
    assert main(["quickstart"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["items"] >= 1
    assert summary["coverage"] >= 1
    listed = list(Path(offline, "scopes").glob("*.yaml"))
    assert listed
    assert "example-scope" in listed[0].read_text(encoding="utf-8")


def test_fake_provider_is_refused_for_a_live_router(offline, monkeypatch):
    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "fake")
    from app.clhear.settings import get_settings

    get_settings.cache_clear()
    from app.clhear import scope_build
    from app.clhear.runtime import engine

    handle = engine()
    with pytest.raises(RuntimeError, match="anthropic, openai_compatible, or bedrock"):
        scope_build._router(handle)
    router = scope_build._router(handle, allow_fake=True)
    assert "fake" in router.providers
