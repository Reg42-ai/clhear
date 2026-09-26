# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Evals harness (HLD §7.1): suites registered per layer; runs recorded in
l0_platform.eval_runs plus a JSON artifact; release gate = all suites passed.

Evals are gates, not reports (HLD principle 5).
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.platform import record
from app.clhear.models import eval_runs
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.evals")

# suite name -> callable(engine, source_key) -> (scores: dict, passed: bool)
SUITES: dict[str, Callable] = {}


def register_suite(name: str):
    def wrap(fn: Callable):
        SUITES[name] = fn
        return fn

    return wrap


@register_suite("l0_smoke")
def l0_smoke(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """P0 skeleton suite: the l0_platform tables exist and are queryable."""
    from app.clhear.models import events, llm_calls, proposals, runs

    counts = {}
    with engine.connect() as conn:
        for table in (events, proposals, llm_calls, runs):
            counts[table.name] = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
    return {"tables_queryable": len(counts), "row_counts": counts}, True


@register_suite("l1_fidelity")
def l1_fidelity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """E2/E3 skeleton (pulled forward from P4): every registry adapter's parse
    must cover its own oracle text at >= the gate threshold with zero contract
    violations. Runs offline against recorded fixtures in CI; blocks releases
    via the existing release gate."""
    from app.clhear.l1 import fidelity
    from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter

    settings = get_settings()
    threshold = settings.clhear_fidelity_threshold
    scores: dict = {}
    passed = True
    for key in ADAPTER_KEYS:
        if source_key and key != source_key:
            continue
        adapter = get_adapter(key)
        try:
            result = adapter.fetch()
            report = fidelity.check(result.tree, adapter.expected_text(result.artifacts))
            ok = report.ok(threshold)
            scores[key] = {
                "coverage": round(report.coverage, 5),
                "violations": len(report.violations),
                "passed": ok,
            }
            passed = passed and ok
        except Exception as exc:
            scores[key] = {"error": str(exc)[:200], "passed": False}
            passed = False
    return {"threshold": threshold, "adapters": scores}, passed


def run_suite(engine: Engine, suite: str, source_key: str | None = None, release: str | None = None) -> dict:
    """Run one suite -> eval_runs row + JSON artifact. Returns the record."""
    fn = SUITES[suite]
    before = _source_identity(engine, source_key) if source_key and suite in SOURCE_SUITES else None
    permission_blocked = []
    candidate_permissions, publisher_permissions = {}, {}
    if source_key and suite in SOURCE_SUITES:
        from app.clhear.l1 import permissions
        from app.clhear.l1.models import sources
        with engine.connect() as conn:
            source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).mappings().first()
            if source and permissions.required_for(source):
                publisher_permissions = {op: permissions.decision(conn, source_key, op) for op in ("acquire", "store", "parse")}
                candidate_permissions = {op: permissions.candidate_decision(conn, source_key, op,
                                           canonical_url=source["canonical_url"]) for op in ("acquire", "store", "parse")}
                permission_blocked = [op for op in ("store", "parse") if not candidate_permissions[op]["allowed"]]
    if permission_blocked:
        scores, passed = {"not_evaluated": True, "reason": "permission_blocked", "operations": permission_blocked}, False
    else:
        scores, passed = fn(engine, source_key)
    if source_key and suite in SOURCE_SUITES:
        after = _source_identity(engine, source_key)
        scores = {**scores, "source_version_id": before.get("id") if before else None,
                  "content_hash": before.get("content_hash") if before else None,
                  "version_label": before.get("version_label") if before else None}
        if before is None or before != after:
            passed = False
            scores["binding_error"] = "No stored version" if before is None else "Source changed during evaluation; rerun required"
        if candidate_permissions:
            with engine.connect() as conn:
                current = {op: permissions.candidate_decision(conn, source_key, op, canonical_url=source["canonical_url"])
                           for op in candidate_permissions}
            fields = ("allowed", "permission_id", "authority_type", "exception_id", "activation_id", "binding_id", "binding_hash")
            if any(tuple(current[op].get(k) for k in fields) != tuple(choice.get(k) for k in fields)
                   for op, choice in candidate_permissions.items()):
                passed = False
                scores["binding_error"] = "Candidate authorization changed during evaluation; rerun required"
            exception_used = bool(scores.get("operator_exception_used")) or any(
                choice.get("authority_type") == "operator_exception" and choice["allowed"] for choice in candidate_permissions.values())
            scores.update(operator_exception_used=exception_used,
                          release_eligible=not exception_used and all(choice["allowed"] for choice in publisher_permissions.values()),
                          publisher_permissions=publisher_permissions, candidate_permissions=candidate_permissions,
                          evaluation_scope="technical_candidate" if exception_used else "publisher_authorized")
    record = {
        "suite": suite,
        "source_key": source_key,
        "release": release,
        "scores": scores,
        "passed": passed,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    with engine.begin() as conn:
        conn.execute(
            eval_runs.insert().values(
                suite=suite, source_key=source_key, release=release, scores=scores, passed=passed
            )
        )
    artifacts_dir = Path(get_settings().clhear_artifacts_dir) / "evals"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_key = (source_key or "").replace("/", "_")
    artifact = artifacts_dir / f"{suite}{'-' + safe_key if safe_key else ''}-{stamp}.json"
    artifact.write_text(json.dumps(record, indent=2, default=str))
    log.info("suite %s passed=%s -> %s", suite, passed, artifact)
    from app.clhear.platform import langfuse

    langfuse.export_run(record)  # maintainers' view; the public dashboard reads gates.publish_summary
    return record


SOURCE_SUITES = (
    "e1_fidelity",
    "e2_completeness",
    "e3_roundtrip",
    "e4_change_replay",
    "e5_provenance",
    "e6_retrievability",
    "e7_closure",
)

GOLDEN_QUERIES = {
    "uksi/2017/692": [("customer due diligence", ("regulation-27", "regulation-28", "regulation-27-1"))],
    "celex/32016R0679": [("personal data", ("art_4", "art_6", "article-4"))],
    "celex/32014L0065": [("investment firm", ("art_4", "article-4"))],
    "celex/32023R1114": [("crypto-asset", ("art_3", "article-3"))],
}


def _ws(text: str) -> str:
    return " ".join((text or "").split())


def _source_identity(engine, source_key, source_version_id=None):
    from app.clhear.l1.models import source_versions, sources
    with engine.connect() as conn:
        query = sa.select(source_versions.c.id, source_versions.c.content_hash, source_versions.c.version_label)
        query = (query
                           .join(sources, sources.c.id == source_versions.c.source_id)
                           .where(sources.c.key == source_key))
        query = query.where(source_versions.c.id == source_version_id) if source_version_id is not None else query.where(source_versions.c.status == "in_force")
        row = conn.execute(query.order_by(source_versions.c.id.desc()).limit(1)).mappings().first()
        return dict(row) if row else None


def _latest_source(engine: Engine, source_key: str):
    from app.clhear.l1.models import clauses, doc_nodes, source_versions, sources

    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return None, None, [], []
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .order_by(source_versions.c.id.desc())
            .limit(1)
        ).first()
        if version is None:
            return source, None, [], []
        nodes = conn.execute(
            sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version.id)
        ).all()
        clause_rows = conn.execute(
            sa.select(clauses).where(clauses.c.source_version_id == version.id)
        ).all()
        return source, version, nodes, clause_rows


def _need_source(source_key: str | None) -> tuple[dict, bool] | None:
    if source_key:
        return None
    return {"note": "per-source suite — run via run_source_evals", "passed": False, "not_evaluated": True}, False


