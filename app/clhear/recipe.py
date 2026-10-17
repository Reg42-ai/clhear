# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""A recipe: the sources, the scope and the organisation profiles of a run, in one file.

``clhear recipe apply recipe.json`` registers the sources, writes the scope and
stores the profiles, so anyone can run the same scope again. A snapshot carries
the recipe of its release (``recipe.json``), without any text that was pasted
rather than fetched. The engine ships no recipe: each install brings its own.
"""
from __future__ import annotations

from sqlalchemy.engine import Engine

FORMAT = "clhear-recipe/1"
SOURCE_FIELDS = ("adapter", "locator", "name", "kind", "binding", "jurisdiction", "issuer", "reference", "licence")


class RecipeError(ValueError):
    """A recipe cannot be applied; the message says why."""


def build(scope: str, sources: list[dict], profiles: list[dict]) -> dict:
    """A recipe from stored registrations: sources as registered, the scope, the profiles."""
    out_sources = []
    for s in sources:
        entry = {"key": s["source_key"] if "source_key" in s else s["key"]}
        for field in SOURCE_FIELDS:
            value = s.get("publisher") if field == "issuer" and "publisher" in s else s.get(field)
            if value not in (None, "", {}):
                entry[field] = value
        if entry.get("adapter") == "local_text" and not (entry.get("locator") or {}).get("path"):
            entry["note"] = "The text was pasted when this run was made and is not in the recipe: supply it again."
        out_sources.append(entry)
    return {"format": FORMAT, "scope": {"name": scope, "sources": [s["key"] for s in out_sources]},
            "sources": out_sources,
            "profiles": [{"profile_id": p["profile_id"], "name": p.get("name") or p["profile_id"],
                          "attributes": p.get("attributes") or {}} for p in profiles]}


def apply(engine: Engine, recipe: dict) -> dict:
    """Register the recipe's sources, write its scope and store its profiles."""
    from app.clhear import hoststore
    from app.clhear.first_run import put_profile
    from app.clhear.l1 import scopes
    from app.clhear.l1.models import SOURCE_KINDS

    if recipe.get("format") != FORMAT:
        raise RecipeError(f"unknown recipe format {recipe.get('format')!r}; this engine reads {FORMAT}")
    scope = recipe.get("scope") or {}
    if not scope.get("name") or not scope.get("sources"):
        raise RecipeError("the recipe names no scope or no sources")
    declared = {s.get("key"): s for s in recipe.get("sources") or []}
    missing = [k for k in scope["sources"] if k not in declared]
    if missing:
        raise RecipeError("the scope names sources the recipe does not declare: " + ", ".join(missing))
    problems = []
    for key in scope["sources"]:
        s = declared[key]
        if s.get("kind", "guidance") not in SOURCE_KINDS:
            problems.append(f"{key}: kind must be one of {', '.join(SOURCE_KINDS)}")
        if not s.get("adapter"):
            problems.append(f"{key}: no adapter")
        if s.get("adapter") == "local_text" and not ((s.get("locator") or {}).get("path") or (s.get("locator") or {}).get("text")):
            problems.append(f"{key}: no text or path to read ({s.get('note') or 'supply it'})")
    if problems:
        raise RecipeError("; ".join(problems))
    registered = []
    for key in scope["sources"]:
        s = declared[key]
        body = {field: s[field] for field in SOURCE_FIELDS if field in s}
        body.setdefault("name", key)
        try:
            hoststore.upsert_source(engine, key, body)
        except ValueError as exc:
            raise RecipeError(f"{key}: {exc}") from exc
        registered.append(key)
    scopes.put(scope["name"], list(scope["sources"]), label=scope.get("label") or "")
    stored = {}
    for p in recipe.get("profiles") or []:
        result = put_profile(engine, p["profile_id"], name=p.get("name") or p["profile_id"],
                             attributes=p.get("attributes") or {})
        stored[p["profile_id"]] = result["validation"]
    return {"scope": scope["name"], "sources": registered, "profiles": stored}
