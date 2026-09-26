# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""HLD v2 §5 "Watch": public change feed, digest, watchlists, profile watches.

* ``feed`` combines the three layers whose changes matter to a reader —
  L1 instrument versions (``change_events``), L2 obligation changes
  (``l2_change_events``) and L6 blueprint re-compositions (``clhear.l6.changed``
  outbox events) — into one list ordered by date, each entry carrying the
  effective date and where it came from (I7: change ≠ detection).
* ``digest`` narrows the feed to what a watcher follows: their instrument
  watchlist (L1) and their profile watches ("profiles like mine": any current
  blueprint composed for a watched profile, plus any obligation those
  blueprints satisfy).
* Atom rendering for feed readers — same rows, no separate store.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy.engine import Connection, Engine

from app.clhear.community_models import profile_watches
from app.clhear.db import get_engine
from app.clhear.derived_models import blueprints, l2_change_events, obligations, profiles
from app.clhear.l1.models import change_events, sources, watchlists
from app.clhear.models import events
from app.clhear.platform import record
from app.clhear.settings import get_settings

router = APIRouter(tags=["watch"])
WEB_DIR = Path(__file__).resolve().parent / "web"

FEED_LAYERS = ("L1", "L2", "L6")


def _iso(value) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value) if value else None


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _sort_key(entry: dict) -> str:
    return (entry.get("effective_date") or entry.get("detected_at") or "")[:19]


# --------------------------------------------------------------------------- feed


def _l1_entries(conn: Connection, *, since: date | None, source_keys: set[str] | None, limit: int) -> list[dict]:
    stmt = (
        sa.select(change_events, sources.c.key.label("source_key"), sources.c.short_name, sources.c.instrument, sources.c.jurisdiction)
        .join(sources, sources.c.id == change_events.c.source_id)
        .order_by(change_events.c.id.desc()).limit(limit)
    )
    if source_keys is not None:
        if not source_keys:
            return []
        stmt = stmt.where(sources.c.key.in_(sorted(source_keys)))
    if since is not None:
        stmt = stmt.where(sa.or_(change_events.c.effective_date >= since,
                                 sa.and_(change_events.c.effective_date.is_(None),
                                         change_events.c.detected_at >= datetime(since.year, since.month, since.day, tzinfo=timezone.utc))))
    out = []
    for r in conn.execute(stmt).mappings():
        out.append({
            "layer": "L1", "kind": r["kind"], "id": f"l1:{r['id']}", "subject": r["source_key"],
            "title": f"{r['instrument'] or r['short_name']}: {r['kind']} ({r['new_version']})",
            "jurisdiction": r["jurisdiction"], "effective_date": _iso(r["effective_date"]),
            "effective_date_basis": r["effective_date_basis"] or "none", "detected_at": _iso(r["detected_at"]),
            "clause_ids": list(r["clause_ids"] or []), "href": f"/l1#{r['source_key']}",
        })
    return out


def _l2_entries(conn: Connection, *, since: date | None, source_keys: set[str] | None,
                obligation_ids: set[str] | None, limit: int) -> list[dict]:
    stmt = (
        sa.select(l2_change_events, obligations.c.stable_id, obligations.c.title, obligations.c.jurisdiction)
        .join(obligations, obligations.c.id == l2_change_events.c.obligation_id)
        .order_by(l2_change_events.c.detected_at.desc(), l2_change_events.c.id.desc()).limit(limit)
    )
    conds = []
    if source_keys is not None:
        conds.append(l2_change_events.c.source_key.in_(sorted(source_keys)) if source_keys else sa.false())
    if obligation_ids is not None:
        conds.append(l2_change_events.c.obligation_id.in_(sorted(obligation_ids)) if obligation_ids else sa.false())
    if conds:
        stmt = stmt.where(sa.or_(*conds))
    if since is not None:
        stmt = stmt.where(sa.or_(l2_change_events.c.effective_date >= since,
                                 sa.func.date(l2_change_events.c.detected_at) >= since.isoformat()))
    out = []
    for r in conn.execute(stmt).mappings():
        out.append({
            "layer": "L2", "kind": r["kind"], "id": r["id"], "subject": r["stable_id"] or r["obligation_id"],
            "title": f"{r['title']}: {r['kind']}", "jurisdiction": r["jurisdiction"],
            "effective_date": _iso(r["effective_date"]), "effective_date_basis": r["effective_date_basis"] or "none",
            "detected_at": _iso(r["detected_at"]), "clause_ids": list(r["cause_clause_ids"] or []),
            "source": r["source_key"], "href": f"/l2#{r['obligation_id']}",
        })
    return out