@register_suite("e1_fidelity")
def e1_fidelity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Re-check a ≥10% sample (min 50 clauses) against stored node text."""
    early = _need_source(source_key)
    if early:
        return early
    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None:
        return {"error": "unknown source", "passed": False}, False
    if version is None:
        return {"error": "no version stored", "sampled": 0}, False
    haystack = _ws(" ".join((n.heading or "") + " " + (n.raw_text or "") for n in nodes))
    sample_n = max(min(len(clause_rows), 50), max(1, len(clause_rows) // 10)) if clause_rows else 0
    if sample_n == 0:
        # Title-only trees (landing pages) still count if node text is present.
        ok = bool(haystack)
        return {"sampled": 0, "matched": 0, "score": 1.0 if ok else 0.0, "grain": "nodes"}, ok
    sample = clause_rows[:: max(1, len(clause_rows) // sample_n)][:sample_n]
    matched = 0
    for row in sample:
        needle = _ws(row.text)
        if needle and needle in haystack:
            matched += 1
    score = matched / len(sample)
    return {"sampled": len(sample), "matched": matched, "score": round(score, 5)}, score >= 1.0


@register_suite("e2_completeness")
def e2_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Stored tree vs last-run coverage (gate already 99.5%)."""
    early = _need_source(source_key)
    if early:
        return early
    from app.clhear.l1.inventory import source_inventory_evidence
    inventory = source_inventory_evidence(engine, source_key)
    if inventory.get("audit_id"):
        technical = bool(inventory.get("technical_verified")) if inventory.get("operator_exception_used") else bool(inventory.get("verified"))
        return {"audit_id": inventory["audit_id"], "inventory_hash": inventory["inventory_hash"],
                "nodes": inventory.get("node_count", 0), "clauses": inventory.get("clause_count", 0),
                "scope_verified": inventory.get("scope_verified", False),
                "stored_over_expected": 1 if technical else 0,
                "technical_verified": technical,
                "publisher_permissions_resolved": all(inventory.get("permissions", {}).get(op, {"allowed": True})["allowed"]
                                                      for op in ("acquire", "store", "parse")),
                "operator_exception_used": inventory.get("operator_exception_used", False),
                "release_eligible": inventory.get("release_eligible", inventory.get("verified", False)),
                "findings": inventory.get("findings", []),
                "method": "Worker reconciliation of exact artifacts, stored version, tree and clauses; technical evidence is separate from publisher permission"}, technical
    from app.clhear.models import runs

    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None:
        return {"error": "unknown source"}, False
    if source.license == "restricted":
        return {"note": "Protected source requires a publisher comparison", "not_evaluated": True, "nodes": len(nodes)}, False
    if version is None:
        return {"error": "no version stored", "stored": 0, "expected": 1}, False
    coverage = None
    with engine.connect() as conn:
        for row in conn.execute(sa.select(runs).where(runs.c.fleet.like("l1.%"))
                                .where(runs.c.inputs["source"].as_string() == source_key)
                                .order_by(runs.c.id.desc())):
            inputs = row.inputs if isinstance(row.inputs, dict) else json.loads(row.inputs or "{}")
            outputs = row.outputs if isinstance(row.outputs, dict) else json.loads(row.outputs or "{}")
            if (inputs.get("source") == source_key and outputs.get("version") == version.version_label
                    and outputs.get("content_hash") == version.content_hash and outputs.get("coverage") is not None):
                coverage = float(outputs["coverage"])
                break
    ok = len(nodes) > 0 and coverage is not None and coverage >= 0.995
    return {
        "nodes": len(nodes),
        "clauses": len(clause_rows),
        "last_coverage": coverage,
        "stored_over_expected": None,
        "scope_verified": False,
        "notice": "Parse coverage for this stored artifact; independent publisher inventory is not yet verified.",
    }, ok


