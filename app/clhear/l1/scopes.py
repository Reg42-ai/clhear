# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Named source scopes stored as files the consumer writes.

``CLHEAR_SOURCE_SCOPE`` names one file in ``CLHEAR_SCOPES_DIR`` (default
``./scopes``). A scope is a set of source keys. It is not derived content,
and this package does not ship one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

SCOPE_ENV = "CLHEAR_SOURCE_SCOPE"
SCOPES_DIR_ENV = "CLHEAR_SCOPES_DIR"


def directory() -> Path:
    return Path(os.environ.get(SCOPES_DIR_ENV, "scopes"))


def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a mapping")
    name = data.get("name") or path.stem
    sources = list(data.get("sources") or data.get("source_keys") or [])
    return {
        "name": name,
        "label": data.get("label") or name,
        "sources": sources,
        "roles": data.get("roles") or {},
        "imports": data.get("imports") or {},
    }


def _all() -> dict:
    root = directory()
    found: dict[str, dict] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.iterdir()):
        if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
            continue
        scope = _read(path)
        found[scope["name"]] = scope
    return found


def names() -> list[str]:
    return sorted(_all())


def get(name: str) -> dict:
    scopes = _all()
    if name not in scopes:
        known = ", ".join(sorted(scopes)) or "(none)"
        raise KeyError(f"Unknown source scope {name!r}; known: {known}")
    return scopes[name]


def put(name: str, sources: list[str], *, label: str = "", roles: dict | None = None) -> dict:
    """Write one scope file. The consumer chooses the source keys."""
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "label": label or name, "sources": list(sources), "roles": roles or {}}
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return body


def active_name() -> str:
    return os.environ.get(SCOPE_ENV, "").strip()


def active() -> dict | None:
    """The scope named by the environment, or None when that file is absent.

    A missing file is not an error here: migrations and filters run before a
    host has written the scope. ``get`` still raises for a name the caller
    asked to load.
    """
    name = active_name()
    if not name:
        return None
    try:
        return get(name)
    except KeyError:
        return None


def source_keys(name: str | None = None) -> tuple[str, ...]:
    scope = get(name) if name else active()
    return tuple(scope["sources"]) if scope else ()


def role(role_name: str, name: str | None = None) -> tuple[str, ...]:
    scope = get(name) if name else active()
    return tuple((scope or {}).get("roles", {}).get(role_name, ()))


def in_scope(source_key: str) -> bool:
    scope = active()
    return scope is None or source_key in scope["sources"]


def keys() -> frozenset[str] | None:
    """Source keys of the active scope, or None when no scope is active."""
    scope = active()
    return frozenset(scope["sources"]) if scope else None


def limiting(column, explicit: str | None = None):
    """A WHERE clause for one source key, or for every key of the active scope.

    None means the caller named no key and no scope is active, so the query
    stays unfiltered. An explicit key wins over the scope.
    """
    if explicit:
        return column == explicit
    chosen = keys()
    if not chosen:
        return None
    return column.in_(sorted(chosen))