def _l6_entries(conn: Connection, *, since: date | None, profile_ids: set[str] | None, limit: int) -> list[dict]:
    stmt = (
        sa.select(events).where(events.c.kind == "clhear.l6.changed")
        .order_by(events.c.id.desc()).limit(limit)
    )
    if since is not None:
        stmt = stmt.where(events.c.created_at >= datetime(since.year, since.month, since.day, tzinfo=timezone.utc))
    out = []
    for r in conn.execute(stmt).mappings():
        payload = _json(r["payload"], {})
        if profile_ids is not None and payload.get("profile_id") not in profile_ids:
            continue
        summary = payload.get("summary") or {}
        out.append({
            "layer": "L6", "kind": "recomposed", "id": f"l6:{r['event_id']}", "subject": r["subject_ref"],
            "title": f"Blueprint {r['subject_ref']} supersedes {payload.get('supersedes')} ({payload.get('cause') or 'lower layer changed'})",
            "jurisdiction": "", "effective_date": None, "effective_date_basis": "none",
            "detected_at": _iso(r["created_at"]), "profile_id": payload.get("profile_id"),
            "summary": summary, "href": f"/l6#{r['subject_ref']}",
        })
    return out


def feed(engine: Engine, *, since: date | None = None, layers=FEED_LAYERS, limit: int = 100) -> dict:
    """The public change feed: every change in L1 / L2 / L6 with its effective date."""
    wanted = {l.upper() for l in layers} or set(FEED_LAYERS)
    entries: list[dict] = []
    with engine.connect() as conn:
        if "L1" in wanted:
            entries += _l1_entries(conn, since=since, source_keys=None, limit=limit)
        if "L2" in wanted:
            entries += _l2_entries(conn, since=since, source_keys=None, obligation_ids=None, limit=limit)
        if "L6" in wanted:
            entries += _l6_entries(conn, since=since, profile_ids=None, limit=limit)
    entries.sort(key=_sort_key, reverse=True)
    entries = entries[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(), "since": _iso(since), "layers": sorted(wanted),
        "count": len(entries), "entries": entries,
        "counts": {l: sum(1 for e in entries if e["layer"] == l) for l in sorted(wanted)},
    }


