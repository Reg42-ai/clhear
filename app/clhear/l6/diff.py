# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L6 diff engine — what changes in a blueprint when any lower layer changes (HLD v2 §4.6).

``diff_compositions`` compares two compositions item by item and obligation
by obligation. ``recompose`` re-runs the composer for every current stored
blueprint after an L2 / L3 / L4 / L5 change: unchanged compositions are left
alone; changed ones are stored as a new ``BLU-`` (the old one is superseded,
never deleted) and ``clhear.l6.changed`` carries the diff summary.
"""
from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import blueprints
from app.clhear.l6 import composer
from app.clhear.platform.events import publish_layer_event

log = logging.getLogger("clhear.l6.diff")


def _chars(item: dict) -> dict[str, str]:
    return {c["key"]: c["value"] for c in item.get("characteristics") or [] if c.get("in_profile", True)}


def diff_compositions(before: dict, after: dict) -> dict:
    """Item / obligation level diff between two compositions."""
    items_a = {i["block_id"]: i for i in before.get("items") or []}
    items_b = {i["block_id"]: i for i in after.get("items") or []}
    added = [items_b[b] for b in sorted(set(items_b) - set(items_a))]
    removed = [items_a[b] for b in sorted(set(items_a) - set(items_b))]
    changed = []
    for bid in sorted(set(items_a) & set(items_b)):
        a, b = items_a[bid], items_b[bid]
        delta = {}
        if a["basis"] != b["basis"]:
            delta["basis"] = {"before": a["basis"], "after": b["basis"]}
        sa_, sb_ = set(a["obligations_satisfied"]), set(b["obligations_satisfied"])
        if sa_ != sb_:
            delta["obligations_satisfied"] = {"added": sorted(sb_ - sa_), "removed": sorted(sa_ - sb_)}
        ca, cb = _chars(a), _chars(b)
        keys = [k for k in sorted(set(ca) | set(cb)) if ca.get(k) != cb.get(k)]
        if keys:
            delta["characteristics"] = {k: {"before": ca.get(k), "after": cb.get(k)} for k in keys}
        if delta:
            changed.append({"block_id": bid, "name": b["name"], "kind": b["kind"], "delta": delta})
    cov_a = {c["obligation_id"]: c for c in before.get("coverage") or []}
    cov_b = {c["obligation_id"]: c for c in after.get("coverage") or []}
    ob_added = sorted(set(cov_b) - set(cov_a))
    ob_removed = sorted(set(cov_a) - set(cov_b))
    state_changes = []
    for oid in sorted(set(cov_a) & set(cov_b)):
        a, b = cov_a[oid], cov_b[oid]
        if a["state"] != b["state"] or sorted(a.get("satisfied_by") or []) != sorted(b.get("satisfied_by") or []):
            state_changes.append({"obligation_id": oid, "title": b.get("title"),
                                  "before": {"state": a["state"], "satisfied_by": a.get("satisfied_by") or []},
                                  "after": {"state": b["state"], "satisfied_by": b.get("satisfied_by") or []}})
    summary = {
        "items_added": len(added), "items_removed": len(removed), "items_changed": len(changed),
        "obligations_added": len(ob_added), "obligations_removed": len(ob_removed), "coverage_changes": len(state_changes),
        "gaps_before": (before.get("coverage_summary") or {}).get("gaps", 0),
        "gaps_after": (after.get("coverage_summary") or {}).get("gaps", 0),
        "minimal_before": (before.get("minimality") or {}).get("minimal"),
        "minimal_after": (after.get("minimality") or {}).get("minimal"),
    }
    return {
        "changed": any(v for k, v in summary.items() if k.startswith(("items_", "obligations_", "coverage_"))),
        "summary": summary,
        "items": {"added": [_brief(i) for i in added], "removed": [_brief(i) for i in removed], "changed": changed},
        "obligations": {"added": [_ob(cov_b[o]) for o in ob_added], "removed": [_ob(cov_a[o]) for o in ob_removed],
                        "coverage_changes": state_changes},
    }


def _brief(item: dict) -> dict:
    return {"block_id": item["block_id"], "name": item["name"], "kind": item["kind"], "basis": item["basis"],
            "obligations_satisfied": item["obligations_satisfied"]}


def _ob(c: dict) -> dict:
    return {"obligation_id": c["obligation_id"], "stable_id": c.get("stable_id"), "title": c.get("title"), "state": c["state"],
            "satisfied_by": c.get("satisfied_by") or []}


def diff_blueprints(conn: Connection, blueprint_id: str, against: str) -> dict:
    """Diff two stored blueprints (``BLU-`` ids); raises KeyError when unknown."""
    a = composer.get_blueprint(conn, blueprint_id)
    b = composer.get_blueprint(conn, against)
    if a is None:
        raise KeyError(blueprint_id)
    if b is None:
        raise KeyError(against)
    out = diff_compositions(a["composition"], b["composition"])
    out.update({"blueprint_id": blueprint_id, "against": against, "same_profile": a["fingerprint"] == b["fingerprint"],
                "releases": {"before": a["release"], "after": b["release"]}})
    return out


def what_changed_since(engine: Engine, blueprint_id: str) -> dict:
    """Recompose the stored blueprint's profile now (unstored) and diff against it."""
    with engine.connect() as conn:
        stored = composer.get_blueprint(conn, blueprint_id)
    if stored is None:
        raise KeyError(blueprint_id)
    fresh = composer.compose(engine, stored["profile"], release=stored["release"], log_request=False)
    out = diff_compositions(stored["composition"], fresh)
    out.update({"blueprint_id": blueprint_id, "against": "now", "status": stored["status"],
                "fresh_composition_hash": fresh["composition_hash"], "stored_composition_hash": stored["composition"].get("composition_hash")})
    return out


