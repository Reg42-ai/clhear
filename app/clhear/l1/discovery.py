# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Bounded, leased publisher-catalog traversal invoked only by L1 workers.

Checkpoints contain URLs, hashes and declared document metadata, never original
text. Aurora owns the frontier; artifacts stay in the restricted artifact store.
A new UTC day starts a new traversal; interrupted batches resume their old day.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
import os
from contextvars import ContextVar
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

import sqlalchemy as sa
from bs4 import BeautifulSoup

from app.clhear.l1 import permissions
from app.clhear.l1.models import Json, L1_SCHEMA

_cycle_date = ContextVar("l1_publisher_discovery_date", default=None)


@contextmanager
def bind_cycle_date(value):
    if value is not None and datetime.fromisoformat(value).date().isoformat() != value:
        raise ValueError("Discovery cycle date must be YYYY-MM-DD")
    token = _cycle_date.set(value)
    try:
        yield
    finally:
        _cycle_date.reset(token)


metadata = sa.MetaData(schema=L1_SCHEMA)
cycles = sa.Table("l1_discovery_cycles", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("publisher_id", sa.Text, nullable=False, index=True),
    sa.Column("profile_hash", sa.Text, nullable=False),
    sa.Column("cycle_date", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
pages = sa.Table("l1_discovery_pages", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("cycle_id", sa.Text, sa.ForeignKey(f"{L1_SCHEMA}.l1_discovery_cycles.id"), nullable=False, index=True),
    sa.Column("url", sa.Text, nullable=False), sa.Column("source_key", sa.Text, nullable=False),
    sa.Column("category", sa.Text, nullable=False), sa.Column("role", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, default="pending"),
    sa.Column("lease_token", sa.Text), sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("last_job_id", sa.Text), sa.Column("attempts", sa.Integer, nullable=False, default=0),
    sa.Column("result", Json, nullable=False, default=dict),
    sa.Column("checked_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("cycle_id", "url", name="discovery_cycle_url_unique"))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _insert_once(conn, table, values):
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    conn.execute(insert(table).values(**values).on_conflict_do_nothing())


# One catalog page may wait out publisher throttling (inventory._fetch_discovery
# sleeps up to THROTTLE_WAITS_S in total) before its checkpoint is written; the
# lease must outlive that, or the write fails with "checkpoint lease lost" and
# the whole cycle fails (19 Sep 2026, cycle-manual-l1-cycle-35453100897-1).
PAGE_LEASE = timedelta(minutes=6)


def _throttled(exc) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or (isinstance(status, int) and status >= 500) or "throttled" in str(exc).lower()


def _page(cycle_id, url, source_key, category, role):
    return dict(id=_hash([cycle_id, url]), cycle_id=cycle_id, url=url, source_key=source_key,
                category=category, role=role, status="pending", attempts=0, result={})


def _claim(engine, cycle_id, job_id):
    now = datetime.now(timezone.utc)
    eligible = sa.or_(pages.c.status == "pending",
        sa.and_(pages.c.status.in_(["failed", "permission_blocked"]), pages.c.last_job_id != job_id),
        sa.and_(pages.c.status == "leased", pages.c.lease_until < now))
    with engine.begin() as conn:
        query = sa.select(pages).where(pages.c.cycle_id == cycle_id, eligible).order_by(pages.c.attempts, pages.c.id).limit(1)
        if conn.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        row = conn.execute(query).mappings().first()
        if not row:
            return None
        token = str(uuid.uuid4())
        changed = conn.execute(pages.update().where(pages.c.id == row["id"], eligible).values(
            status="leased", lease_token=token, lease_until=now + PAGE_LEASE, last_job_id=job_id,
            attempts=pages.c.attempts + 1))
        return {**row, "lease_token": token} if changed.rowcount else None


def _terminal_resolution(conn, source_key, url):
    result = {"terminal": True, "entries": [], "links": [], "findings": []}
    if permissions.candidate_decision(conn, source_key, "acquire", canonical_url=url)["allowed"]:
        return "checked", result
    if permissions.decision(conn, source_key, "acquire").get("reason") == "not_approved":
        result["findings"].append({"code": "publisher_permission_denied",
            "detail": "The publisher explicitly denied acquisition; the operator exception does not apply."})
        return "permission_blocked", {**result, "publisher_denied": True}
    return "awaiting_exception_binding", result


def _close_without_fetch(conn, page, mode):
    if mode == "skip":
        return "checked", {"skipped": True, "out_of_scope": True, "entries": [], "links": [], "findings": []}
    if mode == "terminal":
        return _terminal_resolution(conn, page["source_key"], page["url"])
    raise ValueError("Unknown discovery settle mode")


def _settle_existing(engine, cycle_id, settle):
    """Close leftover leaf/off-book pages without spending the fetch budget.

    The live 19 Sep frontier already had pending rule leaves and notice pages.
    Claiming them one-by-one at 4 s + 429 backoff kept the rulebook cycle queued.
    """
    now = datetime.now(timezone.utc)
    eligible = sa.or_(
        pages.c.status.in_(["pending", "failed", "awaiting_exception_binding"]),
        sa.and_(pages.c.status == "leased", pages.c.lease_until < now),
    )
    with engine.begin() as conn:
        rows = list(conn.execute(sa.select(pages).where(pages.c.cycle_id == cycle_id, eligible)).mappings())
        for row in rows:
            mode = settle(row)
            if mode == "skip" or (mode == "terminal" and row["status"] != "awaiting_exception_binding"):
                status, result = _close_without_fetch(conn, row, mode)
                conn.execute(pages.update().where(pages.c.id == row["id"], eligible).values(
                    status=status, result=result, checked_at=now, lease_token=None, lease_until=None))


def run_batch(engine, store, *, publisher_id, profile, seeds, job_id, fetcher, classify, max_pages=100,
              cycle_date=None, decoder=None, decode_documents=False, settle=None):
    """Resume one publisher's UTC cycle. classify(url, parent) -> target or None.

    A target has url/source_key/category/role and optional entry. Catalog
    pagination retains its exact catalog permission key; documents never inherit
    that permission. Concurrent claims and checkpoint writes are token-fenced.
    """
    stamp = cycle_date or _cycle_date.get() or datetime.now(timezone.utc).date().isoformat()
    if datetime.fromisoformat(stamp).date().isoformat() != stamp:
        raise ValueError("cycle_date must be an ISO date")
    profile_hash = _hash({"profile": profile, "seeds": seeds})
    cycle_id = _hash([publisher_id, profile_hash, stamp])
    with engine.begin() as conn:
        _insert_once(conn, cycles, dict(id=cycle_id, publisher_id=publisher_id, profile_hash=profile_hash, cycle_date=stamp))
        for seed in seeds:
            _insert_once(conn, pages, _page(cycle_id, seed["url"], seed["source_key"], seed["category"], "collection"))
    if settle is not None:
        _settle_existing(engine, cycle_id, settle)
    for _ in range(max(1, min(int(max_pages), 10000))):
        page = _claim(engine, cycle_id, job_id)
        if not page:
            break
        result = {"findings": [], "entries": [], "links": []}
        state = "checked"
        mode = settle(page) if settle is not None else None
        if mode:
            with engine.begin() as conn:
                state, result = _close_without_fetch(conn, page, mode)
                changed = conn.execute(pages.update().where(
                    pages.c.id == page["id"], pages.c.lease_token == page["lease_token"],
                    pages.c.lease_until > datetime.now(timezone.utc)).values(
                    status=state, result=result, checked_at=datetime.now(timezone.utc),
                    lease_token=None, lease_until=None))
                if not changed.rowcount:
                    raise RuntimeError("Discovery checkpoint lease lost")
            continue
        with engine.connect() as conn:
            choices = {op: permissions.candidate_decision(conn, page["source_key"], op, canonical_url=page["url"])
                       for op in ("acquire", "store", "parse")}
        denied = [op for op, choice in choices.items() if not choice["allowed"]]
        if denied:
            state = "permission_blocked"
            result["findings"].append({"code": "discovery_permission_blocked", "detail": "Catalog/document acquisition requires reviewed permission evidence.", "operations": denied})
        else:
            try:
                body, origin = fetcher(page["url"])
                digest = hashlib.sha256(body).hexdigest()
                # Revocation while the request was in flight cannot authorize a
                # store/parse by an obsolete grant. Compare exact ledger IDs.
                with engine.connect() as conn:
                    current = {op: permissions.candidate_decision(conn, page["source_key"], op, canonical_url=page["url"])
                               for op in choices}
                if any(not current[op]["allowed"] or current[op]["permission_id"] != choices[op]["permission_id"] for op in choices):
                    raise PermissionError("Permission changed during acquisition")
                uri = store.put(f"restricted/_l1_inventory/{digest}.bin", body, "application/octet-stream")
                result["artifact"] = {"sha256": digest, "byte_count": len(body), "artifact_uri": uri,
                                      "permissions": {op: {k: v.get(k) for k in (
                                          "allowed", "permission_id", "authority_type", "exception_id", "binding_id", "release_eligible")}
                                          for op, v in choices.items()},
                                      "origin": origin, "publisher_checked_at": datetime.now(timezone.utc).isoformat() if origin == "live" else None}
                if origin != "live":
                    result["findings"].append({"code": "discovery_not_live", "detail": "Fixture evidence does not establish publisher freshness."})
                if decoder is not None and (page["role"] == "collection" or decode_documents):
                    decoded = decoder(body, page)
                    for field in ("entries", "links", "findings"):
                        result[field].extend(decoded.get(field, []))
                    result["catalog_metadata"] = decoded.get("catalog_metadata", {})
                elif body[:5] != b"%PDF-":
                    soup = BeautifulSoup(body, "html.parser")
                    area = soup.select_one("main") or soup.body or soup
                    if area.select("form select, [data-drupal-views-infinite-scroll-content-wrapper]"):
                        result["findings"].append({"code": "dynamic_enumeration_unverified", "detail": "Dynamic publisher filters still require explicit enumeration evidence."})
                    for link in area.find_all("a", href=True):
                        target = classify(urljoin(page["url"], str(link["href"])), page)
                        if target is None:
                            if "next" in (link.get("rel") or []):
                                result["findings"].append({"code": "unsupported_pagination", "detail": "Next page falls outside reviewed discovery boundaries."})
                            continue
                        result["links"].append({k: target[k] for k in ("url", "source_key", "category", "role", "terminal") if k in target})
                        if target.get("entry"):
                            result["entries"].append(target["entry"])
                    if page["role"] == "collection" and not result["links"]:
                        result["findings"].append({"code": "empty_discovery_index", "detail": "A collection yielded no supported documents or pagination; completeness is unverified."})
            except PermissionError:
                state = "permission_blocked"
                result = {"entries": [], "links": [], "findings": [{"code": "discovery_permission_changed", "detail": "Permission changed during acquisition; parsing and storage denied."}]}
            except Exception as exc:
                state = "failed"
                result = {**({"artifact": result["artifact"]} if result.get("artifact") else {}),
                          "entries": [], "links": [], "findings": [{"code": "discovery_failed", "detail": "Publisher discovery failed; the checkpoint remains retryable.", "error_type": type(exc).__name__}]}
                if _throttled(exc) and page["attempts"] < 5:
                    # A throttled or 5xx catalog page is not a discovery result. Keep
                    # it pending so this cycle retries it after the other pages,
                    # instead of planning imports from an incomplete inventory.
                    state = "pending"
                    result["findings"][-1].update(code="discovery_throttled", detail="Publisher throttled or failed transiently; the page stays pending in this cycle.")
        max_links = max(1, min(int(os.environ.get("CLHEAR_L1_DISCOVERY_MAX_DOCUMENTS", "10000")), 50000))
        if len(result["links"]) > max_links or len(result["entries"]) > max_links:
            result["links"] = result["links"][:max_links]
            result["entries"] = result["entries"][:max_links]
            result["findings"].append({"code": "discovery_document_limit", "detail": "Catalog page exceeds the configured expansion limit; complete enumeration requires a larger reviewed bound."})
        with engine.begin() as conn:
            changed = conn.execute(pages.update().where(pages.c.id == page["id"], pages.c.lease_token == page["lease_token"], pages.c.lease_until > datetime.now(timezone.utc)).values(
                status=state, result=result, checked_at=datetime.now(timezone.utc), lease_token=None, lease_until=None))
            if not changed.rowcount:
                raise RuntimeError("Discovery checkpoint lease lost")
            for link in result["links"]:
                row = _page(cycle_id, link["url"], link["source_key"], link["category"], link["role"])
                if link.get("terminal"):
                    # An enumerated leaf: never fetched by discovery. It still
                    # needs its permission binding before the import may run,
                    # so it waits for L0 exactly like a fetched page would; a
                    # publisher denial stays a denial and is never bound.
                    status, result_row = _terminal_resolution(conn, link["source_key"], link["url"])
                    row.update(status=status, result=result_row, checked_at=datetime.now(timezone.utc))
                _insert_once(conn, pages, row)
    from app.clhear.l1.finra_private_review import request_frontier_bindings
    request_frontier_bindings(engine, cycle_id)
    return read_cycle(engine, cycle_id)


def read_cycle(engine, cycle_id):
    with engine.connect() as conn:
        cycle = conn.execute(sa.select(cycles).where(cycles.c.id == cycle_id)).mappings().one()
        rows = list(conn.execute(sa.select(pages).where(pages.c.cycle_id == cycle_id).order_by(pages.c.url)).mappings())
    entries, findings, evidence, categories = {}, [], [], {}
    for row in rows:
        result = row["result"] or {}
        context = {"source_key": row["source_key"], "url": row["url"], "category": row["category"], "publisher_id": cycle["publisher_id"]}
        findings.extend({**f, **context} for f in result.get("findings", []))
        for entry in result.get("entries", []):
            entries[entry["key"]] = {**entries.get(entry["key"], {}), **entry}
        if result.get("artifact"):
            evidence.append({**context, **result["artifact"], "catalog_metadata": result.get("catalog_metadata", {})})
        state = categories.setdefault(row["category"], {"key": row["category"], "status": "checked", "documents": 0, "pending_pages": 0, "unresolved_pages": 0})
        if row["status"] != "checked" or result.get("findings"):
            state["status"] = "incomplete"
            state["unresolved_pages"] += 1
        if row["status"] in {"pending", "leased", "awaiting_exception_binding"}:
            state["pending_pages"] += 1
    pending = sum(row["status"] in {"pending", "leased", "awaiting_exception_binding"} for row in rows)
    if pending:
        findings.append({"code": "discovery_limit", "detail": "Persisted catalog frontier has remaining pages; another worker batch must resume it.", "pending_pages": pending, "publisher_id": cycle["publisher_id"]})
    for entry in entries.values():
        if entry.get("discovered_category") in categories:
            categories[entry["discovered_category"]]["documents"] += 1
    return entries, {"cycle_id": cycle_id, "cycle_date": cycle["cycle_date"], "publisher_id": cycle["publisher_id"],
                     "complete": bool(entries) and not findings and all(r["status"] == "checked" for r in rows),
                     "checked_at": min((p["publisher_checked_at"] for p in evidence if p.get("publisher_checked_at")), default=None),
                     "last_attempt_at": max((r["checked_at"].replace(tzinfo=timezone.utc).isoformat() for r in rows if r["checked_at"]), default=None),
                     "categories": list(categories.values()),
                     "pages": evidence, "findings": findings, "pending_pages": pending,
                     "denominator_known": False, "expected_documents": None}
