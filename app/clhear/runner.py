# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Execute one queued run. The HTTP handler only inserts the row."""
from __future__ import annotations

import json
import os
from typing import Callable

from sqlalchemy.engine import Engine

from app.clhear import hoststore, notify, scope_build
from app.clhear.l1 import scopes
from app.clhear.l6 import composer
from app.clhear.settings import get_settings


def _plain(value):
    return json.loads(json.dumps(value, default=str))


def _blueprint_for(engine: Engine, profile_id: str) -> dict | None:
    with engine.connect() as conn:
        rows = composer.list_blueprints(conn, profile_id=profile_id, status="current", limit=1)
        if not rows:
            return None
        stored = composer.get_blueprint(conn, rows[0]["blueprint_id"])
    if not stored:
        return None
    composition = stored.get("composition") or {}
    composition["blueprint_id"] = stored["blueprint_id"]
    composition["profile_id"] = profile_id
    return composition


def _live(engine: Engine, llm, profiles: list[dict]) -> dict:
    report = scope_build.build(
        engine, llm,
        profiles=[{"name": item.get("name") or "", "attributes": item.get("attributes") or {}} for item in profiles],
    )
    engine_ids = list(report.get("profiles") or [])
    stored = []
    blueprints = {}
    for item, engine_id in zip(profiles, engine_ids):
        composed = _blueprint_for(engine, engine_id)
        host_id = item.get("host_id") or engine_id
        if composed is not None:
            blueprints[host_id] = composed
        stored.append({"host_id": host_id, "engine_id": engine_id, "blueprint_id": (composed or {}).get("blueprint_id")})
    return {
        "profiles": stored,
        "blueprints": blueprints,
        "layers": report.get("layers") or {},
        "sources": report.get("sources") or {},
        "failed_sources": report.get("failed_sources") or [],
    }


def execute(engine: Engine, run: dict, *, sender: Callable[[str, bytes, dict], None] | None = None) -> dict:
    """Run scope_build for a live provider, or the offline sample when the provider is fake."""
    os.environ[scopes.SCOPE_ENV] = run["scope"]
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
                          "sources": built.get("sources") or {}, "failed_sources": built.get("failed_sources") or []})
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
