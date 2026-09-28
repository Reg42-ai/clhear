# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Worker-owned, immutable L1 scope and reconciliation evidence.

The registry is a declared minimum, never an independent publisher inventory.
Publisher collection indexes are discovery inputs, not regulatory documents. This
module has no CLI, request-time ingestion, credential discovery or permission
shortcut. Workers call ``run_inventory_audit``; HTTP readers only call the two
read functions. Audit outputs contain metadata and finding codes, never text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import Counter, deque
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import sqlalchemy as sa
from bs4 import BeautifulSoup

from app.clhear.l1 import permissions
from app.clhear.l1.public import nodes_internal_select
from app.clhear.l1.models import BigId, Json, L1_SCHEMA, clauses, doc_nodes, source_versions, sources
from app.clhear.models import runs

log = logging.getLogger("clhear.l1.inventory")
# Successive waits after a 429 on one catalog page: 150 s in total, well inside
# discovery.PAGE_LEASE (6 min) including the fetches themselves.
THROTTLE_WAITS_S = (30.0, 60.0, 60.0)
SCOPE_VERSION = "2026-09-20.1"
SCOPES = frozenset({"registered", "all_publishers"})
EXPECTED_EDITIONS = {
    "iso/27001-2022": "ISO/IEC 27001:2022",
    "iso/27001-2022-amd1-2024": "ISO/IEC 27001:2022/Amd 1:2024",
    "aicpa/soc2-tsc": "Trust Services Criteria 2017; revised points of focus 2022",
}
metadata = sa.MetaData(schema=L1_SCHEMA)
inventory_snapshots = sa.Table(
    "l1_inventory_snapshots", metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column("scope", sa.Text, nullable=False, index=True),
    sa.Column("scope_version", sa.Text, nullable=False),
    sa.Column("inventory_hash", sa.Text, nullable=False, unique=True),
    sa.Column("definition", Json, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
inventory_audits = sa.Table(
    "l1_inventory_audits", metadata,
    sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
    sa.Column("inventory_id", sa.Uuid(as_uuid=False), sa.ForeignKey(f"{L1_SCHEMA}.l1_inventory_snapshots.id"), nullable=False),
    sa.Column("scope", sa.Text, nullable=False, index=True),
    sa.Column("job_id", sa.Text, nullable=False, index=True),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("summary", Json, nullable=False),
)
inventory_reviews = sa.Table(
    "l1_inventory_reviews", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("inventory_hash", sa.Text, nullable=False, index=True),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("approved", sa.Boolean, nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)
artifact_reviews = sa.Table(
    "l1_artifact_reviews", metadata,
    sa.Column("id", BigId, sa.Identity(), primary_key=True),
    sa.Column("source_key", sa.Text, nullable=False, index=True),
    sa.Column("content_hash", sa.Text, nullable=False, index=True),
    sa.Column("publisher_edition", sa.Text, nullable=False),
    sa.Column("canonical_url", sa.Text, nullable=False),
    sa.Column("coverage", sa.Text, nullable=False),
    sa.Column("evidence_ref", sa.Text, nullable=False),
    sa.Column("approved_by", sa.Text, nullable=False),
    sa.Column("approved", sa.Boolean, nullable=False),
    sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("coverage in ('full','preview','excerpt')", name="l1_artifact_coverage_check"),
)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value) -> str:
    return _hash(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _iso(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _recent(value, now, *, hours=24):
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return at.tzinfo is not None and now - timedelta(hours=hours) <= at <= now + timedelta(minutes=5)
    except (AttributeError, TypeError, ValueError):
        return False


def _scope(scope):
    if scope not in SCOPES:
        raise ValueError("scope must be all_publishers or registered")
    return "registered" if scope == "all_publishers" else scope


def _finding(code, detail, **extra):
    return {"code": code, "detail": detail, **extra}


def _available(engine) -> bool:
    with engine.connect() as conn:
        schema = L1_SCHEMA if engine.dialect.name == "postgresql" else None
        return sa.inspect(conn).has_table(inventory_audits.name, schema=schema)


def _declared_entries(scope):
    scope = _scope(scope)
    from app.clhear.l1.source_registry import S, source_role
    entries = {e["key"]: dict(e) for e in S if source_role(e["key"]) == "document"}
    # Include real starter declarations outside S, without fetching them.
    from app.clhear.l1.fleet import fleet_plan
    for _, adapter in fleet_plan():
        meta = adapter.meta()
        if meta.source_key not in entries and source_role(meta.source_key) == "document":
            entries[meta.source_key] = {
                "key": meta.source_key, "name": meta.name, "canonical_url": meta.canonical_url,
                "adapter": meta.adapter, "license": meta.license, "family": meta.family_key,
                "issuer": meta.issuer, "kind": meta.kind, "jurisdiction": meta.jurisdiction,
            }
    return entries


def _fetch_discovery(url):
    """Bounded official discovery; no redirects, shared public cache or fallback.

    Replay reads existing fixtures only. It cannot establish publisher freshness.
    Bodies are stored by the permission-gated caller under the restricted prefix.
    """
    from app.clhear.l1 import http
    if os.environ.get("CLHEAR_HTTP_MODE", "replay") == "replay":
        path = http._fixture_path(url)
        if not path.exists():
            raise http.FixtureMissing("Discovery fixture unavailable")
        return http._read_fixture(path), "fixture"
    import httpx
    # Same publisher courtesy as document fetches: paced per host, and a 429
    # waits for Retry-After instead of burning the page's retry budget.
    for attempt in range(len(THROTTLE_WAITS_S) + 1):
        http._pace(url)
        with httpx.stream("GET", url, headers={"User-Agent": http.USER_AGENT}, timeout=20, follow_redirects=False) as response:
            if response.is_redirect:
                raise ValueError("Publisher redirect requires an independently validated official discovery URL")
            if response.status_code == 429 and attempt < len(THROTTLE_WAITS_S):
                # Bounded so the page's discovery lease (discovery.PAGE_LEASE)
                # outlives the waits; a page still throttled after them stays
                # pending and is retried later in the cycle.
                pause = min(max(http._retry_after_seconds(response) or 0.0, THROTTLE_WAITS_S[attempt]), THROTTLE_WAITS_S[-1])
                log.warning("discovery throttled by %s; waiting %.0fs", urlparse(url).hostname, pause)
                time.sleep(pause)
                continue
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                limit = max(1, min(int(os.environ.get("CLHEAR_L1_DISCOVERY_MAX_BYTES", str(16 * 1024 * 1024))), 64 * 1024 * 1024))
                if len(body) > limit:
                    raise ValueError("Discovery page exceeds the configured bounded byte limit")
            if not body:
                raise ValueError("Publisher returned an empty discovery page")
            return bytes(body), "live"
    raise RuntimeError("Publisher discovery stayed throttled")


def _discover_publishers(engine, store, job_id):
    from app.clhear.l1 import publishers
    from app.clhear.l1.catalogs import discover_catalog
    entries, reports = {}, []
    for profile in publishers.publisher_profiles():
        docs, report = discover_catalog(engine, store, profile, job_id=job_id, fetcher=_fetch_discovery)
        entries.update(docs)
        reports.append(report)
    return entries, {"complete": all(r["complete"] for r in reports),
                     "checked_at": min((r.get("checked_at") for r in reports if r.get("checked_at")), default=None),
                     "pending_pages": sum(r.get("pending_pages", 0) for r in reports),
                     "publishers": reports, "categories": [c for r in reports for c in r.get("categories", [])],
                     "pages": [p for r in reports for p in r.get("pages", [])],
                     "findings": [f for r in reports for f in r.get("findings", [])]}


def _latest(engine, scope):
    with engine.connect() as conn:
        row = conn.execute(sa.select(inventory_audits).where(inventory_audits.c.scope == scope)
                           .order_by(inventory_audits.c.finished_at.desc(), inventory_audits.c.id.desc()).limit(1)).mappings().first()
    return dict(row) if row else None


def planned_entries(engine, scope="registered", adapter_key=None, *, audit_id=None):
    """Discovered supported documents for the existing fleet adapter factory."""
    scope = _scope(scope)
    if not _available(engine):
        return []
    if audit_id is None:
        prior = _latest(engine, scope)
    else:
        with engine.connect() as conn:
            prior = conn.execute(sa.select(inventory_audits).where(inventory_audits.c.id == audit_id,
                                inventory_audits.c.scope == scope)).mappings().first()
        if prior is None:
            raise ValueError("Frozen discovery audit is missing or belongs to another scope")
    if not prior:
        return []
    with engine.connect() as conn:
        definition = conn.execute(sa.select(inventory_snapshots.c.definition)
                                  .where(inventory_snapshots.c.id == prior["inventory_id"])).scalar_one()
    return [entry for entry in definition["entries"] if entry.get("discovered_category")
            and entry.get("source_role", "document") == "document"
            and (adapter_key is None or entry.get("adapter") == adapter_key)]


def record_scope_review(engine, inventory_hash, evidence_ref, approved_by, approved):
    """Append reviewed external enumeration evidence from trusted worker code.

    This never grants source permissions. A review binds the exact frozen
    finite list, scope version and category boundaries. Revocation overrides an
    older approval. No reviews are seeded or inferred from a successful crawl.
    An Engine owns its transaction; a Connection joins the caller's transaction.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", inventory_hash or ""):
        raise ValueError("inventory_hash must identify one frozen SHA-256 inventory")
    if type(approved) is not bool or not isinstance(evidence_ref, str) or not evidence_ref.strip() or not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("Explicit approval, reviewer and external enumeration evidence are required")
    with (engine.begin() if isinstance(engine, sa.engine.Engine) else nullcontext(engine)) as conn:
        if conn.execute(sa.select(inventory_snapshots.c.id).where(inventory_snapshots.c.inventory_hash == inventory_hash)).first() is None:
            raise ValueError("Review must reference an existing frozen inventory")
        review = conn.execute(inventory_reviews.insert().values(inventory_hash=inventory_hash, evidence_ref=evidence_ref.strip(),
                              approved_by=approved_by.strip(), approved=approved).returning(inventory_reviews)).mappings().one()
    return {**dict(review), "reviewed_at": _iso(review["reviewed_at"])}


def _review(conn, digest):
    row = conn.execute(sa.select(inventory_reviews).where(inventory_reviews.c.inventory_hash == digest)
                       .order_by(inventory_reviews.c.id.desc()).limit(1)).mappings().first()
    return {**dict(row), "reviewed_at": _iso(row["reviewed_at"])} if row else None


def record_artifact_review(engine, source_key, content_hash, publisher_edition, canonical_url,
                           coverage="full", evidence_ref=None, approved_by=None, approved=False):
    """Record human-reviewed artifact identity from trusted worker code only.

    Exact acquired bytes, edition, publisher reference and extent must all be
    reviewed. A full approval is neither an acquisition/display permission nor
    a parser pass. Preview/excerpt evidence can never stand for a full document.
    An uploaded filename or a successful sign-in is not identity evidence.
    An Engine owns its transaction; a Connection joins the caller's transaction.
    """
    if not isinstance(source_key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}", source_key):
        raise ValueError("An exact source key is required")
    if not re.fullmatch(r"[a-f0-9]{64}", content_hash or ""):
        raise ValueError("content_hash must identify the acquired artifact set")
    if (type(approved) is not bool or coverage not in {"full", "preview", "excerpt"}
            or any(not isinstance(v, str) or not v.strip() for v in (publisher_edition, canonical_url, evidence_ref, approved_by))):
        raise ValueError("Explicit edition, coverage, approval, reviewer and evidence are required")
    parsed = urlparse(canonical_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("An HTTPS publisher canonical reference is required")
    with (engine.begin() if isinstance(engine, sa.engine.Engine) else nullcontext(engine)) as conn:
        row = conn.execute(artifact_reviews.insert().values(source_key=source_key, content_hash=content_hash,
                           publisher_edition=publisher_edition.strip(), canonical_url=canonical_url.strip(), coverage=coverage,
                           evidence_ref=evidence_ref.strip(), approved_by=approved_by.strip(), approved=approved)
                           .returning(artifact_reviews)).mappings().one()
    return {**dict(row), "reviewed_at": _iso(row["reviewed_at"])}


def _artifact_review(conn, source_key, content_hash):
    row = conn.execute(sa.select(artifact_reviews).where(artifact_reviews.c.source_key == source_key,
                       artifact_reviews.c.content_hash == content_hash).order_by(artifact_reviews.c.id.desc()).limit(1)).mappings().first()
    return {**dict(row), "reviewed_at": _iso(row["reviewed_at"])} if row else None


def _store_key(store, uri):
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if parsed.netloc != getattr(store, "bucket", None):
            raise ValueError("Artifact belongs to a different store")
        return unquote(parsed.path.lstrip("/"))
    if parsed.scheme == "file" and not parsed.netloc and hasattr(store, "base_dir"):
        return str(Path(unquote(parsed.path)).resolve().relative_to(store.base_dir))
    raise ValueError("Artifact URI is outside the configured store")


_NODE_FIELDS = ("id", "parent_id", "seq", "depth", "node_type", "ref", "label", "heading", "raw_text", "source_fragment", "source_locator", "text_hash")
_CLAUSE_FIELDS = ("id", "doc_node_id", "ref", "ordering", "text", "text_hash", "span_start", "span_end")


def _projection_digest(nodes, clause_rows):
    return _digest({"nodes": [{k: n[k] for k in _NODE_FIELDS} for n in nodes],
                    "clauses": [{k: c[k] for k in _CLAUSE_FIELDS} for c in clause_rows]})


def _artifact_check_matches(check, key, manifest_parts):
    if not isinstance(check, dict) or check.get("schema") != "clhear.authorized-artifact-check.v1":
        return False
    method = check.get("method")
    live = method == "publisher_url_read" and check.get("publisher_check_performed") is True
    stored = method == "authorized_artifact_store_read" and check.get("publisher_check_performed") is False
    if not (stored or live):
        return False
    return (check.get("source_key") == key and manifest_parts
            and isinstance(check.get("checked_at"), str) and check.get("artifacts") == manifest_parts)


def _audit_source(conn, store, entry, now):
    key = entry["key"]
    findings = []
    out = {"source_key": key, "name": entry["name"], "canonical_url": entry.get("canonical_url", ""),
           "expected_edition": EXPECTED_EDITIONS.get(key), "source_version_id": None, "version_label": None,
           "content_hash": None, "ingested_at": None, "publisher_checked_at": None, "artifact_checked_at": None,
           "freshness_basis": "reviewed_immutable_artifact" if entry.get("adapter") == "restricted_file" else "publisher",
           "permissions": {}, "candidate_permissions": {},
           "release_eligible": False, "technical_verified": False,
           "artifacts": [], "findings": findings, "verified": False, "node_count": 0, "clause_count": 0}
    if permissions.required_for(entry):
        out["permissions"] = {op: permissions.decision(conn, key, op, now=now)
                              for op in ("acquire", "store", "parse", "display_internal", "display_public", "infer", "embed", "derive", "translate")}
        for op in ("acquire", "store", "parse"):
            choice = out["permissions"][op]
            if not choice["allowed"]:
                code = "permission_unverified" if choice["reason"] in {"missing_permission", "not_yet_valid"} else "permission_blocked"
                findings.append(_finding(code, "Required operation has no current approval.", operation=op, reason=choice["reason"]))
        out["candidate_permissions"] = {op: permissions.candidate_decision(conn, key, op, now=now,
                                                                          canonical_url=entry.get("canonical_url", ""))
                                        for op in ("acquire", "store", "parse")}
    source = conn.execute(sa.select(sources).where(sources.c.key == key)).mappings().first()
    from app.clhear.l1.origin import is_test_source
    if source and is_test_source(source):
        findings.append(_finding("test_origin_not_publisher_evidence", "The stored row has explicit test origin; a genuine publisher artifact is still required for this expected source."))
        return _source_status(out)
    versions = list(conn.execute(sa.select(source_versions).where(source_versions.c.source_id == source["id"], source_versions.c.status == "in_force")
                                .order_by(source_versions.c.id.desc())).mappings()) if source else []
    if not versions:
        findings.append(_finding("awaiting_artifact", "No current imported document exists for this expected source."))
        return _source_status(out)
    if len(versions) != 1:
        findings.append(_finding("duplicate_current_versions", "Several versions are marked current; select the publisher state explicitly.", version_ids=[v["id"] for v in versions]))
    version = versions[0]
    out.update(source_version_id=version["id"], version_label=version["version_label"], content_hash=version["content_hash"], ingested_at=_iso(version["retrieved_at"]))
    artifact_review = _artifact_review(conn, key, version["content_hash"])
    out["artifact_review"] = artifact_review
    out["node_count"] = conn.execute(sa.select(sa.func.count()).select_from(doc_nodes).where(doc_nodes.c.source_version_id == version["id"])).scalar_one()
    out["clause_count"] = conn.execute(sa.select(sa.func.count()).select_from(clauses).where(clauses.c.source_version_id == version["id"])).scalar_one()
    ledger = list(conn.execute(sa.select(runs.c.id, runs.c.outputs).where(runs.c.inputs["source"].as_string() == key)
                              .order_by(runs.c.created_at.desc(), runs.c.id.desc())).mappings())
    matched = [row for row in ledger if (row["outputs"] or {}).get("source_version_id") == version["id"]
               and (row["outputs"] or {}).get("content_hash") == version["content_hash"]]
    evidence = next((row["outputs"] for row in matched if row["outputs"].get("artifact_manifest")), {})
    checked = [str(row["outputs"]["publisher_checked_at"]) for row in matched if row["outputs"].get("publisher_checked_at")]
    out["publisher_checked_at"] = max(checked, default=None)
    if entry.get("adapter") == "restricted_file":
        checks = [row["outputs"].get("authorized_artifact_check") for row in matched
                  if row["outputs"].get("status") in {"succeeded", "unchanged", "added", "amended"}]
        raw_manifest = evidence.get("artifact_manifest")
        manifest_parts = ([{k: item.get(k) for k in ("name", "sha256", "byte_count", "content_type")}
                           for item in raw_manifest if isinstance(item, dict)] if isinstance(raw_manifest, list) else [])
        valid_checks = [check for check in checks if _artifact_check_matches(check, key, manifest_parts)]
        out["artifact_checked_at"] = max((check.get("checked_at") or "" for check in valid_checks), default=None)
        if not _recent(out["artifact_checked_at"], now, hours=26):
            findings.append(_finding("artifact_check_overdue", "No recent authorized artifact read is bound to this exact version's complete artifact set; this is not a publisher check."))
        if ledger and (ledger[0]["outputs"] or {}).get("status") not in {"succeeded", "unchanged", "added", "amended"}:
            findings.append(_finding("artifact_availability_unverified", "The most recent artifact acquisition did not succeed; earlier availability evidence has not been refreshed."))
    elif not out["publisher_checked_at"]:
        findings.append(_finding("publisher_check_unverified", "No successful live publisher check is bound to this exact version and artifact hash."))
    else:
        if not _recent(out["publisher_checked_at"], now, hours=26):
            findings.append(_finding("publisher_check_overdue", "The version's publisher check is invalid, in the future, or older than the daily freshness window."))
    reviewed_identity = bool(artifact_review and artifact_review["approved"]
                             and artifact_review["canonical_url"] == entry.get("canonical_url"))
    if out["expected_edition"] and (not reviewed_identity or artifact_review["publisher_edition"] != out["expected_edition"]):
        findings.append(_finding("edition_unverified", "Acquired bytes are not independently bound to the required publisher edition."))
    if entry.get("adapter") == "restricted_file" and not reviewed_identity:
        findings.append(_finding("artifact_identity_unverified", "An explicit review must bind the actual artifact hash to its publisher, edition and extent."))
    if artifact_review and artifact_review["coverage"] != "full":
        findings.append(_finding("partial_artifact", "A preview or excerpt cannot fulfill a complete-document requirement.", coverage=artifact_review["coverage"]))
    if artifact_review and not artifact_review["approved"]:
        findings.append(_finding("artifact_review_not_approved", "The latest artifact identity review does not approve this document."))
    if artifact_review and artifact_review["canonical_url"] != entry.get("canonical_url"):
        findings.append(_finding("artifact_review_reference_mismatch", "The artifact review refers to a different publisher document."))
    if source["canonical_url"] != entry.get("canonical_url", ""):
        findings.append(_finding("source_reference_mismatch", "Stored source metadata differs from the declared publisher reference."))
    if not evidence.get("parser_identity"):
        findings.append(_finding("parser_provenance_unverified", "No parser identity is bound to this exact version."))
    manifest = evidence.get("artifact_manifest")
    if manifest and (not isinstance(manifest, list) or any(not isinstance(item, dict) for item in manifest)):
        findings.append(_finding("artifact_manifest_invalid", "The bound run does not contain a valid artifact manifest."))
        manifest = None
    if not manifest:
        findings.append(_finding("artifact_manifest_unverified", "Legacy version lacks a complete version-bound artifact manifest; rerun through the worker."))
    # Metadata is inspectable even when permission has not been granted. Do not
    # read protected original or parsed text merely to satisfy an audit.
    if out["permissions"] and any(not out["candidate_permissions"][op]["allowed"] for op in ("store", "parse")):
        return _source_status(out)
    artifacts = []
    from app.clhear.l1.adapters.base import Artifact, DocNode
    for item in manifest or []:
        record = {k: item.get(k) for k in ("name", "uri", "sha256", "byte_count", "content_type")}
        record["verified"] = False
        out["artifacts"].append(record)
        try:
            blob = store.get(_store_key(store, item.get("uri", "")))
            if blob is None:
                findings.append(_finding("artifact_missing_or_unreadable", "A manifest artifact cannot be read from the configured store.", name=item.get("name")))
                continue
            actual_hash = _hash(blob)
            record.update(observed_sha256=actual_hash, observed_byte_count=len(blob))
            if not blob or actual_hash != item.get("sha256") or len(blob) != item.get("byte_count"):
                findings.append(_finding("artifact_hash_mismatch", "Stored original differs from the acquired manifest.", name=item.get("name")))
                continue
            record["verified"] = True
            artifacts.append(Artifact(name=item["name"], content=blob, content_type=item.get("content_type") or "application/octet-stream"))
        except Exception as exc:
            findings.append(_finding("artifact_store_error", "Artifact retrieval failed without changing stored history.", name=item.get("name"), error_type=type(exc).__name__))
    if manifest and len(artifacts) == len(manifest):
        from app.clhear.l1.pipeline import artifact_set_hash
        names = [a.name for a in artifacts]
        declared_method = evidence.get("content_hash_method")
        out["artifact_set_hash_evidence"] = {"declared_method": declared_method, "method": None,
            "content_hash": version["content_hash"], "verified_parts": len(artifacts), "verified": False}
        hash_evidence = out["artifact_set_hash_evidence"]
        if not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
            findings.append(_finding("artifact_composite_mismatch", "Original artifact names must be nonempty and unique."))
        elif declared_method not in {None, "artifact-set-v2", "legacy-unframed-v1"}:
            findings.append(_finding("artifact_hash_method_unverified", "The run declares an unsupported artifact-set hash method."))
        elif declared_method != "legacy-unframed-v1" and artifact_set_hash(artifacts) == version["content_hash"]:
            hash_evidence.update(method="artifact-set-v2", verified=True)
        elif declared_method == "artifact-set-v2":
            findings.append(_finding("artifact_composite_mismatch", "The framed original artifact set differs from its declared version hash."))
        elif _hash(b"".join(a.content for a in sorted(artifacts, key=lambda a: a.name))) == version["content_hash"]:
            # Existing single-file versions are unambiguous after individual
            # manifest/hash/readback checks. Multipart legacy hashes lost part
            # boundaries and need worker re-ingestion before acceptance.
            hash_evidence.update(method="legacy-unframed-single-v1" if len(artifacts) == 1 else "legacy-unframed-multipart-v1",
                                 verified=len(artifacts) == 1)
            if len(artifacts) != 1:
                findings.append(_finding("legacy_artifact_set_unframed", "Legacy multipart originals remain readable, but worker re-ingestion must bind artifact names, types and boundaries before acceptance."))
        else:
            findings.append(_finding("artifact_composite_mismatch", "Complete original artifact set differs from the version hash."))
    nodes = list(conn.execute(nodes_internal_select(conn).where(doc_nodes.c.source_version_id == version["id"]).order_by(doc_nodes.c.seq)).mappings())
    clause_rows = list(conn.execute(sa.select(clauses).where(clauses.c.source_version_id == version["id"]).order_by(clauses.c.ordering)).mappings())
    out["projection_hash"] = _projection_digest(nodes, clause_rows)
    if not nodes or not clause_rows:
        findings.append(_finding("empty_projection", "Expected document has no complete addressable node and clause projection."))
        return _source_status(out)
    tree_nodes = {n["id"]: DocNode(node_type=n["node_type"], ref=n["ref"], label=n["label"], heading=n["heading"], raw_text=n["raw_text"], source_fragment=n["source_fragment"], source_locator=n.get("source_locator") or {}) for n in nodes}
    roots = []
    try:
        for n in nodes:
            if n["parent_id"] is None:
                roots.append(tree_nodes[n["id"]])
            else:
                tree_nodes[n["parent_id"]].children.append(tree_nodes[n["id"]])
        from app.clhear.l1.spans import canonical_text
        canonical = canonical_text(roots)
        # Disconnected cycles must also be detected; they are not reachable
        # through canonical_text(roots).
        visited = [id(n) for root in roots for n in root.walk()]
        if len(visited) != len(nodes) or len(set(visited)) != len(nodes):
            raise ValueError("Disconnected or repeated nodes")
        if _hash(canonical.encode()) != evidence.get("canonical_text_hash"):
            findings.append(_finding("canonical_hash_mismatch", "Stored ordered document text differs from the ingestion evidence or lacks its digest."))
        if any(_hash("\n".join(n[k] for k in ("node_type", "ref", "label", "heading", "raw_text")).encode()) != n["text_hash"] for n in nodes):
            findings.append(_finding("node_hash_mismatch", "Stored node content differs from its recorded digest."))
        for clause in clause_rows:
            start, end = clause["span_start"], clause["span_end"]
            node = tree_nodes.get(clause["doc_node_id"])
            if (node is None or start is None or end is None or not 0 <= start <= end <= len(canonical)
                    or canonical[start:end] != clause["text"] or node.subtree_text() != clause["text"]
                    or _hash(clause["text"].encode()) != clause["text_hash"]):
                findings.append(_finding("clause_roundtrip_mismatch", "Stored clause, subtree, hash or character span disagree.", clause_id=clause["id"]))
        if artifacts and len(artifacts) == len(manifest or []):
            from app.clhear.l1.originals import verify_original_projection
            original = verify_original_projection(key, entry.get("adapter", ""), artifacts, nodes,
                                                  clauses=clause_rows, canonical_url=entry.get("canonical_url", ""))
            out["original_comparison"] = original
            findings.extend(original["findings"])
            if not original["verified"] and not original["findings"]:
                findings.append(_finding("independent_text_comparison_unverified", "The original comparison did not return verified evidence."))
        else:
            findings.append(_finding("independent_text_comparison_unverified", "A complete preserved original artifact set is required for independent comparison."))

    except (KeyError, ValueError, RecursionError):
        findings.append(_finding("invalid_document_tree", "Stored tree is disconnected, cyclic, or cannot be reconstructed."))
    return _source_status(out)


def _source_status(out):
    codes = {f["code"] for f in out["findings"]}
    if "permission_blocked" in codes:
        status = "permission_blocked"
    elif "permission_unverified" in codes:
        status = "permissions_unverified"
    elif "awaiting_artifact" in codes:
        status = "awaiting_artifact"
    else:
        status = "gaps" if codes else "verified"
    technical_codes = codes - {"permission_unverified", "permission_blocked"}
    technical = bool((out.get("original_comparison") or {}).get("verified")) and not technical_codes
    return {**out, "status": status, "verified": not codes,
            "technical_verified": technical,
            "release_eligible": not codes}


def run_inventory_audit(engine, store, *, job_id, scope="registered", discover=False, discovery_cycle_date=None):
    """Reconcile the worker's authoritative engine; never seed or mutate corpus.

    A no-discovery post-import audit carries the previous discovery evidence
    forward, including its timestamp and unresolved failures. It does not make
    a cached inventory freshly checked. All audit rows are append-only.
    """
    scope = _scope(scope)
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("The owning worker job_id is required")
    started = datetime.now(timezone.utc)
    tick = time.monotonic()
    entries = _declared_entries(scope)
    prior = _latest(engine, scope)
    discovery = {"complete": False, "checked_at": None, "categories": [], "pages": [], "findings": [_finding("discovery_not_run", "Publisher collections have not been enumerated by the worker.")]}
    discovered, prior_aliases = {}, []
    if prior:
        discovery = prior["summary"]["discovery"]
        with engine.connect() as conn:
            old = conn.execute(sa.select(inventory_snapshots.c.definition).where(inventory_snapshots.c.id == prior["inventory_id"])).scalar_one()
        discovered = {e["key"]: e for e in old["entries"] if e.get("discovered_category")}
        prior_aliases = list(old.get("source_aliases", []))
    if discover:
        from app.clhear.l1.workflow import bind_execution
        from app.clhear.l1.discovery import bind_cycle_date
        with bind_execution(engine, job_id), bind_cycle_date(discovery_cycle_date):
            new_entries, discovery = _discover_publishers(engine, store, job_id)
        # A failed/partial crawl cannot silently remove previously expected
        # documents from the denominator. Removal requires a new scope review.
        discovered.update(new_entries)
    aliases, alias_findings = list(prior_aliases), []
    declared_urls = {}
    for entry in entries.values():
        if entry.get("canonical_url"):
            declared_urls.setdefault((entry["canonical_url"], entry.get("adapter")), []).append(entry["key"])
    for key, entry in sorted(discovered.items()):
        same = declared_urls.get((entry.get("canonical_url"), entry.get("adapter")), [])
        if key not in entries and len(same) == 1:
            # Exact URL + parser identity is an alias, not another expected
            # original. Preserve existing document identity and all DB history.
            alias = {"discovered_source_key": key, "source_key": same[0], "canonical_url": entry["canonical_url"]}
            if alias not in aliases:
                aliases.append(alias)
            continue
        if key not in entries and len(same) > 1:
            alias_findings.append(_finding("ambiguous_document_alias", "Multiple declared documents share this exact original URL and parser; identity needs review.", source_keys=sorted([key, *same])))
        entries.setdefault(key, entry)
    aliases.sort(key=lambda value: (value["discovered_source_key"], value["source_key"], value["canonical_url"]))
    if alias_findings:
        discovery = {**discovery, "complete": False, "findings": [*discovery["findings"], *alias_findings]}
    from app.clhear.l1.publishers import profiles_for_scope, BOUNDARIES, coverage_findings
    profiles = profiles_for_scope(scope)
    missing = coverage_findings(profiles)
    known_codes = {(f.get("code"), f.get("publisher_id")) for f in discovery["findings"]}
    discovery = {**discovery, "findings": [*discovery["findings"], *[f for f in missing if (f["code"], f["publisher_id"]) not in known_codes]],
                 "complete": bool(discovery["complete"] and discovery.get("publishers") and not missing)}
    definition = {"scope": scope, "scope_version": SCOPE_VERSION, "boundaries": BOUNDARIES,
                  "publishers": profiles, "source_aliases": aliases, "categories": [],
                  "required_editions": {key: edition for key, edition in EXPECTED_EDITIONS.items() if key in entries},
                  "entries": [entries[key] for key in sorted(entries)]}
    digest = _digest(definition)
    with engine.begin() as conn:
        inventory_id = conn.execute(sa.select(inventory_snapshots.c.id).where(inventory_snapshots.c.inventory_hash == digest)).scalar()
        if inventory_id is None:
            inventory_id = str(uuid.uuid4())
            # Concurrent worker audits may freeze the same inventory. The
            # existing unique key is the arbiter; upsert is dialect-specific.
            if engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            conn.execute(insert(inventory_snapshots).values(id=inventory_id, scope=scope, scope_version=SCOPE_VERSION,
                         inventory_hash=digest, definition=definition).on_conflict_do_nothing(index_elements=["inventory_hash"]))
            inventory_id = conn.execute(sa.select(inventory_snapshots.c.id).where(inventory_snapshots.c.inventory_hash == digest)).scalar_one()
    with engine.connect() as conn:
        evidence = [_audit_source(conn, store, e, started) for e in definition["entries"]]
        review = _review(conn, digest)
        from app.clhear.l1.origin import corpus_sources_predicate
        actual_keys = set(conn.execute(sa.select(sources.c.key).where(corpus_sources_predicate())).scalars())
    findings = list(discovery["findings"])
    from app.clhear.l1.source_registry import COLLECTION_SOURCE_KEYS, REFERENCE_SOURCE_KEYS
    outside = sorted(actual_keys - entries.keys() - COLLECTION_SOURCE_KEYS - REFERENCE_SOURCE_KEYS)
    if outside:
        findings.append(_finding("outside_declared_scope", "Existing sources are outside this declared inventory and need scope classification.", source_keys=outside))
    if not review or not review["approved"]:
        findings.append(_finding("scope_review_required", "An independent reviewed publisher inventory must confirm the exact categories and expected document list."))
    discovery_ok = bool(discovery["complete"])
    full_scope_verified = bool(review and review["approved"] and discovery_ok and not outside)
    count = Counter(f["code"] for e in evidence for f in e["findings"])
    verified = sum(e["verified"] for e in evidence)
    finished = datetime.now(timezone.utc)
    summary = {"audit_id": str(uuid.uuid4()), "job_id": job_id, "scope": scope, "scope_version": SCOPE_VERSION,
               "inventory_hash": digest, "audited_at": finished.isoformat(), "duration_ms": round((time.monotonic() - tick) * 1000),
               "status": "verified" if full_scope_verified and verified == len(evidence) else "gaps",
               "known_expected": len(evidence), "known_expected_is_lower_bound": not full_scope_verified,
               "verified": verified, "unresolved": len(evidence) - verified, "discovery_complete": discovery["complete"],
               "technical_verified": sum(bool(e.get("technical_verified")) for e in evidence),
               "release_eligible": bool(full_scope_verified and evidence and verified == len(evidence)),
               "full_scope_verified": full_scope_verified, "scope_review": review, "discovery": discovery,
               "publisher_profiles": profiles, "publisher_count": len(profiles), "source_aliases": aliases,
               "expected_total": len(evidence) if full_scope_verified else None, "denominator_known": full_scope_verified,
               "findings": findings, "sources": evidence, "counts": dict(count), "current_binding_valid": True,
               "bindings_hash": _digest([{k: e.get(k) for k in ("source_key", "source_version_id", "content_hash", "projection_hash", "permissions", "candidate_permissions", "artifact_review")} for e in evidence]),
               "collection_sources": [{"source_key": key, "status": "discovery_index", "source_role": "collection", "counts_as_document": False,
                                       "detail": "Collection history is retained; constituent documents require their own imports."}
                                      for key in sorted(COLLECTION_SOURCE_KEYS)],
               "reference_sources": [{"source_key": key, "source_role": "reference", "status": "official_document_inventory_required", "counts_as_document": False}
                                     for key in sorted(REFERENCE_SOURCE_KEYS)]}

    with engine.begin() as conn:
        conn.execute(inventory_audits.insert().values(id=summary["audit_id"], inventory_id=inventory_id,
                     scope=scope, job_id=job_id, started_at=started, finished_at=finished, summary=summary))
    return summary


def inventory_summary(engine, scope="registered"):
    """Latest immutable audit, plus read-time checks that prevent stale passes."""
    scope = _scope(scope)
    from app.clhear.l1.publishers import profiles_for_scope
    profiles = profiles_for_scope(scope)
    blank = {"expected_total": None, "denominator_known": False, "publisher_profiles": profiles, "publisher_count": len(profiles), "status": "not_run", "scope": scope, "scope_version": SCOPE_VERSION, "audit_id": None, "job_id": None,
             "inventory_hash": None, "audited_at": None, "known_expected": len(_declared_entries(scope)),
             "known_expected_is_lower_bound": True, "verified": 0, "unresolved": len(_declared_entries(scope)),
             "technical_verified": 0, "release_eligible": False,
             "discovery_complete": False, "full_scope_verified": False, "current_binding_valid": False,
             "findings": [], "sources": [], "counts": {}, "discovery": {"complete": False, "categories": [], "pages": [], "findings": [], "checked_at": None}}
    if not _available(engine):
        return {**blank, "status": "unavailable", "reason": "migration_required"}
    row = _latest(engine, scope)
    if not row:
        return blank
    summary = row["summary"]
    invalid = []
    with engine.connect() as conn:
        for source in summary["sources"]:
            current = list(conn.execute(sa.select(source_versions.c.id, source_versions.c.content_hash, sources.c.key, sources.c.issuer)
                         .join(sources, sources.c.id == source_versions.c.source_id)
                         .where(sources.c.key == source["source_key"], source_versions.c.status == "in_force")).mappings())
            from app.clhear.l1.origin import is_test_source
            if any(is_test_source(version) for version in current):
                invalid.append(source["source_key"])
            expected = [(source["source_version_id"], source["content_hash"])] if source["source_version_id"] is not None else []
            if [(v["id"], v["content_hash"]) for v in current] != expected:
                invalid.append(source["source_key"])
            review_now = _artifact_review(conn, source["source_key"], source["content_hash"]) if source["content_hash"] else None
            if (source.get("artifact_review") or {}).get("id") != (review_now or {}).get("id"):
                invalid.append(source["source_key"])
            for op in ("acquire", "store", "parse"):
                before = source.get("permissions", {}).get(op)
                if before:
                    now = permissions.decision(conn, source["source_key"], op)
                    if (before["allowed"], before["permission_id"]) != (now["allowed"], now["permission_id"]):
                        invalid.append(source["source_key"])
                candidate_before = source.get("candidate_permissions", {}).get(op)
                if candidate_before:
                    candidate_now = permissions.candidate_decision(conn, source["source_key"], op,
                                                                  canonical_url=source.get("canonical_url", ""))
                    fields = ("allowed", "permission_id", "authority_type")
                    if tuple(candidate_before.get(k) for k in fields) != tuple(candidate_now.get(k) for k in fields):
                        invalid.append(source["source_key"])
        review = _review(conn, summary["inventory_hash"])
    if not review or not review["approved"]:
        summary = {**summary, "full_scope_verified": False, "known_expected_is_lower_bound": True,
                   "expected_total": None, "denominator_known": False}
    if not _recent(summary["audited_at"], datetime.now(timezone.utc), hours=26):
        invalid.append("audit_overdue")
    # Registry edits also invalidate an old frozen report, without a GET
    # endpoint making writes or enumerating publisher websites.
    declared = _declared_entries(scope)
    with engine.connect() as conn:
        frozen = conn.execute(sa.select(inventory_snapshots.c.definition).where(inventory_snapshots.c.id == row["inventory_id"])).scalar_one()
    old_entries = {e["key"]: e for e in frozen["entries"]}
    editions = {key: edition for key, edition in EXPECTED_EDITIONS.items() if key in old_entries}
    from app.clhear.l1.publishers import profiles_for_scope
    if (frozen["scope_version"] != SCOPE_VERSION or frozen.get("publishers") != profiles_for_scope(scope) or frozen.get("required_editions") != editions
            or any(old_entries.get(k) != v for k, v in declared.items())):
        invalid.append("declared_scope_changed")
    if invalid:
        return {**summary, "status": "stale", "current_binding_valid": False, "full_scope_verified": False,
                "release_eligible": False,
                "known_expected_is_lower_bound": True, "expected_total": None, "denominator_known": False,
                "findings": [*summary["findings"], _finding("audit_binding_changed", "Stored evidence must be rerun against current source versions, permissions, scope or freshness.", source_keys=sorted(set(invalid)))]}
    if not summary["full_scope_verified"] and summary["status"] == "verified":
        summary = {**summary, "status": "gaps"}
    if not summary["full_scope_verified"]:
        summary = {**summary, "release_eligible": False}
    return {**summary, "current_binding_valid": True}


def source_inventory_evidence(engine, source_key):
    """Metadata-only current-source evidence, explicitly version/hash bound."""
    summaries = [inventory_summary(engine, "registered")]
    for summary in summaries:
        for source in summary["sources"]:
            if source["source_key"] == source_key:
                return {**source, "audit_id": summary["audit_id"], "job_id": summary["job_id"],
                        "audited_at": summary["audited_at"], "inventory_hash": summary["inventory_hash"],
                        "audit_verified": source["verified"], "verified": source["verified"] and summary["current_binding_valid"],
                        "technical_verified": bool(source.get("technical_verified")) and summary["current_binding_valid"],
                        "release_eligible": bool(source.get("release_eligible", source["verified"])) and summary["current_binding_valid"],
                        "current_binding_valid": summary["current_binding_valid"], "scope_verified": summary["full_scope_verified"]}
    return {"source_key": source_key, "status": summaries[0]["status"] if not summaries[0].get("audit_id") else "not_in_inventory",
            "reason": summaries[0].get("reason"), "source_version_id": None, "content_hash": None,
            "audit_id": None, "verified": False, "technical_verified": False,
            "release_eligible": False, "current_binding_valid": False, "findings": [], "artifacts": [], "permissions": {}}


def acceptance_status(engine, scope="registered"):
    """Release gate: exact current bindings, fresh audit and reviewed full scope.

    This never runs acquisition or a new audit. A release gate re-hashes the
    current projection to reject in-place corruption of an otherwise unchanged
    version ID. Protected text is only read after current parse/store grants.
    """
    summary = inventory_summary(engine, scope)
    from app.clhear.l1.translation import english_acceptance
    english = english_acceptance(engine, [source["source_version_id"] for source in summary["sources"]
                                         if source.get("source_version_id") is not None])
    reasons = []
    if summary["status"] != "verified":
        reasons.append("inventory_not_verified")
    if not summary["full_scope_verified"]:
        reasons.append("scope_not_verified")
    if not summary["current_binding_valid"]:
        reasons.append("current_binding_invalid")
    if not summary["known_expected"] or summary["unresolved"] or summary["verified"] != summary["known_expected"]:
        reasons.append("expected_documents_unresolved")
    now = datetime.now(timezone.utc)
    if not _recent(summary.get("audited_at"), now):
        reasons.append("audit_overdue")
    discovery_at = summary.get("discovery", {}).get("checked_at")
    if not _recent(discovery_at, now):
        reasons.append("publisher_inventory_overdue")
    for source in summary["sources"]:
        if any(not source.get("permissions", {}).get(op, {"allowed": True})["allowed"] for op in ("acquire", "store", "parse")):
            reasons.append("publisher_permission_unresolved:" + source["source_key"])
        artifact_basis = source.get("freshness_basis") == "reviewed_immutable_artifact"
        checked_at = source.get("artifact_checked_at") if artifact_basis else source.get("publisher_checked_at")
        if not _recent(checked_at, now):
            reasons.append(("artifact_check_overdue:" if artifact_basis else "publisher_check_overdue:") + source["source_key"])
        if artifact_basis:
            review = source.get("artifact_review") or {}
            if (not review.get("approved") or review.get("coverage") != "full"
                    or review.get("canonical_url") != source.get("canonical_url")
                    or not review.get("publisher_edition")
                    or (source.get("expected_edition") and review["publisher_edition"] != source["expected_edition"])):
                reasons.append("artifact_identity_unverified:" + source["source_key"])
    if not reasons:
        with engine.connect() as conn:
            for source in summary["sources"]:
                if any(not permissions.decision(conn, source["source_key"], op)["allowed"] for op in ("store", "parse") if op in source["permissions"]):
                    reasons.append("permission_changed")
                    break
                nodes = list(conn.execute(nodes_internal_select(conn).where(doc_nodes.c.source_version_id == source["source_version_id"]).order_by(doc_nodes.c.seq)).mappings())
                clause_rows = list(conn.execute(sa.select(clauses).where(clauses.c.source_version_id == source["source_version_id"]).order_by(clauses.c.ordering)).mappings())
                if _projection_digest(nodes, clause_rows) != source.get("projection_hash"):
                    reasons.append("projection_changed:" + source["source_key"])
    if not english["passed"]:
        reasons.append("english_views_unresolved")
    return {"passed": not reasons, "release_eligible": not reasons,
            "reasons": reasons, "audit_id": summary.get("audit_id"),
            "english": english,
            "inventory_hash": summary.get("inventory_hash"), "bindings_hash": summary.get("bindings_hash"),
            "scope": scope, "audited_at": summary.get("audited_at"), "evidence": summary,
            "method": "Reviewed exact inventory; complete artifact hashes; ordered original and clause/span checks; current permissions, versions and projection digests"}
