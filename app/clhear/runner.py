# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Execute one queued run. The HTTP handler only inserts the row."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Callable

from sqlalchemy.engine import Engine

from app.clhear import hoststore, notify, scope_build
from app.clhear.l1 import scopes
from app.clhear.settings import get_settings


def _plain(value):
    return json.loads(json.dumps(value, default=str))


def _check_profiles(engine: Engine, profiles: list[dict]) -> None:
    """Refuse a run for a profile that cannot be read, before any model is called."""
    from app.clhear.l4.validate import validate

    problems = []
    for item in profiles:
        result = validate(engine, item.get("attributes") or {})
        if result["errors"]:
            problems.append(f"{item.get('host_id')}: " + "; ".join(e["message"] for e in result["errors"]))
    if problems:
        raise ValueError("Invalid profile: " + " | ".join(problems))


def _live(engine: Engine, llm, profiles: list[dict]) -> dict:
    _check_profiles(engine, profiles)
    report = scope_build.build(
        engine, llm,
        profiles=[{"name": item.get("name") or "", "attributes": item.get("attributes") or {}} for item in profiles],
    )
    engine_ids = list(report.get("profiles") or [])
    checks = {c["id"]: c for c in report.get("profile_checks") or []}
    compositions = report.get("compositions") or {}
    stored = []
    blueprints = {}
    for item, engine_id in zip(profiles, engine_ids):
        host_id = item.get("host_id") or engine_id
        composed = compositions.get(engine_id)
        if composed is not None:
            composed = {**composed, "profile_id": host_id,
                        "profile_warnings": (checks.get(engine_id) or {}).get("warnings") or []}
            blueprints[host_id] = composed
        stored.append({"host_id": host_id, "engine_id": engine_id, "blueprint_id": (composed or {}).get("blueprint_id")})
    return {
        "profiles": stored,
        "blueprints": blueprints,
        "layers": report.get("layers") or {},
        "sources": report.get("sources") or {},
        "failed_sources": report.get("failed_sources") or [],
        "lineage": report.get("lineage") or {},
    }


def execute(engine: Engine, run: dict, *, sender: Callable[[str, bytes, dict], None] | None = None) -> dict:
    """Run scope_build for a live provider, or the offline sample when the provider is fake."""
    previous_scope = os.environ.get(scopes.SCOPE_ENV)
    os.environ[scopes.SCOPE_ENV] = run["scope"]
    try:
        with _one_build_at_a_time(engine):
            return _execute(engine, run, sender=sender)
    finally:
        if previous_scope is None:
            os.environ.pop(scopes.SCOPE_ENV, None)
        else:
            os.environ[scopes.SCOPE_ENV] = previous_scope


@contextmanager
def _one_build_at_a_time(engine: Engine):
    """Layer builds share the derived tables: two workers take turns (PostgreSQL advisory lock)."""
    if engine.dialect.name != "postgresql":
        yield
        return
    import sqlalchemy as sa

    with engine.connect() as conn:
        conn.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": 0x434C4852554E})
        try:
            yield
        finally:
            conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": 0x434C4852554E})


def _execute(engine: Engine, run: dict, *, sender: Callable[[str, bytes, dict], None] | None = None) -> dict:
    profiles = []
    for profile_id in run.get("profiles") or []:
        row = hoststore.get_profile(engine, profile_id)
        if row is None:
            raise KeyError(f"unknown profile {profile_id}")
        attributes = row["attributes"] if isinstance(row["attributes"], dict) else json.loads(row["attributes"] or "{}")
        profiles.append({"host_id": profile_id, "name": row.get("name") or "", "attributes": attributes})
    notify.emit(engine, "run.started", {"run_id": run["run_id"], "scope": run["scope"]}, sender=sender)
    hoststore.append_log(engine, run["run_id"], "run.started")
    provider = (get_settings().clhear_llm_provider or "").lower()
    try:
        llm = scope_build._router(engine, allow_fake=provider == "fake")
        if provider == "fake":
            from app.clhear.sample import derive

            built = derive(engine, llm, profiles)
        else:
            built = _live(engine, llm, profiles)
        for item in built["profiles"]:
            if item.get("engine_id"):
                hoststore.set_profile_engine_id(engine, item["host_id"], item["engine_id"])
        payload = _plain({"profiles": built["blueprints"], "layers": built["layers"],
                          "sources": built.get("sources") or {}, "failed_sources": built.get("failed_sources") or [],
                          "lineage": built.get("lineage") or {}})
        release = hoststore.save_release(engine, scope=run["scope"], run_id=run["run_id"], blueprints=payload)
        finished = hoststore.finish_run(engine, run["run_id"], status="succeeded", release_id=release["id"])
        notify.emit(engine, "run.finished", {"run_id": run["run_id"], "release_id": release["id"]}, sender=sender)
        notify.emit(engine, "release.published", {"run_id": run["run_id"], "release_id": release["id"]}, sender=sender)
        for host_id, composition in built["blueprints"].items():
            notify.emit(engine, "blueprint.changed", {
                "run_id": run["run_id"],
                "release_id": release["id"],
                "profile_id": host_id,
                "blueprint_id": composition.get("blueprint_id"),
            }, sender=sender)
        return {"run": finished, "release": release}
    except Exception as exc:
        hoststore.finish_run(engine, run["run_id"], status="failed", error=str(exc))
        notify.emit(engine, "run.failed", {"run_id": run["run_id"], "error": str(exc)[:500]}, sender=sender)
        raise


def work_once(engine: Engine | None = None, *, sender: Callable[[str, bytes, dict], None] | None = None) -> dict | None:
    handle = engine
    if handle is None:
        from app.clhear.runtime import engine as open_engine

        handle = open_engine()
    run = hoststore.claim_run(handle)
    if run is None:
        return None
    return execute(handle, run, sender=sender)