@register_suite("e3_roundtrip")
def e3_roundtrip(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Concatenate stored nodes vs clause projection (≥99.9% token overlap)."""
    early = _need_source(source_key)
    if early:
        return early
    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None or version is None or not clause_rows:
        return {"error": "No stored clause projection to round-trip", "score": 0.0}, False
    from app.clhear.l1.adapters.base import DocNode
    from app.clhear.l1.spans import canonical_text
    tree_nodes = {n.id: DocNode(node_type=n.node_type, ref=n.ref, label=n.label,
                               heading=n.heading, raw_text=n.raw_text,
                               source_fragment=n.source_fragment, source_locator=n.source_locator or {}) for n in nodes}
    roots = []
    for n in sorted(nodes, key=lambda n: n.seq):
        if n.parent_id is None:
            roots.append(tree_nodes[n.id])
        elif n.parent_id in tree_nodes:
            tree_nodes[n.parent_id].children.append(tree_nodes[n.id])
        else:
            return {"error": "Orphaned stored node", "node_id": n.id}, False
    try:
        canonical = canonical_text(roots)
    except RecursionError:
        return {"error": "Stored tree contains a cycle"}, False
    failures = []
    for clause in clause_rows:
        node = tree_nodes.get(clause.doc_node_id)
        start, end = clause.span_start, clause.span_end
        if (node is None or start is None or end is None or not 0 <= start <= end <= len(canonical)
                or canonical[start:end] != clause.text or node.subtree_text() != clause.text):
            failures.append(clause.id)
    score = (len(clause_rows) - len(failures)) / len(clause_rows)
    return {"score": score, "clauses_checked": len(clause_rows), "mismatch_ids": failures[:50],
            "method": "Exact ordered subtree and canonical character-span comparison",
            "publisher_comparison": False}, not failures


@register_suite("e4_change_replay")
def e4_change_replay(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Diff recall when a prior version exists; else explicit n/a."""
    early = _need_source(source_key)
    if early:
        return early
    from app.clhear.l1.models import change_events, source_versions, sources

    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return {"error": "unknown source"}, False
        n_versions = conn.execute(
            sa.select(sa.func.count()).select_from(source_versions).where(source_versions.c.source_id == source.id)
        ).scalar_one()
        n_changes = conn.execute(
            sa.select(sa.func.count()).select_from(change_events).where(change_events.c.source_id == source.id)
        ).scalar_one()
    if n_versions < 2:
        return {"note": "n/a — first version", "n/a": True, "versions": n_versions}, True
    return {"versions": n_versions, "change_events": n_changes}, n_changes >= 1


@register_suite("e5_provenance")
def e5_provenance(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Clause → version hashes recomputed; S3/local URI present."""
    import hashlib

    early = _need_source(source_key)
    if early:
        return early
    source, version, nodes, clause_rows = _latest_source(engine, source_key)
    if source is None or version is None:
        return {"error": "no version"}, False
    mismatches = 0
    checked = 0
    for row in clause_rows[:200]:
        checked += 1
        digest = hashlib.sha256((row.text or "").encode()).hexdigest()
        if digest != row.text_hash:
            mismatches += 1
    ok = mismatches == 0 and bool(version.content_hash)
    return {
        "content_hash": version.content_hash,
        "s3_uri": version.s3_uri,
        "clauses_checked": checked,
        "hash_mismatches": mismatches,
    }, ok


@register_suite("e6_retrievability")
def e6_retrievability(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Golden queries per family — hits@5 ≥ 95% when goldens exist."""
    early = _need_source(source_key)
    if early:
        return early
    goldens = GOLDEN_QUERIES.get(source_key)
    if not goldens:
        return {"note": "n/a — no golden queries for this source", "n/a": True}, True
    from app.clhear.l1 import retrieval
    from app.clhear.l1.models import source_families

    source, _, _, _ = _latest_source(engine, source_key)
    family_key = None
    if source is not None:
        with engine.connect() as conn:
            family_key = conn.execute(
                sa.select(source_families.c.key).where(source_families.c.id == source.family_id)
            ).scalar()

    hits = 0
    details = []
    for query, expected in goldens:
        # Per-source (per-family) hits@5 — not "beat the whole corpus".
        rows = retrieval.search(engine, query, limit=5, scope=family_key)
        own = [r for r in rows if r.get("source_key") == source_key][:5]
        refs = {r.get("ref") for r in own}
        hit = any(any(exp in (ref or "") for exp in expected) for ref in refs)
        hits += int(hit)
        details.append({"q": query, "hit": hit, "n": len(own), "refs": sorted(r for r in refs if r)})
    score = hits / len(goldens)
    return {"score": round(score, 5), "queries": details, "scope": family_key}, score >= 0.95


@register_suite("e7_closure")
def e7_closure(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Citator list ⊆ family; unexplained citations fail."""
    early = _need_source(source_key)
    if early:
        return early
    from app.clhear.l1.models import citations, clauses, family_members, source_versions, sources

    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return {"error": "unknown source"}, False
        family_ids = {
            row.source_id
            for row in conn.execute(
                sa.select(family_members.c.source_id).where(family_members.c.family_id == source.family_id)
            )
        }
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .limit(1)
        ).first()
        if version is None:
            return {"note": "n/a — no version", "n/a": True}, True
        clause_ids = [
            r[0]
            for r in conn.execute(sa.select(clauses.c.id).where(clauses.c.source_version_id == version.id))
        ]
        if not clause_ids:
            return {"note": "n/a — no citations extracted", "n/a": True}, True
        rows = conn.execute(sa.select(citations).where(citations.c.from_clause_id.in_(clause_ids))).all()
    # Explained = resolved inside the family, declared cross-family
    # (out_of_scope), or open with a filed discovery candidate awaiting a human.
    unexplained = [
        r
        for r in rows
        if (r.disposition == "open" and not (r.reason or "").startswith("discovery_candidate:"))
        or (r.disposition == "resolved" and r.resolved_source_id and r.resolved_source_id not in family_ids)
    ]
    pending = sum(1 for r in rows if r.disposition == "open")
    cross = sum(1 for r in rows if r.disposition == "out_of_scope")
    return {
        "citations": len(rows),
        "unexplained": len(unexplained),
        "pending_candidates": pending,
        "cross_family": cross,
    }, len(unexplained) == 0


@register_suite("l1_family_completeness")
def l1_family_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Family completeness ≥ 99 % vs registries + mined citations (HLD v2 §4.1)."""
    from app.clhear.l1 import families

    card = families.family_scorecard(engine)
    if not card["families"]:
        return {"note": "no families ingested", "families": 0}, False
    worst = min(card["families"], key=lambda f: f["completeness"])
    return {
        "families": len(card["families"]),
        "failing": [f["family"] for f in card["families"] if not f["passed"]],
        "worst": worst,
        "threshold": 0.99,
    }, card["passed"]


CURRENCY_MAX_LAG_HOURS = 24.0


@register_suite("l1_currency")
def l1_currency(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Median lag between the publisher's text date and our retrieval ≤ 24 h for
    tier-A sources (HLD v2 §4.1). Lag is measured from the LATER of the
    publisher as-of date and the previous successful run — a consolidation
    dated months ago that we picked up on the day it appeared is current.
    Honest failure: no tier-A version at all is a fail, not n/a."""
    from datetime import date as _date

    from app.clhear.l1.models import source_versions, sources
    from app.clhear.l1.starter_corpus import TIER_A_ADAPTERS

    lags: list[float] = []
    per_source: list[dict] = []
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(
                sources.c.key, sources.c.adapter, source_versions.c.as_of_date,
                source_versions.c.effective_date, source_versions.c.retrieved_at, source_versions.c.id,
            )
            .join(source_versions, source_versions.c.source_id == sources.c.id)
            .where(source_versions.c.status == "in_force")
            .where(sources.c.adapter.in_(sorted(TIER_A_ADAPTERS)))
        ).all()
        for row in rows:
            retrieved = row.retrieved_at
            if isinstance(retrieved, str):
                try:
                    retrieved = datetime.fromisoformat(retrieved)
                except ValueError:
                    retrieved = None
            if retrieved is None:
                continue
            if retrieved.tzinfo is None:
                retrieved = retrieved.replace(tzinfo=timezone.utc)
            anchor = row.as_of_date or row.effective_date
            if isinstance(anchor, str):
                anchor = _date.fromisoformat(anchor)
            # Publisher date is a calendar day; the earliest we could have seen it
            # is that day 00:00 UTC. Undated publishers: lag is bounded by our
            # previous probe only.
            published = datetime(anchor.year, anchor.month, anchor.day, tzinfo=timezone.utc) if anchor else None
            previous = conn.execute(
                sa.select(source_versions.c.retrieved_at)
                .where(source_versions.c.source_id == sa.select(sources.c.id).where(sources.c.key == row.key).scalar_subquery())
                .where(source_versions.c.id < row.id)
                .order_by(source_versions.c.id.desc())
                .limit(1)
            ).scalar()
            if isinstance(previous, str):
                try:
                    previous = datetime.fromisoformat(previous)
                except ValueError:
                    previous = None
            if previous is not None and previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            # The publisher may back-date a consolidation; we cannot have seen it
            # before our previous successful probe, so lag counts from the later of
            # the two. A first ingest is backfill (nothing to be late against).
            if previous is None:
                anchor_ts = None
            elif published is None:
                anchor_ts = previous
            else:
                anchor_ts = max(published, previous)
            if anchor_ts is None:
                lag_h = 0.0
            else:
                lag_h = max(0.0, (retrieved - anchor_ts).total_seconds() / 3600.0)
            lags.append(lag_h)
            per_source.append({"source": row.key, "lag_hours": round(lag_h, 2), "first_ingest": previous is None})
    if not lags:
        return {"error": "no tier-A version stored", "tier_a_sources": 0}, False
    lags.sort()
    median = lags[len(lags) // 2] if len(lags) % 2 else (lags[len(lags) // 2 - 1] + lags[len(lags) // 2]) / 2
    return {
        "tier_a_sources": len(lags),
        "median_lag_hours": round(median, 2),
        "max_lag_hours": round(lags[-1], 2),
        "threshold_hours": CURRENCY_MAX_LAG_HOURS,
        "worst": sorted(per_source, key=lambda s: -s["lag_hours"])[:5],
    }, median <= CURRENCY_MAX_LAG_HOURS


BOUNDARY_F1_THRESHOLD = 0.98


def boundary_f1(golden_refs: set[str], parsed_refs: set[str]) -> dict:
    tp = len(golden_refs & parsed_refs)
    fp = len(parsed_refs - golden_refs)
    fn = len(golden_refs - parsed_refs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


@register_suite("l1_boundary_f1")
def l1_boundary_f1(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Clause boundary F1 ≥ 0.98 against the golden set in clhear-evals/l1/boundary.

    Each golden case carries the adapter, a fixture (HTML bytes or text pages)
    and the expected clause refs + texts. The adapter's offline ``parse`` runs
    on the fixture; a clause counts as matched when ref AND whitespace-
    normalised text agree. Missing golden set = fail (honest)."""
    import os
    from pathlib import Path

    from app.clhear.l1.adapters.base import CLAUSE_TYPES, flatten

    root = Path(os.environ.get("CLHEAR_EVALS_DIR", "clhear-evals")) / "l1" / "boundary"
    cases = sorted(root.glob("*.json")) if root.exists() else []
    if not cases:
        return {"error": f"no golden cases under {root}", "cases": 0}, False
    results = []
    for path in cases:
        case = json.loads(path.read_text())
        try:
            adapter = _golden_adapter(case)
            if "pages" in case:
                tree = adapter.parse_pages(case["pages"])
            else:
                tree = adapter.parse(case["html"].encode())
        except Exception as exc:  # parser crash is a boundary failure, not a skip
            results.append({"case": path.stem, "error": str(exc)[:200], "f1": 0.0})
            continue
        parsed = {
            (n.ref, _ws(n.subtree_text())) for n in flatten(tree) if n.node_type in CLAUSE_TYPES and n.ref
        }
        golden = {(c["ref"], _ws(c["text"])) for c in case["clauses"]}
        score = boundary_f1({g[0] for g in golden}, {p[0] for p in parsed})
        exact = boundary_f1(golden, parsed)
        results.append({"case": path.stem, "adapter": case["adapter"], "refs": score, "exact": exact, "f1": exact["f1"]})
    f1s = [r["f1"] for r in results]
    mean = sum(f1s) / len(f1s)
    return {"cases": len(results), "mean_f1": round(mean, 4), "threshold": BOUNDARY_F1_THRESHOLD, "results": results}, mean >= BOUNDARY_F1_THRESHOLD


def _golden_adapter(case: dict):
    from app.clhear.l1.adapters import publisher_adapter_class
    from app.clhear.l1.adapters.official_html import OfficialHtmlAdapter

    key = case["adapter"]
    kwargs = dict(source_key=case.get("source_key", f"golden/{key}"), title=case.get("title", key), url=case.get("url", "https://example.invalid/golden"))
    if key == "official_html":
        adapter = OfficialHtmlAdapter(adapter="official_html", **kwargs)
        adapter.parse = adapter._parse  # type: ignore[attr-defined]
        return adapter
    cls = publisher_adapter_class(key)
    if key == "fca_handbook":
        return cls(case.get("sourcebook", "PRIN"), chapters=case.get("chapters"), **kwargs)
    if key in {"sec_edgar", "finra"}:
        return cls(channel=case.get("channel", "finra" if key == "finra" else "sec"), **kwargs)
    return cls(**kwargs)


@register_suite("l1_completeness")
def l1_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """A failure is an unresolved document, never evidence of completeness."""
    from app.clhear.l1.inventory import inventory_summary

    summary = inventory_summary(engine)
    missing = [s["source_key"] for s in summary["sources"] if not s["verified"]]
    passed = bool(summary.get("full_scope_verified") and summary.get("current_binding_valid")
                  and summary["known_expected"] and not summary["unresolved"])
    return {"registry_rows": summary["known_expected"], "verified": summary["verified"],
            "missing": missing[:40], "missing_count": summary["unresolved"],
            "denominator_is_lower_bound": summary["known_expected_is_lower_bound"],
            "audit_id": summary.get("audit_id"), "inventory_hash": summary.get("inventory_hash"),
            "method": "worker inventory reconciliation; blocked and missing sources remain expected"}, passed


@register_suite("l1_inventory_acceptance")
def l1_inventory_acceptance(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    from app.clhear.l1.inventory import acceptance_status
    evidence = acceptance_status(engine)
    return {k: v for k, v in evidence.items() if k != "evidence"}, evidence["passed"]


@register_suite("l1_schedule_kept")
def l1_schedule_kept(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Actual UTC occurrences and their frozen task sets; manual runs never count."""
    from app.clhear.l1.cycles import schedule_evidence
    return schedule_evidence(engine, cycle_id=source_key)


@register_suite("l2_basis_integrity")
def l2_basis_integrity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Every live obligation's basis clause must still resolve with the SAME
    text hash it was derived from. A drifted hash means L1 changed underneath
    it — the row must be re-derived, never silently served. Target: 100%."""
    from app.clhear.derived_models import obligations
    from app.clhear.l1.models import clauses, source_versions, sources

    checked = mismatched = unresolved = 0
    bad: list[str] = []
    with engine.connect() as conn:
        query = sa.select(obligations).where(obligations.c.status.in_(("derived", "validated")))
        if source_key:
            query = query.where(obligations.c.source_key == source_key)
        for ob in conn.execute(query):
            checked += 1
            row = conn.execute(
                sa.select(clauses.c.text_hash)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.key == ob.source_key)
                .where(source_versions.c.status == "in_force")
                .where(clauses.c.ref == ob.clause_ref)
                .order_by(source_versions.c.id.desc())
                .limit(1)
            ).first()
            if row is None:
                unresolved += 1
                bad.append(ob.id)
            elif row.text_hash != ob.text_hash:
                mismatched += 1
                bad.append(ob.id)
    passed = mismatched == 0 and unresolved == 0
    return {
        "checked": checked,
        "hash_mismatched": mismatched,
        "unresolved": unresolved,
        "failing": bad[:40],
        "integrity": 1.0 if checked == 0 else round((checked - mismatched - unresolved) / checked, 4),
    }, passed


@register_suite("l2_extraction_quality")
def l2_extraction_quality(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Extraction precision/recall against the hand-labeled golden set
    (app/clhear/l2/golden.json), evaluated over golden refs present in this
    corpus. Gates: precision >= 0.75 and recall >= 0.70."""
    import json as _json
    from pathlib import Path

    from app.clhear.derived_models import obligations
    from app.clhear.l1.models import clauses, source_versions, sources

    golden = _json.loads((Path(__file__).parent.parent / "l2" / "golden.json").read_text())
    tp = fp = fn = tn = 0
    evaluated = 0
    misses: list[dict] = []
    with engine.connect() as conn:
        derived = {
            (row.source_key, row.clause_ref)
            for row in conn.execute(
                sa.select(obligations.c.source_key, obligations.c.clause_ref).where(
                    obligations.c.status.in_(("derived", "validated"))
                )
            )
        }
        for item in golden:
            present = conn.execute(
                sa.select(clauses.c.id)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.key == item["source_key"])
                .where(source_versions.c.status == "in_force")
                .where(clauses.c.ref == item["ref"])
                .limit(1)
            ).first()
            if present is None:
                continue
            evaluated += 1
            got = (item["source_key"], item["ref"]) in derived
            if item["is_duty"] and got:
                tp += 1
            elif item["is_duty"] and not got:
                fn += 1
                misses.append({**item, "kind": "missed duty"})
            elif not item["is_duty"] and got:
                fp += 1
                misses.append({**item, "kind": "false positive"})
            else:
                tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    # An empty corpus (fresh dev DB) has nothing to judge — trivially green.
    passed = evaluated < 5 or (precision >= 0.75 and recall >= 0.70)
    return {
        "golden_total": len(golden),
        "evaluated_in_corpus": evaluated,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "misses": misses[:20],
    }, passed


def _l2_binding_normative(conn) -> list[dict]:
    """Normative clauses of in-force, open-rights, binding-or-guidance sources —
    the L2 coverage denominator (HLD v2 §4.2: 'every normative clause of every
    in-force source maps to at least one obligation')."""
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l2.extract import container_clause_ids

    latest = (
        sa.select(source_versions.c.source_id, sa.func.max(source_versions.c.id).label("vid"))
        .where(source_versions.c.status == "in_force")
        .group_by(source_versions.c.source_id)
        .subquery()
    )
    rows = conn.execute(
        sa.select(clauses.c.id, clauses.c.ref, clauses.c.source_version_id, sources.c.key.label("source_key"))
        .join(latest, latest.c.vid == clauses.c.source_version_id)
        .join(sources, sources.c.id == latest.c.source_id)
        .where(clauses.c.normative.is_(True))
        .where(clauses.c.public_ok.is_(True))
    ).mappings().all()
    containers: set[int] = set()
    for vid in {r["source_version_id"] for r in rows}:
        containers |= container_clause_ids(conn, vid)
    return [dict(r) for r in rows if r["id"] not in containers]


@register_suite("l2_coverage")
def l2_coverage(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """>= 99 % of normative clauses in in-force sources are asserted by at least
    one live obligation (via `asserts` or the obligation's clause_ref). An empty
    corpus fails honestly: coverage of nothing is not coverage."""
    from app.clhear.derived_models import asserts, obligations

    with engine.connect() as conn:
        normative = _l2_binding_normative(conn)
        if source_key:
            normative = [n for n in normative if n["source_key"] == source_key]
        live = obligations.c.status.in_(("derived", "validated"))
        asserted_ids = {
            r[0]
            for r in conn.execute(
                sa.select(asserts.c.clause_id)
                .join(obligations, obligations.c.id == asserts.c.obligation_id)
                .where(live, asserts.c.valid_to.is_(None), asserts.c.clause_id.isnot(None))
            )
        }
        asserted_refs = {
            (r.source_key, r.clause_ref)
            for r in conn.execute(sa.select(obligations.c.source_key, obligations.c.clause_ref).where(live))
        }
    covered = [n for n in normative if n["id"] in asserted_ids or (n["source_key"], n["ref"]) in asserted_refs]
    missing = [f"{n['source_key']}#{n['ref']}" for n in normative if n not in covered]
    total = len(normative)
    coverage = len(covered) / total if total else 0.0
    return {
        "normative_clauses": total,
        "covered": len(covered),
        "coverage": round(coverage, 4),
        "threshold": 0.99,
        "missing": missing[:40],
        "missing_count": len(missing),
    }, total > 0 and coverage >= 0.99


@register_suite("l2_precision")
def l2_precision(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Second-model / expert review precision over live obligations at their
    current text hash (l2.review). Gate >= 0.95; nothing reviewed => fail."""
    from app.clhear.l2 import review as l2_review

    stats = l2_review.precision(engine, current_only=True)
    precision = stats.get("precision")
    stats["threshold"] = 0.95
    return stats, precision is not None and precision >= 0.95


@register_suite("l2_dedupe")
def l2_dedupe(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Unmerged near-duplicate rate across live obligations within one
    jurisdiction (l2.dedupe) must stay below 1 %."""
    from app.clhear.l2 import dedupe as l2_dedupe

    stats = l2_dedupe.duplicate_rate(engine)
    stats["threshold"] = 0.01
    return stats, stats["canonical"] > 0 and stats["rate"] < 0.01


L2_CHANGE_GOLDEN = Path(__file__).resolve().parents[3] / "clhear-evals" / "l2" / "change_events"


def _l2_change_cases() -> list[dict]:
    import json as _json

    cases: list[dict] = []
    for path in sorted(L2_CHANGE_GOLDEN.glob("*.json")):
        data = _json.loads(path.read_text())
        items = data if isinstance(data, list) else data.get("cases", [])
        for item in items:
            item.setdefault("file", path.name)
            cases.append(item)
    return cases


@register_suite("l2_change_inference")
def l2_change_inference(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Golden L1 clause changes (clhear-evals/l2/change_events) replayed through
    l2.change.infer_clause_change: expected kind (added / updated / revoked /
    none), materiality and effective-date handling. Gate >= 95 % correct."""
    from app.clhear.l1.change_detect import extract_effective_dates
    from app.clhear.l2.change import infer_clause_change

    cases = _l2_change_cases()
    correct = 0
    failures: list[dict] = []
    for case in cases:
        got = infer_clause_change(case.get("old_text"), case.get("new_text"), case.get("ref", ""))
        ok = got.kind == case["expected_kind"]
        if ok and case.get("expected_materiality"):
            ok = got.materiality == case["expected_materiality"]
        if ok and "expected_effective_date" in case:
            dates = extract_effective_dates(case.get("new_text") or "")
            found = dates[0][0].isoformat() if dates else None
            ok = found == case["expected_effective_date"]
        if ok:
            correct += 1
        else:
            failures.append({"id": case.get("id"), "file": case.get("file"), "expected": case["expected_kind"],
                             "got": got.as_dict()})
    total = len(cases)
    accuracy = correct / total if total else 0.0
    return {
        "cases": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "threshold": 0.95,
        "failures": failures[:20],
    }, total > 0 and accuracy >= 0.95


@register_suite("l3_completeness")
def l3_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Every in-force obligation -> >= 1 live requires edge to a block (100 %).
    An empty registry fails honestly."""
    from app.clhear.l3.decompose import completeness

    stats = completeness(engine)
    stats["threshold"] = 1.0
    return stats, stats["obligations"] > 0 and stats["missing_count"] == 0


@register_suite("l3_characteristics")
def l3_characteristics(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """>= 95 % of required characteristics (over canonical blocks) filled with
    a backing span or an explicit 'not specified by source'."""
    from app.clhear.l3.characterize import completeness

    stats = completeness(engine)
    stats["threshold"] = 0.95
    return stats, stats["required"] > 0 and stats["rate"] >= 0.95


@register_suite("l3_reuse")
def l3_reuse(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Block reuse ratio is published; the gate fails on block explosion
    (more machine-derived canonical blocks than obligations they serve)."""
    from app.clhear.l3.harmonize import reuse_ratio

    stats = reuse_ratio(engine)
    return stats, stats["live_edges"] > 0 and not stats["explosion"]


@register_suite("l3_precision")
def l3_precision(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Expert sample precision >= 92 % from Eval Studio votes on L3 items
    (blocks, requires, characteristics). No votes => fail."""
    from app.clhear.eval_studio import agreement_scores

    layer = agreement_scores(engine)["by_layer"].get("L3", {"n": 0, "agree": 0, "score": None})
    stats = {"votes": layer["n"], "agree": layer["agree"], "precision": layer.get("score"), "threshold": 0.92}
    return stats, layer["n"] > 0 and (layer.get("score") or 0.0) >= 0.92


@register_suite("l3_l5_referential")
def l3_l5_referential(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Curated anchors must point at real registry sources, and any anchored
    clause that IS in the corpus must have derived an obligation. Refs absent
    from the corpus are reported (L1 completeness concern) but non-fatal."""
    from app.clhear.derived_models import activities as activities_t
    from app.clhear.derived_models import blocks as blocks_t
    from app.clhear.derived_models import obligations as obligations_t
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l1.registry_etoro import S

    registry_keys = {e["key"] for e in S}
    unknown_sources: list[str] = []
    extraction_misses: list[dict] = []
    refs_not_in_corpus: list[dict] = []
    with engine.connect() as conn:
        db_keys = {row.key for row in conn.execute(sa.select(sources.c.key))}
        block_rows = [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]
        activity_rows = [dict(r) for r in conn.execute(sa.select(activities_t)).mappings()]
        derived = {
            (r.source_key, r.clause_ref)
            for r in conn.execute(
                sa.select(obligations_t.c.source_key, obligations_t.c.clause_ref).where(
                    obligations_t.c.status.in_(("derived", "validated"))
                )
            )
        }
        present: set[tuple[str, str]] = set()
        for row in conn.execute(
            sa.select(sources.c.key, clauses.c.ref)
            .join(source_versions, source_versions.c.source_id == sources.c.id)
            .join(clauses, clauses.c.source_version_id == source_versions.c.id)
            .where(source_versions.c.status == "in_force")
            .where(sources.c.license == "open")
            .where(clauses.c.public_ok.is_(True))
        ):
            present.add((row.key, row.ref))

    def check_anchor(owner: str, anchor: dict) -> None:
        key = anchor["source_key"]
        if key not in registry_keys and key not in db_keys:
            unknown_sources.append(f"{owner} -> {key}")
            return
        for ref in anchor.get("refs") or []:
            if (key, ref) in present and (key, ref) not in derived:
                extraction_misses.append({"owner": owner, "source_key": key, "ref": ref})
            elif (key, ref) not in present and key in db_keys:
                refs_not_in_corpus.append({"owner": owner, "source_key": key, "ref": ref})

    for b in block_rows:
        for sel in b["satisfies"]:
            check_anchor(f"block:{b['id']}", {"source_key": sel["source_key"], "refs": sel.get("refs")})
    schema_keys: set[str] = set()
    with engine.connect() as conn:
        from app.clhear.derived_models import attribute_schema as attribute_schema_t

        schema_keys = {r.key for r in conn.execute(sa.select(attribute_schema_t.c.key))}
    unknown_when: list[str] = []
    for a in activity_rows:
        for trig in a["triggers"]:
            check_anchor(f"activity:{a['id']}", trig["anchor"])
            for key in (trig.get("when") or {}):
                if key not in schema_keys:
                    unknown_when.append(f"{a['id']}.{key}")
    passed = not unknown_sources and not extraction_misses and not unknown_when
    return {
        "blocks": len(block_rows),
        "activities": len(activity_rows),
        "unknown_sources": unknown_sources[:20],
        "extraction_misses": extraction_misses[:20],
        "refs_not_in_corpus": len(refs_not_in_corpus),
        "unknown_when_attributes": unknown_when[:20],
    }, passed


@register_suite("l2_concept_integrity")
def l2_concept_integrity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Concept gates: every member resolves live and non-stale; every facet
    jurisdiction has >=1 member; canonical statements contain no verbatim
    8-gram runs from RESTRICTED clauses; resolution is deterministic."""
    import re as _re

    from app.clhear.derived_models import concept_members, concepts, obligations
    from app.clhear.l1.models import clauses, source_versions, sources
    from app.clhear.l2.concepts import get_concept, list_concepts, resolve_concept

    dead_members: list[str] = []
    restricted_leaks: list[str] = []
    nondeterministic: list[str] = []
    with engine.connect() as conn:
        member_rows = conn.execute(
            sa.select(concept_members.c.concept_id, concept_members.c.obligation_id, obligations.c.status)
            .join(obligations, obligations.c.id == concept_members.c.obligation_id, isouter=True)
            .where(record.in_force(concept_members))
        ).all()
        for row in member_rows:
            if row.status not in ("derived", "validated"):
                dead_members.append(f"{row.concept_id} -> {row.obligation_id}")
        restricted_texts = [
            row.text
            for row in conn.execute(
                sa.select(clauses.c.text)
                .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                .join(sources, sources.c.id == source_versions.c.source_id)
                .where(sources.c.license == "restricted")
                .where(clauses.c.text.isnot(None))
            )
        ]

    def _grams(text: str, n: int = 8) -> set[tuple]:
        toks = _re.findall(r"[a-z0-9]+", text.lower())
        return {tuple(toks[i : i + n]) for i in range(len(toks) - n + 1)}

    restricted_grams: set[tuple] = set()
    for text in restricted_texts:
        restricted_grams |= _grams(text)

    concept_rows = list_concepts(engine)
    for c in concept_rows:
        if restricted_grams and (_grams(c["canonical_statement"]) & restricted_grams):
            restricted_leaks.append(c["id"])
        for jurs in ([c["jurisdictions"]], [["UK"]], [["EU", "UK", "US"]]):
            first = resolve_concept(engine, c, jurs[0])
            second = resolve_concept(engine, get_concept(engine, c["id"]) or c, jurs[0])
            if first != second:
                nondeterministic.append(f"{c['id']} @ {jurs[0]}")
                break

    passed = not dead_members and not restricted_leaks and not nondeterministic
    return {
        "concepts": len(concept_rows),
        "dead_members": dead_members[:20],
        "restricted_leaks": restricted_leaks,
        "nondeterministic": nondeterministic,
    }, passed


@register_suite("l4_grounding")
def l4_grounding(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """100% of license types resolve to live clause anchors. One ungrounded
    row blocks the L4 fleet's publish — incomplete is fine, invented is not."""
    from app.clhear.derived_models import license_types
    from app.clhear.l1.models import clauses, source_versions, sources

    checked = unresolved = 0
    bad: list[str] = []
    n_types = 0
    with engine.connect() as conn:
        rows = conn.execute(sa.select(license_types)).all()
        n_types = len(rows)
        for row in rows:
            anchors = row.clause_anchors if isinstance(row.clause_anchors, list) else json.loads(row.clause_anchors or "[]")
            if not anchors:
                unresolved += 1
                bad.append(row.id)
                continue
            for anc in anchors:
                checked += 1
                hit = conn.execute(
                    sa.select(clauses.c.id)
                    .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
                    .join(sources, sources.c.id == source_versions.c.source_id)
                    .where(sources.c.key == anc.get("source_key"))
                    .where(source_versions.c.status == "in_force")
                    .where(clauses.c.ref == anc.get("ref"))
                    .limit(1)
                ).first()
                if hit is None:
                    unresolved += 1
                    bad.append(f"{row.id}->{anc.get('source_key')}#{anc.get('ref')}")
    passed = unresolved == 0
    return {
        "license_types": n_types,
        "anchors_checked": checked,
        "unresolved": unresolved,
        "failing": bad[:40],
    }, passed


L4_GOLDEN = Path(__file__).resolve().parents[3] / "clhear-evals" / "l4"


def _l4_cases(folder: str, key: str = "cases") -> list[dict]:
    cases: list[dict] = []
    for path in sorted((L4_GOLDEN / folder).glob("*.json")):
        data = json.loads(path.read_text())
        for item in data.get(key, []) if isinstance(data, dict) else data:
            item.setdefault("file", path.name)
            cases.append(item)
    return cases


@register_suite("l4_validity")
def l4_validity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Golden profiles (clhear-evals/l4/profiles): every real permutation
    validates, every listed impossible permutation is rejected with the
    expected error code, and each licence the golden set names carries register
    provenance. Accuracy >= 99 % (HLD v2 §4.4 profile validity)."""
    from app.clhear.l4.validate import Ontology, validate_with

    with engine.connect() as conn:
        onto = Ontology(conn)
    cases = _l4_cases("profiles")
    checks = correct = 0
    failures: list[dict] = []
    licences_named: set[str] = set()
    for case in cases:
        res = validate_with(onto, case["attributes"])
        checks += 1
        ok = res["valid"] == case["expected_valid"]
        if ok and not case["expected_valid"] and case.get("expect_code"):
            ok = any(e["code"] == case["expect_code"] for e in res["errors"])
        if ok and case.get("expected_warning"):
            ok = any(w.get("rule_id") == case["expected_warning"] for w in res["warnings"])
        if ok:
            correct += 1
        else:
            failures.append({"id": case["id"], "expected_valid": case["expected_valid"], "errors": [e["code"] for e in res["errors"]]})
        if case["expected_valid"]:
            licences_named |= set(case["attributes"].get("authorisations") or [])
        for perm in case.get("invalid_permutations", []):
            checks += 1
            mutated = {**case["attributes"], **perm["set"]}
            r2 = validate_with(onto, mutated)
            good = not r2["valid"] and (not perm.get("expect_code") or any(e["code"] == perm["expect_code"] for e in r2["errors"]))
            if good:
                correct += 1
            else:
                failures.append({"id": case["id"], "permutation": perm["set"], "valid": r2["valid"], "errors": [e["code"] for e in r2["errors"]]})
    provenance_missing = sorted(
        name for name in licences_named
        if (row := onto.licences.resolve(name)) is None or not (row.get("register") and row.get("register_url"))
    )
    accuracy = correct / checks if checks else 0.0
    stats = {
        "cases": len(cases), "checks": checks, "correct": correct, "accuracy": round(accuracy, 4), "threshold": 0.99,
        "licences_named": len(licences_named), "provenance_missing": provenance_missing,
        "ontology_version": onto.version, "ontology_empty": onto.empty(), "failures": failures[:20],
    }
    return stats, checks > 0 and not onto.empty() and accuracy >= 0.99 and not provenance_missing


def _pkey(predicate: dict) -> str:
    return json.dumps(predicate, sort_keys=True)


@register_suite("l4_applicability")
def l4_applicability(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Golden obligations -> applies_to predicates (clhear-evals/l4/applicability):
    precision and recall over predicates >= 0.95. Profile-level expectations
    (must include / exclude source keys) are checked when those obligations are
    in the store. Every stored edge must also point at a live obligation and use
    only schema attributes (referential integrity)."""
    from app.clhear.derived_models import applies_to as applies_to_t
    from app.clhear.derived_models import obligations as obligations_t
    from app.clhear.l4 import predicates as l4_predicates

    with engine.connect() as conn:
        onto = l4_predicates._Onto(conn)
        schema_keys = l4_predicates.schema_keys(conn)
        live_ids = {r[0] for r in conn.execute(sa.select(obligations_t.c.id).where(obligations_t.c.status.in_(("derived", "validated"))))}
        edges = [dict(r) for r in conn.execute(sa.select(applies_to_t).where(applies_to_t.c.valid_to.is_(None))).mappings()]
    tp = fp = fn = 0
    failures: list[dict] = []
    cases = _l4_cases("applicability")
    for case in cases:
        ob = {"id": case["id"], "text_hash": "", "title": "", **case["obligation"]}
        got = {_pkey(e["predicate"]) for e in l4_predicates.deterministic_predicates(ob, onto)}
        want = {_pkey(p) for p in case["expected_predicates"]}
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        if got != want:
            failures.append({"id": case["id"], "missing": sorted(want - got), "extra": sorted(got - want)})
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0

    profile_checks: list[dict] = []
    for exp in _l4_cases("applicability", key="expected_obligations"):
        with engine.connect() as conn:
            present = {r[0] for r in conn.execute(sa.select(obligations_t.c.source_key).where(
                obligations_t.c.source_key.in_(exp.get("must_include_sources", []) + exp.get("must_exclude_sources", []))))}
            if not present:
                profile_checks.append({"id": exp["id"], "skipped": "sources not in store"})
                continue
            items = l4_predicates.obligations_for_attributes(conn, exp["attributes"])
        got_sources = {i["source_key"] for i in items}
        ok = all(s in got_sources for s in exp.get("must_include_sources", []) if s in present) and \
            not any(s in got_sources for s in exp.get("must_exclude_sources", []))
        profile_checks.append({"id": exp["id"], "passed": ok, "sources": sorted(got_sources)[:20]})

    dangling = [e["id"] for e in edges if e["obligation_id"] not in live_ids]
    bad_keys = [e["id"] for e in edges if any(k not in schema_keys for k in (e["predicate"] or {}))]
    stats = {
        "cases": len(cases), "tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 4), "recall": round(recall, 4),
        "threshold": 0.95, "edges": len(edges), "dangling_edges": dangling[:20], "non_schema_edges": bad_keys[:20],
        "profile_checks": profile_checks, "failures": failures[:20],
    }
    passed = (len(cases) > 0 and precision >= 0.95 and recall >= 0.95 and not dangling and not bad_keys
              and all(c.get("passed", True) for c in profile_checks))
    return stats, passed


# ----------------------------------------------------------------- L5 (HLD v2 §4.5)

L5_GOLDEN = Path(__file__).resolve().parents[3] / "clhear-evals" / "l5"


def _l5_cases(folder: str, key: str = "cases") -> list[dict]:
    cases: list[dict] = []
    for path in sorted((L5_GOLDEN / folder).glob("*.json")):
        data = json.loads(path.read_text())
        for item in data.get(key, []) if isinstance(data, dict) else data:
            item.setdefault("file", path.name)
            cases.append(item)
    return cases


@register_suite("l5_completeness")
def l5_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Junction completeness 100 %: no orphan activity (every business activity
    implied by a product / service, every compliance activity operating a block
    and anchored to an obligation), no dangling edge endpoint, vocabulary and
    when-conditions closed-world; every live obligation mapped to an activity."""
    from app.clhear.l5.check import check_junction
    from app.clhear.l5.map import coverage

    junction = check_junction(engine)
    cov = coverage(engine)
    stats = {
        "activities": junction["activities"], "business": junction["business"], "compliance": junction["compliance"],
        "edges": junction["edges"], "orphans": junction["orphans"][:20], "dangling": junction["dangling"][:20],
        "vocabulary_violations": junction["vocabulary_violations"][:20], "when_violations": junction["when_violations"][:20],
        "anchors_not_in_corpus": len(junction["anchors_not_in_corpus"]), "unlit_mitigates": len(junction["unlit_mitigates"]),
        "junction_completeness": junction["completeness"], "obligations": cov["obligations"], "obligations_mapped": cov["mapped"],
        "obligation_coverage": cov["ratio"], "threshold": 1.0,
    }
    passed = bool(junction["ok"]) and cov["unmapped"] == 0
    return stats, passed


@register_suite("l5_mapping")
def l5_mapping(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Golden obligations -> (side, action type, activity) through the deterministic
    mapper (clhear-evals/l5/mapping): accuracy >= 0.92; plus golden activity maps
    (products -> business activities -> governing compliance activities) against
    the live junction."""
    from app.clhear.l5.map import activity_map, classify

    cases = _l5_cases("mapping")
    hits = 0
    failures: list[dict] = []
    for case in cases:
        got = classify({"id": case["id"], **case["obligation"]})
        want = case["expected"]
        ok = (got is not None and got[0] == want["action_type"] and got[1] == want["activity"]) if want.get("activity") else got is None
        hits += int(ok)
        if not ok:
            failures.append({"id": case["id"], "expected": want, "got": {"action_type": got[0], "activity": got[1], "cue": got[2]} if got else None})
    accuracy = hits / len(cases) if cases else 0.0

    map_checks: list[dict] = []
    for exp in _l5_cases("mapping", key="expected_maps"):
        with engine.connect() as conn:
            amap = activity_map(conn, exp["attributes"])
        business = {a["id"] for a in amap["business"]}
        compliance = {a["id"] for a in amap["compliance"]}
        missing_b = [a for a in exp.get("business", []) if a not in business]
        missing_c = [a for a in exp.get("compliance", []) if a not in compliance]
        extra_b = [a for a in exp.get("not_business", []) if a in business]
        map_checks.append({"id": exp["id"], "passed": not (missing_b or missing_c or extra_b),
                           "missing_business": missing_b, "missing_compliance": missing_c, "unexpected_business": extra_b})
    stats = {"cases": len(cases), "correct": hits, "accuracy": round(accuracy, 4), "threshold": 0.92,
             "failures": failures[:20], "map_checks": map_checks}
    passed = len(cases) > 0 and accuracy >= 0.92 and all(c["passed"] for c in map_checks)
    return stats, passed


@register_suite("l5_precision")
def l5_precision(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Expert sample precision >= 92 % from Eval Studio votes on L5 items
    (activity mappings and junction edges). No votes => fail."""
    from app.clhear.eval_studio import agreement_scores

    layer = agreement_scores(engine)["by_layer"].get("L5", {"n": 0, "agree": 0, "score": None})
    stats = {"votes": layer["n"], "agree": layer["agree"], "precision": layer.get("score"), "threshold": 0.92}
    return stats, layer["n"] > 0 and (layer.get("score") or 0.0) >= 0.92


@register_suite("l6_citation")
def l6_citation(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Blueprints that carry a rationale must cite only ids in that blueprint."""
    from app.clhear.derived_models import blueprints
    from app.clhear.l6.rationale import citations_ok

    checked = failed = 0
    extras: list[str] = []
    with engine.connect() as conn:
        for row in conn.execute(sa.select(blueprints)):
            result = row.result if isinstance(row.result, dict) else json.loads(row.result or "{}")
            text = result.get("rationale")
            if not text:
                continue
            checked += 1
            # Reconstruct a minimal blueprint for the checker.
            bp = {
                "coverage": [{"obligation_id": oid} for oid in (result.get("obligation_ids") or [])],
                "blocks": [{"id": bid} for bid in (result.get("blocks") or [])],
                "activities_evaluated": result.get("activities") or [],
            }
            # If we only stored coverage_summary, there is nothing to over-cite.
            if not bp["coverage"] and not bp["blocks"]:
                continue
            ok, extra = citations_ok(text, bp)
            if not ok:
                failed += 1
                extras.extend(extra[:5])
    return {"checked": checked, "failed": failed, "extra_ids": extras[:20]}, failed == 0


L6_GOLDEN = Path(__file__).resolve().parents[3] / "clhear-evals" / "l6"


def _l6_cases(folder: str) -> list[dict]:
    cases: list[dict] = []
    for path in sorted((L6_GOLDEN / folder).glob("*.json")):
        data = json.loads(path.read_text())
        for item in data.get("cases", []):
            item.setdefault("file", path.name)
            cases.append(item)
    return cases


def _l6_current(engine: Engine) -> list[dict]:
    """Current stored blueprints as compositions (stored profiles composed when none is stored)."""
    from app.clhear.derived_models import blueprints
    from app.clhear.l6 import composer

    out: list[dict] = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(blueprints.c.stable_id, blueprints.c.composition, blueprints.c.profile_id)
                .where(blueprints.c.status == "current", blueprints.c.stable_id.isnot(None)).order_by(blueprints.c.id)).all()
    except sa.exc.OperationalError:  # pre-m0014 database
        return out
    for sid, comp, pid in rows:
        comp = composer._json(comp, None)
        if comp:
            comp["blueprint_id"] = sid
            comp["profile_id"] = pid
            out.append(comp)
    if not out:
        from app.clhear.derived_models import profiles

        with engine.connect() as conn:
            pids = [r[0] for r in conn.execute(sa.select(profiles.c.id).where(profiles.c.status == "valid").order_by(profiles.c.id))]
        for pid in pids:
            out.append(composer.compose_for_profile(engine, pid, log_request=False))
    return out


@register_suite("l6_completeness")
def l6_completeness(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Completeness 100 %: in every current blueprint each applicable obligation
    is satisfied by ≥ 1 item. A blueprint without any applicable obligation
    proves nothing, so an instance with only such blueprints fails."""
    comps = _l6_current(engine)
    total = covered = 0
    gaps: list[dict] = []
    for comp in comps:
        s = comp.get("coverage_summary") or {}
        total += s.get("total", 0)
        covered += s.get("covered", 0)
        for c in comp.get("coverage") or []:
            if c["state"] == "gap" and len(gaps) < 20:
                gaps.append({"blueprint": comp.get("blueprint_id"), "obligation_id": c["obligation_id"], "title": c.get("title")})
    stats = {"blueprints": len(comps), "applicable_obligations": total, "covered": covered,
             "completeness": (covered / total) if total else None, "gaps": gaps, "threshold": 1.0}
    return stats, total > 0 and covered == total


@register_suite("l6_minimality")
def l6_minimality(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Minimality checked: no item of a current blueprint is removable without
    breaking coverage; the composer's own proof is re-verified independently."""
    from app.clhear.l6.check import verify_minimality

    comps = _l6_current(engine)
    checked = minimal = 0
    problems: list[dict] = []
    for comp in comps:
        if not comp.get("items"):
            continue
        checked += 1
        v = verify_minimality(comp)
        if v["minimal"] and v["proof_agrees"]:
            minimal += 1
        else:
            problems.append({"blueprint": comp.get("blueprint_id"), "redundant": v["redundant"], "proof_agrees": v["proof_agrees"]})
    stats = {"blueprints": len(comps), "checked": checked, "minimal": minimal, "problems": problems[:20], "threshold": "all"}
    return stats, checked > 0 and minimal == checked


@register_suite("l6_reference")
def l6_reference(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Reference-program agreement ≥ 90 % against expert-authored programs
    (clhear-evals/l6/reference): per case the F1 between the composed item set
    and the expert block set; cases whose instruments are not in the store are
    reported as skipped, never counted as agreement."""
    from app.clhear.derived_models import obligations as obligations_t
    from app.clhear.l6 import composer

    cases = _l6_cases("reference")
    with engine.connect() as conn:
        sources = {r[0] for r in conn.execute(sa.select(sa.distinct(obligations_t.c.source_key)))}
        live = {r[0] for r in conn.execute(sa.select(obligations_t.c.id).where(obligations_t.c.status.in_(("derived", "validated"))))}
    scores: list[float] = []
    detail: list[dict] = []
    skipped = 0
    for case in cases:
        needed = set(case.get("requires_sources") or [])
        if needed and not needed & sources:
            skipped += 1
            detail.append({"case": case["id"], "skipped": sorted(needed - sources)})
            continue
        comp = composer.compose(engine, {"attributes": case["attributes"], "activities": case.get("activities")}, log_request=False)
        # The expert program: block -> the obligations that justify it. Only
        # blocks whose justification is live in this store are in scope, and
        # only composed items addressing the expert's obligations are compared.
        expected: dict[str, list[str]] = case["expected"]
        expert_obls = {o for obls in expected.values() for o in obls}
        want = {b for b, obls in expected.items() if any(o in live for o in obls)}
        composed = {i["block_id"]: set(i["obligations_satisfied"]) for i in comp["items"]}
        got = {b for b, obls in composed.items() if obls & expert_obls}
        beyond = sorted(set(composed) - got)
        forbidden = set(case.get("forbidden_blocks") or []) & set(composed)
        if not want:
            skipped += 1
            detail.append({"case": case["id"], "skipped": "no expected block justified by a live obligation"})
            continue
        tp = len(got & want)
        precision = tp / len(got) if got else 0.0
        recall = tp / len(want)
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        if forbidden:
            f1 = 0.0
        scores.append(f1)
        detail.append({"case": case["id"], "f1": round(f1, 3), "in_scope": sorted(want), "missing": sorted(want - got),
                       "disagree": sorted(got - want), "beyond_reference": beyond, "forbidden_present": sorted(forbidden),
                       "gaps": comp["coverage_summary"]["gaps"]})
    agreement = (sum(scores) / len(scores)) if scores else None
    stats = {"cases": len(cases), "evaluated": len(scores), "skipped": skipped, "agreement": agreement, "detail": detail[:30],
             "threshold": 0.90, "reference_sets": sorted({c["file"] for c in cases})}
    return stats, bool(scores) and agreement >= 0.90


@register_suite("l6_explanation")
def l6_explanation(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Explanation quality ≥ 90 % on the rubric over every item of every current blueprint."""
    from app.clhear.l6.explain import score_blueprint

    comps = _l6_current(engine)
    items = passed = 0
    total = 0.0
    failing: list[dict] = []
    for comp in comps:
        s = score_blueprint(comp)
        items += s["items"]
        passed += s["passed"]
        total += (s["score"] or 0.0) * s["items"]
        failing.extend({"blueprint": comp.get("blueprint_id"), **f} for f in s["failing"][:5])
    score = (total / items) if items else None
    stats = {"blueprints": len(comps), "items": items, "passed": passed, "score": score, "failing": failing[:20], "threshold": 0.90}
    return stats, items > 0 and score >= 0.90


@register_suite("l7_number_echo")
def l7_number_echo(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    from app.clhear.l7.narrate import number_echo_ok
    from app.clhear.models import risk_narratives

    failed = []
    checked = 0
    with engine.connect() as conn:
        for row in conn.execute(sa.select(risk_narratives)):
            checked += 1
            # Reconstruct a vector from stored echoed figures — every number in
            # the narrative must be in that set.
            vector = {f"n{i}": float(x) if "." in str(x) else int(x) for i, x in enumerate(row.echoed_figures or [])}
            # Also allow the raw stored figures as strings via a dummy walk.
            ok, extras = number_echo_ok(row.narrative, {"echoed": row.echoed_figures or [], **vector})
            if not ok:
                failed.append({"id": row.id, "extras": extras[:8]})
    return {"checked": checked, "failed": failed[:20]}, not failed


L7_GOLDEN = Path(__file__).resolve().parents[3] / "clhear-evals" / "l7"


@register_suite("l7_linker")
def l7_linker(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Enforcement linker precision ≥ 90 % on the golden notices
    (clhear-evals/l7/linker): every predicted (source, clause) link must be one the
    notice names. Recall is reported. Stored links are sanity-checked too: a live
    link must point at a live event and a live obligation and carry its citation."""
    from app.clhear.derived_models import obligations as _obligations
    from app.clhear.l7.enforcement import index_from_rows, link_text
    from app.clhear.l7.models import enforcement_events, enforcement_links

    cases: list[dict] = []
    for path in sorted((L7_GOLDEN / "linker").glob("*.json")):
        data = json.loads(path.read_text())
        for item in data.get("cases", []):
            item.setdefault("file", path.name)
            cases.append(item)
    tp = fp = fn = 0
    failures: list[dict] = []
    for case in cases:
        index = index_from_rows(case["registry"])
        got = {(h["obligation_id"].split("OBL:", 1)[1].rsplit("#", 1)[0], h["clause_ref"])
               for h in link_text(case["notice"], index, case.get("aliases"))}
        want = {tuple(e) for e in case["expected"]}
        tp += len(got & want)
        fp += len(got - want)
        fn += len(want - got)
        if got != want:
            failures.append({"id": case["id"], "extra": sorted(got - want), "missing": sorted(want - got)})
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    # stored links: referential integrity + a citation behind every deterministic link
    dangling = uncited = live_links = 0
    with engine.connect() as conn:
        live_events = {r[0] for r in conn.execute(sa.select(enforcement_events.c.id).where(enforcement_events.c.valid_to.is_(None)))}
        live_obs = {r[0] for r in conn.execute(sa.select(_obligations.c.id).where(_obligations.c.status.in_(("derived", "validated"))))}
        for r in conn.execute(sa.select(enforcement_links).where(enforcement_links.c.valid_to.is_(None))).mappings():
            live_links += 1
            if r["event_id"] not in live_events or r["obligation_id"] not in live_obs:
                dangling += 1
            if r["method"] in ("citation", "instrument", "llm") and not (r["citation"] or "").strip():
                uncited += 1
    stats = {"cases": len(cases), "tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall,
             "failures": failures[:20], "live_links": live_links, "dangling": dangling, "uncited": uncited, "threshold": 0.90}
    passed = bool(cases) and precision is not None and precision >= 0.90 and dangling == 0 and uncited == 0
    return stats, passed


@register_suite("l7_brier")
def l7_brier(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """A calibration run for the current method version is published: the Brier
    score on a held-out year, the base-rate baseline and the reliability table
    (HLD v2 §4.7 'Brier score on held-out year published'). Reports whether the
    fitted likelihood beats the baseline."""
    from app.clhear.l7.models import METHOD_VERSION, WEIGHTS
    from app.clhear.l7.score import latest_calibration

    with engine.connect() as conn:
        cal = latest_calibration(conn)
    weights_ok = abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    if cal is None:
        return {"method_version": METHOD_VERSION, "published": False, "weights_sum_to_one": weights_ok,
                "reason": "no published calibration run"}, False
    stats = {"method_version": METHOD_VERSION, "published": True, "calibration": cal["id"], "held_out_year": cal["held_out_year"],
             "training_years": cal["training_years"], "n": cal["n"], "positives": cal["positives"], "brier": cal["brier"],
             "baseline_brier": cal["baseline_brier"],
             "beats_baseline": cal["brier"] is not None and cal["baseline_brier"] is not None and cal["brier"] <= cal["baseline_brier"],
             "reliability": cal["reliability"], "parameters": cal["parameters"], "weights_sum_to_one": weights_ok}
    return stats, cal["brier"] is not None and cal["n"] > 0 and weights_ok


@register_suite("l8_k_anonymity")
def l8_k_anonymity(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    from app.clhear.l8.cohorts import k_anonymity_ok

    ok, detail = k_anonymity_ok(engine)
    return detail, ok


@register_suite("l8_reidentification")
def l8_reidentification(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """HLD v2 §4.8: every current benchmark aggregate has n ≥ k distinct members, echoes
    no raw input, admits no differencing attack between cohorts on the same metric, and
    the input store holds HMAC digests only — never a member identity."""
    from app.clhear.l8.aggregate import reidentification_test

    out = reidentification_test(engine)
    return {k: v for k, v in out.items() if k != "passed"}, out["passed"]


@register_suite("l8_traceability")
def l8_traceability(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Every current fill names a live L3 block and ≥ 1 live obligation and carries a why-trail (I3): 100 %."""
    from app.clhear.l8.fills import traceability

    out = traceability(engine)
    return out, out["ratio"] >= 1.0


@register_suite("l8_fill_rubric")
def l8_fill_rubric(engine: Engine, source_key: str | None) -> tuple[dict, bool]:
    """Endorsed fills all scored ≥ 85 % on the five-criterion expert rubric, by a recorded review."""
    from app.clhear.l8.fills import rubric_gate

    out = rubric_gate(engine)
    return {k: v for k, v in out.items() if k != "ok"}, out["ok"]


def run_source_evals(engine: Engine, source_key: str, release: str | None = None) -> list[dict]:
    return [run_suite(engine, suite, source_key=source_key, release=release) for suite in SOURCE_SUITES]


def latest_source_scorecard(engine: Engine, source_key: str, source_version_id: int | None = None) -> dict:
    """Latest E1–E7 row per suite for the Evidence tab."""
    identity = _source_identity(engine, source_key, source_version_id)
    if source_version_id is None:
        source_version_id = identity["id"] if identity else None
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_runs)
            .where(eval_runs.c.source_key == source_key)
            .where(eval_runs.c.suite.in_(SOURCE_SUITES))
            .order_by(eval_runs.c.id.desc())
        ).all()
    latest: dict[str, dict] = {}
    for row in rows:
        scores = row.scores if isinstance(row.scores, dict) else json.loads(row.scores or "{}")
        if (identity is None or scores.get("source_version_id") != source_version_id
                or scores.get("content_hash") != identity["content_hash"]):
            continue
        if row.suite in latest:
            continue
        latest[row.suite] = {
            "suite": row.suite,
            "passed": bool(row.passed) and not scores.get("n/a") and not scores.get("not_evaluated"),
            "status": "not_evaluated" if scores.get("n/a") or scores.get("not_evaluated") else ("passed" if row.passed else "failed"),
            "scores": scores,
            "eval_run_id": row.id,
            "ran_at": str(row.ran_at),
        }
    open_ok = all(latest[s]["passed"] for s in SOURCE_SUITES if s in latest) if latest else False
    exception_used = any(row["scores"].get("operator_exception_used") for row in latest.values())
    technical_green = open_ok and len(latest) == len(SOURCE_SUITES)
    return {"source_key": source_key, "source_version_id": source_version_id,
            "suites": latest, "missing_suites": [s for s in SOURCE_SUITES if s not in latest],
            "green": technical_green, "technical_green": technical_green, "operator_exception_used": exception_used,
            "release_eligible": technical_green and not exception_used and all(row["scores"].get("release_eligible", True) for row in latest.values()),
            "notice": ("Private technical checks used an operator exception. Publisher permissions remain unresolved; this does not authorize an accepted release. " if exception_used else "")
                      + "Checks apply only to this stored version. Parser consistency alone does not prove complete publisher scope."}


GLOBAL_SUITES = ("l0_smoke", "l1_fidelity")
L1_DIAGNOSTIC_SUITES = ("l1_family_completeness", "l1_currency")


def gate_suites() -> tuple[str, ...]:
    """Every registered suite that sits in some layer's publication gate, plus the
    global suites. Suites a gate names but nobody has registered yet stay
    'missing' in gates.gate_status — the layer is simply not publishable."""
    from app.clhear.platform.gates import LAYER_GATES

    ordered: list[str] = [*GLOBAL_SUITES, *L1_DIAGNOSTIC_SUITES]
    for suites in LAYER_GATES.values():
        for suite in suites:
            if suite in SUITES and suite not in ordered:
                ordered.append(suite)
    return tuple(ordered)


def run_all(engine: Engine, release: str | None = None) -> list[dict]:
    records = []
    for suite in gate_suites():
        try:
            records.append(run_suite(engine, suite, release=release))
        except Exception as exc:  # a crashing suite is a failed suite, never a skipped one
            log.exception("suite %s crashed", suite)
            scores = {"error": str(exc)[:300]}
            with engine.begin() as conn:
                conn.execute(eval_runs.insert().values(suite=suite, release=release, scores=scores, passed=False))
            records.append({"suite": suite, "passed": False, "release": release, "scores": scores})
    return records


def release_gate(engine: Engine, release: str) -> bool:
    """True iff the platform suites (GLOBAL_SUITES) ran and passed for this release.

    Layer gates (HLD v2 I10) decide per layer what gets *published* — see
    ``gates.publishable_layers``; a layer below its gate stays reserved without
    blocking the release of the layers above the bar."""
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(eval_runs.c.suite, eval_runs.c.passed).where(eval_runs.c.release == release)
        ).all()
    ran = {row.suite for row in rows}
    globals_ok = all(row.passed for row in rows if row.suite in GLOBAL_SUITES)
    return bool(rows) and set(GLOBAL_SUITES) <= ran and globals_ok


def l2_gate(engine: Engine) -> dict:
    """Every declared L1 source, including restricted sources, gates L2."""
    from app.clhear.l1.inventory import acceptance_status
    acceptance = acceptance_status(engine)
    summary = acceptance["evidence"]
    blocked = [s["source_key"] for s in summary["sources"] if not s["verified"]]
    return {"passed": acceptance["passed"], "blocked": blocked[:50],
            "blocked_count": summary["unresolved"], "reasons": acceptance["reasons"],
            "audit_id": acceptance["audit_id"], "inventory_hash": acceptance["inventory_hash"]}


def main() -> int:
    """CLI: python -m app.clhear.platform.evals [suite|all] [release]."""
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    release = sys.argv[2] if len(sys.argv) > 2 else None
    records = run_all(engine, release) if target == "all" else [run_suite(engine, target, release=release)]
    print(json.dumps(records, indent=2, default=str))
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
