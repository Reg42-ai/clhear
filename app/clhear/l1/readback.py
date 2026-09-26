# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Version-bound readback of an imported source, independent of the import.

The import transaction already proves the projection before it commits
(pipeline._persist -> originals.verify_original_projection). Deployment
verification must not take the worker's word for it: after the handler
returns, this module reads the version row, the archived originals and the
encoded projection back from the record and the datalake and compares them
with the identities the import reported. Every check is bound to the exact
``source_version_id`` the task named, so a later or earlier version cannot
stand in for it.
"""
from __future__ import annotations

import hashlib

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l1 import originals, spans
from app.clhear.l1.adapters.base import Artifact
from app.clhear.l1.models import clauses, doc_nodes, source_versions, sources

SUCCESS_STATUSES = frozenset({"added", "amended", "unchanged", "up-to-date"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_source_version(engine: Engine, store, source_key: str, summary: dict) -> dict:
    """Compare what the record holds with what the import reported.

    Checks (all must pass):
    * ``version_identity`` — the reported ``source_version_id`` exists for this
      source, is ``in_force`` and carries the reported ``content_hash``.
    * ``artifact_hashes`` — every archived original in the reported manifest reads
      back from the datalake with its reported sha256 and byte count.
    * ``encoded_projection`` — the stored doc_nodes/clauses of that version
      reproduce the canonical text hash the import reported and pass the
      independent original-vs-projection verification against the read-back
      originals.
    """
    result = {"source_key": source_key, "status": "failed", "verified": False, "checks": {}, "findings": []}
    summary = summary or {}
    if summary.get("status") not in SUCCESS_STATUSES:
        result["findings"].append({"code": "import_not_successful", "status": summary.get("status")})
        return result
    version_id = summary.get("source_version_id")
    manifest = summary.get("artifact_manifest") or []
    try:
        version_id = int(version_id)
    except (TypeError, ValueError):
        result["findings"].append({"code": "missing_version_identity"})
        return result

    with engine.connect() as conn:
        row = conn.execute(
            sa.select(source_versions.c.id, source_versions.c.status, source_versions.c.content_hash,
                      source_versions.c.version_label, sources.c.adapter, sources.c.canonical_url)
            .join(sources, sources.c.id == source_versions.c.source_id)
            .where(sources.c.key == source_key, source_versions.c.id == version_id)
        ).mappings().first()
        if row is None:
            result["findings"].append({"code": "version_row_missing", "source_version_id": version_id})
            return result
        identity_ok = row["status"] == "in_force" and bool(row["content_hash"]) and row["content_hash"] == summary.get("content_hash")
        result["checks"]["version_identity"] = {
            "passed": identity_ok, "source_version_id": version_id, "version_label": row["version_label"],
            "status": row["status"], "content_hash": row["content_hash"], "reported_content_hash": summary.get("content_hash"),
        }
        if not identity_ok:
            result["findings"].append({"code": "version_identity_mismatch"})

        artifacts, artifact_checks = [], []
        for item in manifest:
            data = store.get(item.get("key", "")) if item.get("key") else None
            digest = _sha256(data) if data is not None else None
            ok = data is not None and digest == item.get("sha256") and len(data) == item.get("byte_count", len(data))
            artifact_checks.append({"name": item.get("name"), "passed": ok, "reported_sha256": item.get("sha256"),
                                    "readback_sha256": digest, "byte_count": None if data is None else len(data)})
            if ok:
                artifacts.append(Artifact(name=item["name"], content=data, content_type=item.get("content_type") or "application/octet-stream"))
        artifacts_ok = bool(manifest) and all(c["passed"] for c in artifact_checks)
        result["checks"]["artifact_hashes"] = {"passed": artifacts_ok, "count": len(manifest), "artifacts": artifact_checks}
        if not artifacts_ok:
            result["findings"].append({"code": "artifact_readback_mismatch" if manifest else "artifact_manifest_missing"})

        nodes = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version_id)
                             .order_by(doc_nodes.c.seq)).mappings().all()
        clause_rows = conn.execute(sa.select(clauses).where(clauses.c.source_version_id == version_id)).mappings().all()
    tree = originals._stored_tree(nodes) if nodes else []
    canonical = _sha256(spans.canonical_text(tree).encode()) if tree else None
    canonical_ok = canonical is not None and canonical == summary.get("canonical_text_hash")
    projection = {"passed": False, "node_count": len(nodes), "clause_count": len(clause_rows),
                  "canonical_text_hash": canonical, "reported_canonical_text_hash": summary.get("canonical_text_hash")}
    if artifacts_ok and tree:
        proof = originals.verify_original_projection(source_key, row["adapter"], artifacts, tree, clause_rows,
                                                     canonical_url=row["canonical_url"] or "")
        projection.update(original_verification=proof["status"], method=proof["method"],
                          findings=[f.get("code") for f in proof.get("findings", [])])
        projection["passed"] = canonical_ok and bool(proof["verified"])
    elif not canonical_ok:
        result["findings"].append({"code": "canonical_text_hash_mismatch"})
    result["checks"]["encoded_projection"] = projection
    if not projection["passed"]:
        result["findings"].append({"code": "encoded_projection_not_verified"})

    result["verified"] = all(c["passed"] for c in result["checks"].values()) and len(result["checks"]) == 3
    result["status"] = "verified" if result["verified"] else "failed"
    return result


def compare_repeat(first: dict, repeat: dict) -> dict:
    """An unchanged repeat must name the same version, originals and projection."""
    keys = ("source_version_id", "content_hash", "canonical_text_hash")
    same = {k: (first or {}).get(k) == (repeat or {}).get(k) and (first or {}).get(k) is not None for k in keys}
    first_hashes = sorted(a.get("sha256") for a in (first or {}).get("artifact_manifest") or [])
    repeat_hashes = sorted(a.get("sha256") for a in (repeat or {}).get("artifact_manifest") or [])
    same["artifact_hashes"] = bool(first_hashes) and first_hashes == repeat_hashes
    return {"passed": all(same.values()) and (repeat or {}).get("status") in {"unchanged", "up-to-date"},
            "repeat_status": (repeat or {}).get("status"), "same": same}