def atom(feed_doc: dict, *, base_url: str, title: str = "CLHEAR change feed") -> str:
    """Atom 1.0 rendering of ``feed``: one entry per change, updated = effective date when known."""
    updated = feed_doc["generated_at"]
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{escape(title)}</title>",
        f'  <link href="{escape(base_url)}/watch/feed.atom" rel="self"/>',
        f'  <link href="{escape(base_url)}/watch"/>',
        f"  <id>{escape(base_url)}/watch/feed</id>",
        f"  <updated>{escape(updated)}</updated>",
    ]
    for e in feed_doc["entries"]:
        when = e.get("effective_date") or e.get("detected_at") or updated
        if len(when) == 10:
            when += "T00:00:00+00:00"
        summary = (f"{e['layer']} {e['kind']} · effective {e.get('effective_date') or 'unknown'} "
                   f"({e.get('effective_date_basis')}) · detected {e.get('detected_at')}")
        lines += [
            "  <entry>",
            f"    <id>{escape(base_url)}/watch/feed#{escape(str(e['id']))}</id>",
            f"    <title>[{e['layer']}] {escape(e['title'])}</title>",
            f'    <link href="{escape(base_url)}{escape(e["href"])}"/>',
            f"    <updated>{escape(when)}</updated>",
            f"    <category term=\"{e['layer']}\"/>",
            f"    <summary>{escape(summary)}</summary>",
            "  </entry>",
        ]
    lines.append("</feed>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- watchers


def watcher_id(request: Request, x_watcher_id: str | None) -> str:
    from app.clhear.accounts import current_user

    user = current_user(request)
    if user:
        return f"user:{user['id']}"
    if x_watcher_id:
        return f"app:{x_watcher_id.strip()}"
    raise HTTPException(status_code=401, detail="Sign in or send X-Watcher-Id to manage watches")


def watched_sources(conn: Connection, watcher: str) -> list[str]:
    return [r[0] for r in conn.execute(
        sa.select(watchlists.c.source_key).where(watchlists.c.watcher_id == watcher).where(watchlists.c.valid_to.is_(None))
        .order_by(watchlists.c.created_at))]


def watched_profiles(conn: Connection, watcher: str) -> list[dict]:
    rows = conn.execute(
        sa.select(profile_watches.c.profile_id, profile_watches.c.created_at, profiles.c.name, profiles.c.attributes)
        .join(profiles, profiles.c.id == profile_watches.c.profile_id, isouter=True)
        .where(profile_watches.c.watcher_id == watcher).where(profile_watches.c.valid_to.is_(None))
        .order_by(profile_watches.c.created_at)
    ).mappings().all()
    return [{"profile_id": r["profile_id"], "name": r["name"] or "", "since": _iso(r["created_at"]),
             "attributes": _json(r["attributes"], {})} for r in rows]


def watch_profile(engine: Engine, watcher: str, profile_id: str) -> dict:
    with engine.begin() as conn:
        if conn.execute(sa.select(profiles.c.id).where(profiles.c.id == profile_id)).first() is None:
            raise LookupError(profile_id)
        existing = conn.execute(
            sa.select(profile_watches.c.id).where(profile_watches.c.watcher_id == watcher).where(profile_watches.c.profile_id == profile_id)
        ).mappings().first()
        if existing:
            conn.execute(profile_watches.update().where(profile_watches.c.id == existing["id"]).values(valid_to=None))
        else:
            conn.execute(profile_watches.insert().values(id=record.new_uuid(), watcher_id=watcher, profile_id=profile_id))
    return {"watcher": watcher, "profile_id": profile_id, "watching": True}


def unwatch_profile(engine: Engine, watcher: str, profile_id: str) -> dict:
    with engine.begin() as conn:
        conn.execute(
            profile_watches.update().where(profile_watches.c.watcher_id == watcher).where(profile_watches.c.profile_id == profile_id)
            .where(profile_watches.c.valid_to.is_(None)).values(valid_to=datetime.now(timezone.utc))
        )
    return {"watcher": watcher, "profile_id": profile_id, "watching": False}


def _like_mine(conn: Connection, profile_ids: list[str]) -> tuple[set[str], set[str], list[dict]]:
    """Sources and obligations that bind the watched profiles' current blueprints."""
    if not profile_ids:
        return set(), set(), []
    rows = conn.execute(
        sa.select(blueprints.c.stable_id, blueprints.c.profile_id, blueprints.c.composition)
        .where(blueprints.c.profile_id.in_(profile_ids)).where(blueprints.c.status == "current")
    ).mappings().all()
    obligation_ids: set[str] = set()
    source_keys: set[str] = set()
    like = []
    for r in rows:
        comp = _json(r["composition"], {})
        mine: set[str] = set()
        for ob in comp.get("coverage") or []:
            oid = ob.get("obligation_id") if isinstance(ob, dict) else ob
            if oid:
                mine.add(str(oid))
        for item in comp.get("items") or []:
            mine.update(str(oid) for oid in item.get("obligations_satisfied") or [])
        obligation_ids |= mine
        like.append({"blueprint_id": r["stable_id"], "profile_id": r["profile_id"], "obligations": len(mine)})
    if obligation_ids:
        for (key,) in conn.execute(sa.select(obligations.c.source_key).where(obligations.c.id.in_(sorted(obligation_ids))).distinct()):
            source_keys.add(key)
    return source_keys, obligation_ids, like


def digest(engine: Engine, watcher: str, *, since: date | None = None, limit: int = 50) -> dict:
    """What changed for what this watcher follows — instruments and profiles like theirs."""
    with engine.connect() as conn:
        instruments = watched_sources(conn, watcher)
        my_profiles = watched_profiles(conn, watcher)
        like_sources, like_obligations, like = _like_mine(conn, [p["profile_id"] for p in my_profiles])
        source_keys = set(instruments) | like_sources
        entries = _l1_entries(conn, since=since, source_keys=source_keys, limit=limit)
        entries += _l2_entries(conn, since=since, source_keys=set(instruments), obligation_ids=like_obligations, limit=limit)
        entries += _l6_entries(conn, since=since, profile_ids={p["profile_id"] for p in my_profiles}, limit=limit)
    entries.sort(key=_sort_key, reverse=True)
    entries = entries[:limit]
    return {
        "watcher": watcher, "generated_at": datetime.now(timezone.utc).isoformat(), "since": _iso(since),
        "watching": {"instruments": instruments, "profiles": my_profiles, "profiles_like_mine": like},
        "count": len(entries), "entries": entries,
        "counts": {l: sum(1 for e in entries if e["layer"] == l) for l in FEED_LAYERS},
        "empty_reason": None if entries else (
            "nothing you watch has changed" if (instruments or my_profiles) else "watch an instrument or a profile to receive a digest"),
    }


# --------------------------------------------------------------------------- routes


class ProfileWatchBody(BaseModel):
    profile_id: str


@router.get("/watch", response_class=HTMLResponse, include_in_schema=False)
def watch_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "watch.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/watch/feed")
def public_feed(since: date | None = Query(default=None), layers: str = Query(default="L1,L2,L6"),
                limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return feed(get_engine(), since=since, layers=[l for l in layers.split(",") if l.strip()], limit=limit)


@router.get("/watch/feed.atom", include_in_schema=False)
def public_feed_atom(since: date | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500)) -> Response:
    doc = feed(get_engine(), since=since, limit=limit)
    return Response(atom(doc, base_url=get_settings().clhear_public_base_url.rstrip("/")), media_type="application/atom+xml")


@router.get("/watch/digest")
def my_digest(request: Request, since: date | None = Query(default=None), limit: int = Query(default=50, ge=1, le=200),
              x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    return digest(get_engine(), watcher_id(request, x_watcher_id), since=since, limit=limit)


@router.get("/watch/profiles")
def my_profile_watches(request: Request, x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    watcher = watcher_id(request, x_watcher_id)
    with get_engine().connect() as conn:
        return {"watcher": watcher, "profiles": watched_profiles(conn, watcher)}


@router.post("/watch/profiles", status_code=201)
def add_profile_watch(body: ProfileWatchBody, request: Request, x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    try:
        return watch_profile(get_engine(), watcher_id(request, x_watcher_id), body.profile_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown profile {body.profile_id}")


@router.post("/watch/profiles/{profile_id}/unwatch")
def remove_profile_watch(profile_id: str, request: Request, x_watcher_id: str | None = Header(default=None, alias="X-Watcher-Id")) -> dict:
    return unwatch_profile(get_engine(), watcher_id(request, x_watcher_id), profile_id)