def recompose(engine: Engine, *, cause: str = "lower layer changed", publish: bool = True, limit: int | None = None) -> dict:
    """Re-run the composer for every current stored blueprint; store + publish the changed ones."""
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            sa.select(blueprints).where(blueprints.c.status == "current", blueprints.c.stable_id.isnot(None))
            .order_by(blueprints.c.id)).mappings()]
    if limit is not None:
        rows = rows[:limit]
    checked = unchanged = 0
    changes: list[dict] = []
    for row in rows:
        checked += 1
        profile = composer._json(row["profile"], {}) or {}
        before = composer._json(row["composition"], {}) or {}
        fresh = composer.compose(engine, profile, requested_by=f"l6.diff:{cause}", release=row["release"] or "", log_request=False)
        if fresh["composition_hash"] == before.get("composition_hash"):
            unchanged += 1
            continue
        delta = diff_compositions(before, fresh)
        with engine.begin() as conn:
            stored = composer.store_blueprint(conn, fresh, profile, requested_by=f"l6.diff:{cause}", release=row["release"] or "")
            new_id = stored["stable_id"]
            note = {"event": "recomposed", "cause": cause, "supersedes": row["stable_id"], "diff": delta["summary"]}
            current = conn.execute(sa.select(blueprints.c.id, blueprints.c.review).where(blueprints.c.stable_id == new_id)).first()
            review = composer._json(current.review, []) or []
            conn.execute(blueprints.update().where(blueprints.c.id == current.id).values(review=list(review) + [note]))
            if publish:
                publish_layer_event(
                    conn, layer="L6", event="changed", subject_ref=new_id, producer="l6.diff",
                    payload={"blueprint_id": new_id, "supersedes": row["stable_id"], "profile_id": row["profile_id"],
                             "cause": cause, "summary": delta["summary"]},
                )
        changes.append({"blueprint_id": new_id, "supersedes": row["stable_id"], "profile_id": row["profile_id"], "summary": delta["summary"]})
    return {"checked": checked, "unchanged": unchanged, "changed": len(changes), "changes": changes, "cause": cause}


def on_lower_layer_changed(engine: Engine, payload: dict, *, layer: str) -> dict:
    """Worker entry: any L2 / L4 / L5 change re-derives the stored blueprints (I1)."""
    cause = f"{layer} {payload.get('change') or payload.get('event') or 'changed'}".strip()
    try:
        return recompose(engine, cause=cause)
    except sa.exc.OperationalError:  # pre-m0014 database
        return {"checked": 0, "unchanged": 0, "changed": 0, "changes": [], "cause": cause}


def stored_diff_note(row_review) -> dict | None:
    review = row_review if isinstance(row_review, list) else json.loads(row_review or "[]")
    for note in reversed(review):
        if note.get("event") == "recomposed":
            return note
    return None
