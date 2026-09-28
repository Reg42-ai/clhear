# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L1 ingestion pipeline (HLD §7.2) with the fidelity gate + repair loop.

For each adapter run: fetch verbatim artifacts -> structural parse -> FIDELITY
GATE (coverage vs the adapter's dumb oracle + contract invariants) -> on
failure, escalate deterministically (learned parse hints -> bounded salvage ->
re-fetch -> LLM-proposed hints via the L0 gateway) until the threshold is met
or attempts are exhausted -> store artifacts -> persist the DocNode tree ->
derive the `clauses` projection -> clause-level diff -> change_events +
outbox SourceChanged in the SAME transaction -> run ledger entry.

Every run is recorded from START with appended stage transitions (the Fleet
visualizer reads these). On exhaustion NOTHING is persisted: the failure is
logged, recorded, emitted as IngestFidelityFailed, and filed as an
`ingest_rectification` proposal for a maintainer (agents propose, humans
ratify). Daily jobs stay LLM-free unless tiers 1-3 cannot reach the goal.
"""
import hashlib
import inspect
import json
import logging
import time
import uuid
import sys
import re
from importlib import metadata as package_metadata
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.platform import record
from app.clhear.l1 import change_detect, fidelity, originals, permissions, rights, spans
from app.clhear.l1.adapters.base import CLAUSE_TYPES, Adapter, DocNode, FetchResult, SourceMeta
from app.clhear.l1.models import (
    change_events,
    citations,
    clause_annotations,
    clauses,
    doc_nodes,
    family_members,
    parse_hints,
    search_units,
    source_families,
    source_versions,
    sources,
)
from app.clhear.models import runs
from app.clhear.platform import events as l0_events
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform.gateway import parse_json_object
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.l1.pipeline")

REPAIR_FLEET = "l1.repair"


class ArtifactStore(Protocol):
    def put(self, key: str, content: bytes, content_type: str) -> str: ...

    def get(self, key: str) -> bytes | None: ...


class LocalStore:
    """Filesystem stand-in for the datalake (offline dev/tests)."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir).resolve()

    def get(self, key: str) -> bytes | None:
        path = (self.base_dir / key).resolve()
        if not path.is_relative_to(self.base_dir):
            raise ValueError("artifact key escapes the configured store")
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key: str, content: bytes, content_type: str) -> str:
        path = (self.base_dir / key).resolve()
        if not path.is_relative_to(self.base_dir):
            raise ValueError("artifact key escapes the configured store")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("An immutable artifact key already contains different bytes")
        return path.as_uri()


class S3Store:
    """The real datalake: versioned + Object Lock, per the P0 terraform."""

    def __init__(self, bucket: str, region: str):
        import boto3

        self._client = boto3.client("s3", region_name=region)
        self.bucket = bucket

    def put(self, key: str, content: bytes, content_type: str) -> str:
        existing = self.get(key)
        if existing is not None:
            if existing != content:
                raise ValueError("An immutable artifact key already contains different bytes")
            return f"s3://{self.bucket}/{key}"
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=content,
                                    ContentType=content_type, IfNoneMatch="*")
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "412"} or self.get(key) != content:
                raise
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes | None:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_set_hash(artifacts) -> str:
    """Frame each original identity; concatenation loses attachment boundaries."""
    manifest = [{"name": a.name, "content_type": a.content_type,
                 "sha256": sha256(a.content), "byte_count": len(a.content)}
                for a in sorted(artifacts, key=lambda a: a.name)]
    return sha256(json.dumps({"schema": "artifact-set-v2", "artifacts": manifest},
                             sort_keys=True, separators=(",", ":")).encode())


class RunRecorder:
    """Run-ledger row written at START; stage transitions appended as the run
    progresses (append-only within the row — the audit trail the Fleet view
    renders). finish() stamps the final status + summary. When the caller is
    part of a fleet execution, inputs carry its job_id (the Fleet job canvas
    groups tasks by it)."""

    def __init__(self, engine: Engine, fleet: str, trigger: str, inputs: dict):
        from app.clhear.l1.workflow import execution_context

        execution = execution_context()
        inputs = {**inputs, **{k: execution[k] for k in ("job_id", "task_id", "attempt") if k in execution}}
        self._source = inputs.get("source")
        self._protected = bool(inputs.get("protected_source"))
        self.artifact_check = None
        self._engine = engine
        self._started = time.monotonic()
        self._last_stage_at = self._started
        self.stages: list[dict] = []
        with engine.begin() as conn:
            self.run_id = conn.execute(
                runs.insert()
                .values(fleet=fleet, trigger=trigger, inputs=inputs, outputs={"status": "running", "stages": []})
                .returning(runs.c.id)
            ).scalar_one()

    def stage(self, name: str, **detail) -> None:
        now = time.monotonic()
        entry = {
            "stage": name,
            "ts": datetime.now(timezone.utc).isoformat(),
            "ms": int((now - self._last_stage_at) * 1000),
            "measurement": "interval_between_reports",
            **detail,
        }
        if self._protected:
            entry.pop("missing_preview", None)
        self._last_stage_at = now
        self.stages.append(entry)
        with self._engine.begin() as conn:
            conn.execute(
                runs.update()
                .where(runs.c.id == self.run_id)
                .values(outputs={"status": "running", "stages": self.stages})
            )

    def finish(self, status: str, summary: dict) -> dict:
        from app.clhear.l1 import http as l1_http

        summary = dict(summary)
        if self._source:
            summary.setdefault("fetch_evidence", l1_http.fetch_evidence())
            summary.setdefault("publisher_checked_at", l1_http.publisher_checked_at())
        if self.artifact_check:
            summary["authorized_artifact_check"] = self.artifact_check
        if self._protected:
            summary.pop("missing_preview", None)
        outputs = {**summary, "status": status, "stages": self.stages}
        with self._engine.begin() as conn:
            conn.execute(
                runs.update()
                .where(runs.c.id == self.run_id)
                .values(outputs=outputs, duration_ms=int((time.monotonic() - self._started) * 1000))
            )
        return outputs


def ensure_source(conn: Connection, meta: SourceMeta) -> tuple[int, int]:
    """Upsert family + source (+ root membership). Returns (family_id, source_id)."""
    family_id = conn.execute(
        sa.select(source_families.c.id).where(source_families.c.key == meta.family_key)
    ).scalar()
    if family_id is None:
        family_id = conn.execute(
            source_families.insert()
            .values(key=meta.family_key, name=meta.family_name, scope_charter=meta.scope_charter)
            .returning(source_families.c.id)
        ).scalar_one()
    basis = rights_basis_for(meta)
    source_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == meta.source_key)).scalar()
    if source_id is None:
        source_id = conn.execute(
            sources.insert()
            .values(
                family_id=family_id,
                key=meta.source_key,
                name=meta.name,
                kind=meta.kind,
                issuer=meta.issuer,
                jurisdiction=meta.jurisdiction,
                license=meta.license,
                license_ref=meta.license_ref or basis.ref,
                adapter=meta.adapter,
                canonical_url=meta.canonical_url,
                short_name=meta.short_name,
                about=meta.about,
                topics=meta.topics,
                rights_basis=basis.basis,
                publisher=meta.publisher or meta.issuer,
                instrument=meta.instrument or meta.short_name or meta.name,
                family_root=True,
            )
            .returning(sources.c.id)
        ).scalar_one()
        conn.execute(
            family_members.insert().values(
                family_id=family_id,
                source_id=source_id,
                relation="root",
                # enforcement sources are read by L7, never by the L2 extractor (HLD v2 §4.7)
                tier="informative" if meta.kind == "enforcement" else "binding",
                status="active",
                added_via="manual",
            )
        )
    else:
        # Curated context is authored in code; keep the row in sync.
        conn.execute(
            sources.update()
            .where(sources.c.id == source_id)
            .values(
                short_name=meta.short_name,
                about=meta.about,
                topics=meta.topics,
                publisher=meta.publisher or meta.issuer,
                instrument=meta.instrument or meta.short_name or meta.name,
            )
        )
    rights.record(conn, source_id, basis, recorded_by=f"l1.rights.{meta.adapter}")
    return family_id, source_id


def rights_basis_for(meta: SourceMeta) -> rights.RightsBasis:
    """SourceMeta override wins; otherwise the adapter's declared basis."""
    if meta.rights_basis:
        default = rights.rights_for(meta.adapter, meta.license)
        return rights.RightsBasis(meta.rights_basis, meta.rights_ref or default.ref, default.evidence_url)
    return rights.rights_for(meta.adapter, meta.license)


def _latest_version(conn: Connection, source_id: int):
    return conn.execute(
        sa.select(source_versions)
        .where(source_versions.c.source_id == source_id)
        .order_by(source_versions.c.id.desc())
        .limit(1)
    ).first()


def _clause_map(conn: Connection, source_version_id: int) -> dict[str, str]:
    rows = conn.execute(
        sa.select(clauses.c.ref, clauses.c.text_hash).where(clauses.c.source_version_id == source_version_id)
    ).all()
    return {row.ref: row.text_hash for row in rows}


def diff_clauses(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    """Clause-level diff aligned by ref (HLD §7.2)."""
    added = sorted(ref for ref in new if ref not in old)
    removed = sorted(ref for ref in old if ref not in new)
    amended = sorted(ref for ref in new if ref in old and new[ref] != old[ref])
    return {"added": added, "removed": removed, "amended": amended}


def _projection_matches(conn: Connection, version_id: int, tree: list[DocNode], public_ok: bool,
                        *, ignore_artifact_provenance: bool = False) -> bool:
    """Unchanged bytes do not prove that an older parser stored this tree.

    Strict adapters check both the document and its clause projection before
    accepting a hash hit, including repairs of already corrupted stored rows.
    ``ignore_artifact_provenance`` compares a re-fetch whose bytes differ: every
    text, fragment, offset and hash must still match; only the locator's
    archived-artifact digests may differ.
    """
    def locator(value):
        value = dict(value or {})
        if ignore_artifact_provenance:
            value.pop("artifacts", None)
        return value
    expected = [node for root in tree for node in root.walk()]
    rows = conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version_id)
                        .order_by(doc_nodes.c.seq)).mappings().all()
    if len(rows) != len(expected):
        return False
    seq_by_object = {id(node): i for i, node in enumerate(expected, 1)}
    parents = {}

    def visit(node, parent=None, depth=0):
        parents[id(node)] = (seq_by_object.get(id(parent)), depth)
        for child in node.children:
            visit(child, node, depth + 1)

    for root in tree:
        visit(root)
    seq_by_id = {row["id"]: row["seq"] for row in rows}
    fields = ("node_type", "ref", "label", "heading", "raw_text", "source_fragment")
    for seq, (row, node) in enumerate(zip(rows, expected), 1):
        if row["seq"] != seq or row["public_ok"] != public_ok:
            return False
        if any(row[field] != getattr(node, field) for field in fields):
            return False
        if locator(row.get("source_locator")) != locator(node.source_locator):
            return False
        if (seq_by_id.get(row["parent_id"]), row["depth"]) != parents[id(node)]:
            return False
        if row["parent_id"] is not None and row["parent_id"] not in seq_by_id:
            return False
        if row["text_hash"] != sha256("\n".join(getattr(node, f) for f in fields[:-1]).encode()):
            return False
    projected = conn.execute(sa.select(clauses).where(clauses.c.source_version_id == version_id)).mappings().all()
    expected_clauses = {seq: node for seq, node in enumerate(expected, 1) if node.node_type in CLAUSE_TYPES and node.ref}
    if len(projected) != len(expected_clauses):
        return False
    layout = spans.span_layout(tree)
    seen = set()
    for row in projected:
        seq = seq_by_id.get(row["doc_node_id"])
        node = expected_clauses.get(seq)
        if node is None or seq in seen:
            return False
        seen.add(seq)
        text = node.subtree_text()
        if (row["ref"], row["ordering"], row["text"], row["text_hash"], row["public_ok"]) != (node.ref, seq, text, sha256(text.encode()), public_ok):
            return False
        if (row["span_start"], row["span_end"]) != layout[id(node)]:
            return False
    return seen == set(expected_clauses)


def _archive_matches(store, manifest, artifacts, source_key, content_hash, public_ok):
    """Unchanged requires complete, readable, byte-exact archived originals."""
    if not isinstance(manifest, list) or len(manifest) != len(artifacts):
        return False
    if any(not isinstance(item, dict) for item in manifest):
        return False
    indexed = {item.get("name"): item for item in manifest}
    if len(indexed) != len(manifest):
        return False
    prefix = "public-ok" if public_ok else "restricted"
    for artifact in artifacts:
        item = indexed.get(artifact.name) or {}
        digest = sha256(artifact.content)
        base = f"{prefix}/{source_key}/sha256-{content_hash}/{digest}/{artifact.name}"
        key = item.get("key", "")
        if (not isinstance(key, str) or not re.fullmatch(re.escape(base) + r"(?:\.repair-[0-9a-f]{32})?", key)
                or (item.get("sha256"), item.get("byte_count"), item.get("content_type"))
                   != (digest, len(artifact.content), artifact.content_type)):
            return False
        expected_uri = (f"s3://{store.bucket}/{key}" if isinstance(store, S3Store) else
                        (store.base_dir / key).resolve().as_uri() if isinstance(store, LocalStore) else None)
        if expected_uri is not None and item.get("uri") != expected_uri:
            return False
        if store.get(key) != artifact.content:
            return False
    return True


def _load_active_hints(conn: Connection, source_id: int) -> list[dict]:
    rows = conn.execute(
        sa.select(parse_hints)
        .where(parse_hints.c.source_id == source_id)
        .where(parse_hints.c.status.in_(("candidate", "approved")))
        .order_by(parse_hints.c.id)
    ).all()
    out = []
    for row in rows:
        hint = row.hint if isinstance(row.hint, dict) else json.loads(row.hint)
        out.append({**hint, "hint_id": row.id})
    return out


def _markup_window(artifacts, span: str, width: int = 600) -> str:
    """Locate the span's leading text inside an artifact and return the raw
    markup around it — context for the LLM, straight from the original."""
    needle = fidelity.ws(span)[:80]
    for artifact in artifacts:
        try:
            text = artifact.content.decode("utf-8", errors="replace")
        except Exception:
            continue
        idx = text.find(needle[:40])
        if idx == -1:
            idx = fidelity.ws(text).find(needle)
            if idx == -1:
                continue
            return fidelity.ws(text)[max(0, idx - width // 2) : idx + width]
        return text[max(0, idx - width // 2) : idx + width]
    return ""


def _llm_propose_hints(gateway, artifacts, missing_spans: list[str]) -> list[dict]:
    """Tier-4 escalation: ask the gateway for parse hints. The LLM only ever
    CLASSIFIES artifact text (node_type/label per span) — it never writes it."""
    from app.clhear.l1.models import NODE_TYPES

    settings = get_settings()
    samples = []
    for span in missing_spans[:12]:
        samples.append(
            {
                "span": fidelity.ws(span)[:400],
                "markup_context": _markup_window(artifacts, span)[:800],
            }
        )
    prompt = (
        "You are repairing a deterministic legal-document parser. The following text spans exist in the "
        "official artifact but were missed by the structural parse. For each span, propose a parse hint.\n"
        f"Allowed node_type values: {', '.join(NODE_TYPES)}.\n"
        'Respond with JSON only: {"hints": [{"match": "<distinctive substring of the span>", '
        '"node_type": "...", "label": "<printed marker if the span starts with one, else empty>", '
        '"ref": "<stable ref if inferable, else empty>"}]}\n\n'
        f"Missed spans with surrounding original markup:\n{json.dumps(samples, ensure_ascii=False, indent=1)}"
    )
    from app.clhear.platform.router import complete

    result = complete(
        gateway,
        "l1.parse_repair",
        prompt=prompt,
        system="You classify document structure. You never rewrite or invent text. JSON only.",
        max_tokens=2000,
        required_keys=["hints"],
        model=settings.clhear_model_repair or None,
    )
    hints = parse_json_object(result.text).get("hints", [])
    return [h for h in hints if isinstance(h, dict) and h.get("match") and h.get("node_type")]


def ingest(engine: Engine, adapter: Adapter, store: ArtifactStore, **kwargs) -> dict:
    """Worker entrypoint: evidence is scoped to this acquisition only."""
    from app.clhear.l1 import http as l1_http
    l1_http.begin_fetch()
    summary = _ingest(engine, adapter, store, **kwargs)
    summary.setdefault("fetch_evidence", l1_http.fetch_evidence())
    summary.setdefault("publisher_checked_at", l1_http.publisher_checked_at())
    if permissions.required_for(adapter.meta()):
        summary.pop("missing_preview", None)
    return summary


def parser_identity(adapter: Adapter) -> dict:
    # Include inherited parsers, independent readers and all local L1 helpers.
    # Only digests leave this function; header/config values are not logged.
    root = Path(__file__).parent
    parts = [(str(path.relative_to(root)), path.read_bytes()) for path in sorted(root.rglob("*.py"))]
    module = inspect.getmodule(type(adapter))
    path = getattr(module, "__file__", None)
    if path and not Path(path).is_relative_to(root):
        parts.append((module.__name__, Path(path).read_bytes()))
    dependencies = {"python": sys.version.split()[0]}
    for name in ("beautifulsoup4", "pypdf", "pdfminer.six", "docling", "crawl4ai", "soupsieve"):
        try:
            dependencies[name] = package_metadata.version(name)
        except package_metadata.PackageNotFoundError:
            dependencies[name] = "not_installed"

    def serializable(value):
        if is_dataclass(value):
            return serializable(asdict(value))
        if isinstance(value, re.Pattern):
            return {"pattern": value.pattern, "flags": value.flags}
        if isinstance(value, dict):
            return {str(k): serializable(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [serializable(v) for v in sorted(value, key=str)] if isinstance(value, (set, frozenset)) else [serializable(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return value.isoformat() if isinstance(value, date) else type(value).__qualname__

    parameters = set(inspect.signature(type(adapter).__init__).parameters)
    # Runtime counters/caches are not parser configuration. Constructor inputs
    # and private configuration participate; acquired response bytes do not.
    configured = {name: value for name, value in vars(adapter).items()
                  if name.lstrip("_") in parameters or name in {"_meta", "_headers", "_url", "_urls"}}
    config = {**configured, "meta": adapter.meta(),
              "worker_parser_settings": {name: getattr(get_settings(), name) for name in (
                  "clhear_fidelity_threshold", "clhear_ingest_max_attempts", "clhear_salvage_cap", "clhear_model_repair")},
              **{name: getattr(adapter, name, None) for name in ("PROVISION", "HEADING", "renderer", "version_kind", "version_policy", "headers")}}
    config_hash = sha256(json.dumps(serializable(config), sort_keys=True).encode())
    body = b"".join(name.encode() + b"\0" + content for name, content in sorted(parts))
    body += json.dumps(dependencies, sort_keys=True).encode() + config_hash.encode()
    return {"adapter": adapter.meta().adapter, "method": "l1-implementation-dependencies-config-sha256-v2",
            "implementation_sha256": sha256(body), "configuration_sha256": config_hash,
            "dependencies": dependencies, "normalization_version": originals.NORMALIZATION_VERSION,
            "modules": [name for name, _ in sorted(parts)]}


def _original_proof(meta: SourceMeta, result: FetchResult) -> dict:
    try:
        originals.attach_source_locations(meta.source_key, meta.adapter, result.artifacts, result.tree, meta.canonical_url)
    except Exception:
        # The verifier returns a safe, explicit reason instead of assigning
        # invented locations to an unsupported or mismatched original.
        pass
    return originals.verify_original_projection(meta.source_key, meta.adapter, result.artifacts, result.tree,
                                                canonical_url=meta.canonical_url)


def _ingest(
    engine: Engine,
    adapter: Adapter,
    store: ArtifactStore,
    *,
    trigger: str = "manual",
    gateway=None,
    job_id: str | None = None,
    force: bool = False,
    index_embeddings: bool = True,
) -> dict:
    """Run one adapter through fetch -> fidelity gate/repair loop -> persist.

    Returns the run summary. status: rights-blocked|added|amended|unchanged|up-to-date|
    stale|failed|not-fully-successful. `llm_assisted`/`recovered_spans`/
    `hints_used` mark degraded-but-successful runs (warnings in the Activity
    feed). A crash in fetch() always `finish()`es the run: previous version
    kept as `stale`, first ingest recorded as `failed`.

    Set index_embeddings=False for an L1-only import: the global embedding
    projection is left untouched and no embedding provider is called.
    """
    settings = get_settings()
    meta = adapter.meta()
    inputs = {"source": meta.source_key, "protected_source": permissions.required_for(meta)}
    if job_id:
        inputs["job_id"] = job_id
    recorder = RunRecorder(engine, f"l1.{meta.adapter}", trigger, inputs)
    try:
        return _ingest_recorded(engine, adapter, store, recorder, meta, settings, trigger=trigger,
                                gateway=gateway, job_id=job_id, force=force, index_embeddings=index_embeddings)
    except Exception as exc:
        # Decoder/validator failures must terminate this run as well as the
        # queue task. Diagnostics never include publisher text or credentials.
        outputs = recorder.finish("failed", {
            "source": meta.source_key, "error_type": type(exc).__name__,
            "error": "Import validation or archive verification failed; no successful run was recorded",
        })
        return {**outputs, "run_id": recorder.run_id}


def _ingest_recorded(engine, adapter, store, recorder, meta, settings, *, trigger="manual", gateway=None,
                     job_id=None, force=False, index_embeddings=True):
    from app.clhear.l1.origin import is_test_source, production_worker
    if production_worker() and is_test_source(meta):
        outputs = recorder.finish("source-blocked", {
            "source": meta.source_key, "freshness": "not_checked",
            "error_type": "TestSourceInProduction",
            "error": "Explicit test corpus cannot enter a production worker import",
        })
        return {**outputs, "run_id": recorder.run_id}
    identity = parser_identity(adapter)
    from app.clhear.l1 import workflow

    protected = permissions.required_for(meta)
    permission_checks = {}
    public_ok = meta.license == "open" and rights.republishable(rights_basis_for(meta).basis)
    if protected:
        with workflow.stage("permission", details={"source": meta.source_key}) as step, engine.connect() as conn:
            permission_checks = {operation: permissions.candidate_decision(conn, meta.source_key, operation, canonical_url=meta.canonical_url)
                                 for operation in ("acquire", "store", "parse", "infer", "embed", "derive", "display_public")}
            existing_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == meta.source_key)).scalar()
            previous = _latest_version(conn, existing_id) if existing_id is not None else None
            step.details["decisions"] = permission_checks
            if any(not permission_checks[operation]["allowed"] for operation in ("acquire", "store", "parse")):
                step.status = "blocked"
        blocked = [operation for operation in ("acquire", "store", "parse")
                   if not permission_checks[operation]["allowed"]]
        recorder.stage("permissions", decisions=permission_checks, blocked_operations=blocked)
        if blocked:
            summary = {
                "source": meta.source_key, "version": previous.version_label if previous else None,
                "content_hash": previous.content_hash if previous else None,
                "source_version_id": previous.id if previous else None,
                "blocked_operations": blocked, "permission_decisions": permission_checks,
                "previous_version_preserved": previous is not None, "freshness": "not_checked",
                "error": "Protected source requires approved, current permissions for: " + ", ".join(blocked),
            }
            outputs = recorder.finish("rights-blocked", summary)
            return {**summary, "status": "rights-blocked", "run_id": recorder.run_id, "stages": outputs["stages"]}
        if not permission_checks["infer"]["allowed"]:
            gateway = None
        if not permission_checks["embed"]["allowed"]:
            index_embeddings = False
        public_ok = permission_checks["display_public"]["allowed"]

    with engine.begin() as conn:
        family_id, source_id = ensure_source(conn, meta)
        previous = _latest_version(conn, source_id)
        stored_hints = _load_active_hints(conn, source_id)

    # Always probe the publisher (first ingest of a key must fetch; daily
    # "up-to-date" is a content-hash match after a real GET, not a label skip).
    from app.clhear.l1 import http as l1_http

    try:
        with workflow.stage("acquisition_parse", details={"source": meta.source_key, "parser_identity": identity}):
            result = adapter.fetch(None)
    except Exception as exc:
        if meta.adapter == "restricted_file" and isinstance(exc, FileNotFoundError):
            outputs = recorder.finish("awaiting-artifact", {
                "source": meta.source_key, "freshness": "not_checked",
                "error_type": "FileNotFoundError",
                "error": "The reviewed local publisher artifact is not available to this worker",
                "previous_version_preserved": previous is not None,
                "source_version_id": previous.id if previous else None,
            })
            return {**outputs, "run_id": recorder.run_id}
        error = str(exc)[:500]
        log.exception("fetch crashed for %s", meta.source_key)
        if previous is not None:
            summary = {
                "source": meta.source_key,
                "version": previous.version_label,
                "content_hash": previous.content_hash,
                "source_version_id": previous.id,
                "error": error,
                "freshness": "stale",
            }
            outputs = recorder.finish("stale", summary)
            return {**summary, "status": "stale", "run_id": recorder.run_id, "stages": outputs["stages"]}
        summary = {"source": meta.source_key, "error": error}
        outputs = recorder.finish("failed", summary)
        return {**summary, "status": "failed", "run_id": recorder.run_id, "stages": outputs["stages"]}
    observations = l1_http.fetch_evidence()
    if meta.adapter == "restricted_file" and result is not None:
        check = getattr(adapter, "artifact_check", None)
        expected = [{"name": a.name, "sha256": sha256(a.content), "byte_count": len(a.content),
                     "content_type": a.content_type} for a in result.artifacts]
        if (isinstance(check, dict) and check.get("schema") == "clhear.authorized-artifact-check.v1"
                and check.get("source_key") == meta.source_key and check.get("artifacts") == expected
                and ((check.get("method") == "authorized_artifact_store_read"
                      and check.get("publisher_check_performed") is False)
                     or (check.get("method") == "publisher_url_read"
                         and check.get("publisher_check_performed") is True))):
            recorder.artifact_check = dict(check)
    freshness = ("stale" if l1_http.last_good_used() else
                 "live" if l1_http.publisher_checked_at() else
                 "fixture" if observations and all(o["origin"] == "fixture" for o in observations) else
                 "local_snapshot" if getattr(adapter, "fetch_origin", None) == "local_snapshot" else
                 "artifact" if meta.adapter == "restricted_file" else "not_checked")
    recorder.stage("fetch", artifacts=len(result.artifacts) if result else 0, freshness=freshness)
    if freshness == "stale":
        summary = {"source": meta.source_key, "freshness": "stale", "previous_version_preserved": previous is not None,
                   "source_version_id": previous.id if previous else None,
                   "content_hash": previous.content_hash if previous else None,
                   "version": previous.version_label if previous else None,
                   "error": "Publisher check failed; cached bytes cannot advance the candidate."}
        outputs = recorder.finish("stale", summary)
        return {**outputs, "run_id": recorder.run_id}
    if result is None:
        if previous is None:
            outputs = recorder.finish("failed", {"source": meta.source_key, "freshness": "not_checked",
                                                 "error": "Adapter returned no artifact and there is no stored version."})
            return {**outputs, "run_id": recorder.run_id}
        summary = {
            "source": meta.source_key,
            "version": previous.version_label if previous else None,
            "content_hash": previous.content_hash,
            "source_version_id": previous.id,
            "freshness": freshness,
            "note": "No new artifact; freshness requires successful publisher evidence.",
        }
        outputs = recorder.finish("up-to-date" if l1_http.publisher_checked_at() else "stale", summary)
        return {**outputs, "run_id": recorder.run_id}

    content_hash = artifact_set_hash(result.artifacts)
    validator = getattr(adapter, "validate_tree", None)
    strict_violations = validator(result.tree, result.artifacts) if validator else []
    original_proof = _original_proof(meta, result)

    def _prior_manifest(conn):
        """The archive manifest recorded when the stored version was imported."""
        for previous_run in conn.execute(sa.select(runs.c.outputs)
                .where(runs.c.inputs["source"].as_string() == meta.source_key).order_by(runs.c.id.desc())):
            evidence = previous_run.outputs or {}
            if (evidence.get("source_version_id") == previous.id and evidence.get("content_hash") == previous.content_hash
                    and evidence.get("artifact_manifest")):
                return evidence["artifact_manifest"]
        return None

    if previous is not None and previous.content_hash == content_hash and not force and not strict_violations and original_proof["verified"]:
        with engine.connect() as conn:
            matching = _projection_matches(conn, previous.id, result.tree, public_ok)
            prior_manifest = _prior_manifest(conn)
            # Legacy imports without a complete archive manifest get an
            # append-only repair, not invented acquisition provenance.
            matching = matching and _archive_matches(store, prior_manifest, result.artifacts,
                                                       meta.source_key, content_hash, public_ok and not protected)
        if matching:
            summary = {
                "source": meta.source_key,
                "version": previous.version_label,
                "content_hash": previous.content_hash,
                "source_version_id": previous.id,
                "freshness": freshness,
                "note": "Artifact bytes and stored projection are unchanged.",
                "content_hash_method": "artifact-set-v2",
                "parser_identity": identity,
                "canonical_text_hash": sha256(spans.canonical_text(result.tree).encode()),
                "artifact_manifest": prior_manifest,
                "original_verification": original_proof,
            }
            outputs = recorder.finish("unchanged", summary)
            return {**summary, "status": "unchanged", "run_id": recorder.run_id, "stages": outputs["stages"],
                    **({"authorized_artifact_check": recorder.artifact_check} if recorder.artifact_check else {})}
        recorder.stage("projection_repair", reason="stored projection differs from validated source parse")
    elif (previous is not None and not force and not strict_violations and original_proof["verified"]
          and len(result.artifacts) == 1):
        # Dynamic publishers never serve the same bytes
        # twice, so the artifact-set hash alone would mint a new version and a
        # zero-clause "amended" event on every daily check. When the validated
        # parse of a single-document page reproduces the stored projection
        # exactly, the text is unchanged; the byte difference is recorded, not
        # versioned. Multi-part bundles keep versioning on any boundary shift so
        # every preserved original stays addressable by its own version.
        with engine.connect() as conn:
            matching = _projection_matches(conn, previous.id, result.tree, public_ok, ignore_artifact_provenance=True)
            # Readback verifies the stored version's archived originals from this
            # manifest; without it the shortcut would report an unverifiable import.
            prior_manifest = _prior_manifest(conn) if matching else None
        if matching and prior_manifest:
            summary = {
                "source": meta.source_key,
                "version": previous.version_label,
                "content_hash": previous.content_hash,
                "source_version_id": previous.id,
                "freshness": freshness,
                "note": "Publisher bytes differ (dynamic page markup) but the parsed text and stored projection are unchanged.",
                "artifact_bytes_changed": True,
                "observed_content_hash": content_hash,
                "content_hash_method": "artifact-set-v2",
                "parser_identity": identity,
                "canonical_text_hash": sha256(spans.canonical_text(result.tree).encode()),
                "artifact_manifest": prior_manifest,
                "original_verification": original_proof,
            }
            recorder.stage("unchanged_text", artifact_bytes_changed=True, observed_content_hash=content_hash)
            outputs = recorder.finish("unchanged", summary)
            return {**summary, "status": "unchanged", "run_id": recorder.run_id, "stages": outputs["stages"]}

    # ---- fidelity gate + escalation loop -----------------------------------
    threshold = settings.clhear_fidelity_threshold
    max_attempts = max(1, settings.clhear_ingest_max_attempts)
    salvage_cap = settings.clhear_salvage_cap

    report = None
    hints_used: list[int] = []
    new_llm_hints: list[dict] = []
    recovered_spans = 0
    llm_assisted = False
    last_attempt_coverage = -1.0

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            try:
                with workflow.stage("acquisition_parse", details={"attempt": attempt, "parser_identity": identity}):
                    refetched = adapter.fetch(None)
            except Exception as exc:
                status = ("awaiting-artifact" if meta.adapter == "restricted_file" and isinstance(exc, FileNotFoundError)
                          else "stale" if previous is not None else "failed")
                outputs = recorder.finish(status, {
                    "source": meta.source_key, "attempt": attempt, "error_type": type(exc).__name__,
                    "error": "Repair acquisition failed; no candidate was persisted",
                    "previous_version_preserved": previous is not None,
                    "source_version_id": previous.id if previous else None,
                    "freshness": "stale" if previous is not None else "not_checked",
                })
                return {**outputs, "run_id": recorder.run_id}
            recorder.stage("fetch", attempt=attempt, artifacts=len(refetched.artifacts) if refetched else 0)
            if refetched is not None:
                result = refetched
        tree = result.tree
        expected = adapter.expected_text(result.artifacts)
        node_count = sum(1 for n in tree for _ in n.walk())
        recorder.stage("parse", attempt=attempt, nodes=node_count)

        with workflow.stage("fidelity", details={"attempt": attempt}) as step:
            report = fidelity.check(tree, expected)
            step.details.update({k: v for k, v in report.summary().items() if k != "missing_preview"})
        # Publisher-specific exact checks are gates too. A substring coverage
        # score cannot establish ordering, multiplicity or clause boundaries.
        if validator:
            report.violations.extend(validator(tree, result.artifacts))
        recorder.stage("gate", attempt=attempt, **report.summary())

        # Tier 1b: learned hints (deterministic; zero LLM).
        if not report.ok(threshold) and not report.violations and report.missing_spans and stored_hints:
            remaining, used = fidelity.apply_hints(tree, report.missing_spans, stored_hints)
            if used:
                hints_used = sorted(set(hints_used) | set(used))
                report = fidelity.check(tree, expected)
                recorder.stage("hints", attempt=attempt, hints_used=used, **report.summary())

        if protected and gateway is not None:
            with engine.connect() as conn:
                if not permissions.decision(conn, meta.source_key, "infer")["allowed"]:
                    gateway = None
        # Tier 4: LLM-proposed hints for NOVEL gaps (only if deterministic tiers
        # can't close the gap within the salvage cap).
        if (
            not report.ok(threshold)
            and not report.violations
            and report.missing_spans
            and gateway is not None
            and fidelity.span_tokens(report.missing_spans) / max(1, report.total_tokens) > salvage_cap
        ):
            try:
                proposed = _llm_propose_hints(gateway, result.artifacts, report.missing_spans)
            except Exception as exc:  # spend cap, provider error — degrade, never crash the fleet
                log.warning("LLM repair tier unavailable for %s: %s", meta.source_key, exc)
                proposed = []
            if proposed:
                remaining, _ = fidelity.apply_hints(tree, report.missing_spans, proposed)
                new_llm_hints = proposed
                llm_assisted = True
                report = fidelity.check(tree, expected)
                recorder.stage("llm_repair", attempt=attempt, hints_proposed=len(proposed), **report.summary())

        # Tier 2: bounded salvage for small residual gaps.
        if not report.ok(threshold) and not report.violations and report.missing_spans:
            residual_share = fidelity.span_tokens(report.missing_spans) / max(1, report.total_tokens)
            if residual_share <= salvage_cap:
                recovered_spans += fidelity.salvage(tree, report.missing_spans)
                report = fidelity.check(tree, expected)
                recorder.stage("salvage", attempt=attempt, recovered=recovered_spans, **report.summary())

        # Re-run both the publisher grammar and independent original reader
        # after every repair. Appending the right words under the wrong parent
        # must never turn a failed import into a certified one.
        if validator:
            report.violations.extend(validator(tree, result.artifacts))
        original_proof = _original_proof(meta, result)
        if not original_proof["verified"]:
            report.violations.extend(f"original verification: {item['code']}" for item in original_proof["findings"])
        if report.ok(threshold):
            break
        if report.violations:
            break  # structural contract bugs: retrying cannot help
        if report.coverage <= last_attempt_coverage:
            break  # deterministic no-progress: further attempts are identical
        last_attempt_coverage = report.coverage

    if report is None or not report.ok(threshold):
        # ---- exhaustion: nothing persisted, loudly visible ------------------
        detail = report.summary() if report else {"coverage": 0.0}
        if protected:
            detail.pop("missing_preview", None)
        log.error(
            "ingest NOT fully successful for %s: coverage %.4f after %d attempts — %s",
            meta.source_key,
            detail.get("coverage", 0.0),
            max_attempts,
            detail,
        )
        with engine.begin() as conn:
            l0_events.emit(
                conn,
                layer="l1",
                kind="IngestFidelityFailed",
                subject_ref=meta.source_key,
                payload={"source": meta.source_key, "attempts": max_attempts, **detail},
                producer=f"l1.pipeline.{meta.adapter}",
            )
            l0_proposals.create_proposal(
                conn,
                layer="l1",
                kind="ingest_rectification",
                subject_ref=meta.source_key,
                draft={"attempts": max_attempts, **detail},
                rationale=(
                    f"Ingest of {meta.source_key} did not reach the fidelity threshold "
                    f"({threshold:.3%}) after {max_attempts} attempts — manual rectification needed."
                ),
            )
        summary = {"source": meta.source_key, "version": result.version_label, **detail,
                   "original_verification": original_proof, "parser_identity": identity,
                   "previous_version_preserved": previous is not None}
        outputs = recorder.finish("failed", summary)
        return {**summary, "status": "not-fully-successful", "run_id": recorder.run_id, "stages": outputs["stages"]}

    # ---- persist (gate green) ------------------------------------------------
    try:
        # A repair retry can return different bytes; hash the artifact actually
        # persisted, never the first (failed) fetch.
        content_hash = artifact_set_hash(result.artifacts)
        with engine.connect() as conn:
            existing_label = conn.execute(sa.select(source_versions.c.id).where(
                source_versions.c.source_id == source_id,
                source_versions.c.version_label == result.version_label,
            )).scalar()
        if existing_label is not None:
            # Every adapter preserves historical clause identities on repair.
            result.version_label += f":parse-{uuid.uuid4().hex[:12]}"
        if l1_http.last_good_used():
            raise RuntimeError("Repair acquisition used stale cached bytes; previous version retained")
        with workflow.stage("persistence", details={"parser_identity": identity}):
            persisted = _persist(
                engine, store, meta, source_id, previous, result, content_hash, report,
                hints_used, new_llm_hints, recovered_spans, llm_assisted, recorder,
                force=force, llm_router=gateway, index_embeddings=index_embeddings, freshness=freshness,
                public_ok=public_ok, protected=protected, permission_checks=permission_checks,
                parser=identity,
            )
            return persisted
    except Exception as exc:
        # The raw driver message carries the SQL statement and its parameters;
        # only the redacted, structured description leaves the worker.
        from app.clhear.l1 import workflow
        from app.clhear.platform import failures
        failure = failures.describe(exc, source=meta.source_key, worker="l1", stage=workflow.current_stage(),
                                    **{k: v for k, v in workflow.execution_context().items() if k in {"task_id", "attempt", "job_id"}})
        recorder.finish("failed", {"source": meta.source_key, "error": failure["message"], "error_type": failure["error_type"],
                                   "error_code": failure["error_code"], "sqlstate": failure["sqlstate"]})
        return {"source": meta.source_key, "status": "failed", "error": failure["message"], "error_type": failure["error_type"],
                "error_code": failure["error_code"], "sqlstate": failure["sqlstate"], "failure": failure, "run_id": recorder.run_id}


def _persist(
    engine: Engine,
    store: ArtifactStore,
    meta: SourceMeta,
    source_id: int,
    previous,
    result: FetchResult,
    content_hash: str,
    report,
    hints_used: list[int],
    new_llm_hints: list[dict],
    recovered_spans: int,
    llm_assisted: bool,
    recorder: RunRecorder,
    force: bool = False,
    llm_router=None,
    index_embeddings: bool = True,
    freshness: str = "live",
    public_ok: bool | None = None,
    protected: bool = False,
    permission_checks: dict | None = None,
    parser: dict | None = None,
) -> dict:
    if protected:
        with engine.connect() as conn:
            current = {operation: permissions.candidate_decision(conn, meta.source_key, operation, canonical_url=meta.canonical_url)
                       for operation in ("store", "parse", "derive", "infer", "display_public")}
        if any(not current[operation]["allowed"] for operation in ("store", "parse")):
            raise PermissionError("Source permissions changed before persistence; previous version retained")
        public_ok = current["display_public"]["allowed"]
    if public_ok is None:
        public_ok = meta.license == "open" and rights.republishable(rights_basis_for(meta).basis)
    # Displaying clauses does not authorize redistribution of full originals.
    prefix = "public-ok" if public_ok and not protected else "restricted"
    artifact_uris = []
    artifact_manifest = []
    names = [a.name for a in result.artifacts]
    if not names or len(set(names)) != len(names):
        raise ValueError("An artifact manifest requires unique, nonempty artifact names")
    for artifact in result.artifacts:
        if not artifact.name or "/" in artifact.name or "\\" in artifact.name or artifact.name in {".", ".."}:
            raise ValueError("Artifact names must be a single safe path component")
        from app.clhear.l1 import workflow
        workflow.assert_ownership()
        # Content addressing prevents a stale lease from overwriting another
        # worker's original before its database fencing check rejects it.
        key = f"{prefix}/{meta.source_key}/sha256-{content_hash}/{sha256(artifact.content)}/{artifact.name}"
        archived = store.get(key)
        if archived is not None and archived != artifact.content:
            # Preserve the corrupted object for investigation; the repaired
            # version binds a fresh immutable object, not an overwritten one.
            key += ".repair-" + uuid.uuid4().hex
        uri = store.put(key, artifact.content, artifact.content_type)
        artifact_uris.append(uri)
        artifact_manifest.append({"name": artifact.name, "key": key, "uri": uri,
                                  "sha256": sha256(artifact.content), "byte_count": len(artifact.content),
                                  "content_type": artifact.content_type})

    # Text is public only when the licence is open AND the rights basis allows
    # republication (derived_only sources keep hashes/derived facts public).
    tree = result.tree

    with engine.begin() as conn:
        from app.clhear.l1 import workflow
        workflow.assert_ownership(conn)
        if protected:
            final_permissions = {op: permissions.candidate_decision(conn, meta.source_key, op,
                                 canonical_url=meta.canonical_url) for op in ("store", "parse", "display_public")}
            if any(not final_permissions[op]["allowed"] or
                   final_permissions[op]["permission_id"] != current[op]["permission_id"] for op in ("store", "parse")):
                raise PermissionError("Source authorization changed while archiving; previous version retained")
            public_ok = final_permissions["display_public"]["allowed"]
        if previous is not None:
            conn.execute(source_versions.update().where(source_versions.c.id == previous.id).values(status="superseded"))
        version_id = conn.execute(
            source_versions.insert().values(
                source_id=source_id, version_label=result.version_label,
                version_kind=result.version_kind, as_of_date=result.as_of_date,
                effective_date=result.effective_date,
                s3_uri=artifact_uris[0] if artifact_uris else "",
                content_hash=content_hash, status="in_force",
            ).returning(source_versions.c.id)
        ).scalar_one()

        clause_rows = persist_tree(
            conn, version_id, tree, public_ok,
            derived_by=f"l1.pipeline.{meta.adapter}",
            valid_from=result.effective_date,
        )
        if clause_rows:
            conn.execute(clauses.insert(), clause_rows)
        readback_proof = originals.verify_original_projection(
            meta.source_key, meta.adapter, result.artifacts,
            conn.execute(sa.select(doc_nodes).where(doc_nodes.c.source_version_id == version_id).order_by(doc_nodes.c.seq)).mappings().all(),
            conn.execute(sa.select(clauses).where(clauses.c.source_version_id == version_id)).mappings().all(),
            canonical_url=meta.canonical_url,
        )
        if not readback_proof["verified"]:
            raise ValueError("Persisted original verification failed: " + ", ".join(f["code"] for f in readback_proof["findings"]))
        from app.clhear.l1 import annotate as l1_annotate
        from app.clhear.l1 import retrieval as l1_retrieval

        derivation_allowed = not protected or permissions.decision(conn, meta.source_key, "derive")["allowed"]
        annotation_count = l1_annotate.heuristics_for_version(conn, version_id, list(meta.topics)) if derivation_allowed else 0
        search_meta = replace(meta, license="open" if public_ok else "restricted")
        unit_count = l1_retrieval.build_units_for_version(conn, search_meta, source_id, version_id, tree)
        from app.clhear.l1 import families as l1_families

        citation_counts = (l1_families.mine_citations(conn, source_id, version_id) if derivation_allowed
                           else {"status": "skipped", "reason": "derive permission not granted"})

        new_map = {row["ref"]: row["text_hash"] for row in clause_rows}
        old_map = _clause_map(conn, previous.id) if previous is not None else {}
        diff = diff_clauses(old_map, new_map)
        changed_refs = diff["added"] + diff["removed"] + diff["amended"]
        change_kind = "amended" if previous is not None else "added"

        # Clause ids of the new version for every changed ref + the effective
        # date extracted from the changed text (HLD v2 §4.1 change detectors).
        changed_ref_set = set(diff["added"] + diff["amended"])
        id_rows = conn.execute(
            sa.select(clauses.c.id, clauses.c.ref, clauses.c.text).where(clauses.c.source_version_id == version_id)
        ).all()
        changed_clause_ids = [r.id for r in id_rows if r.ref in changed_ref_set]
        changed_texts = [r.text for r in id_rows if r.ref in changed_ref_set]
        if previous is None:
            changed_texts = []  # first ingest: no amendment text to date
        effective = change_detect.effective_date_for(
            changed_texts,
            publisher_effective=result.effective_date,
            publisher_as_of=result.as_of_date,
            detected_on=datetime.now(timezone.utc).date(),
        )
        infer_allowed = not protected or permissions.decision(conn, meta.source_key, "infer")["allowed"]
        if previous is not None and effective.basis != "text" and llm_router is not None and infer_allowed:
            effective = change_detect.refine_with_router(llm_router, meta.source_key, changed_texts, effective)

        diff_uri = ""
        if previous is not None:
            diff_doc = json.dumps(
                {
                    "source": meta.source_key,
                    "old_version": previous.version_label,
                    "new_version": result.version_label,
                    **diff,
                },
                indent=2,
            ).encode()
            diff_uri = store.put(
                f"{prefix}/{meta.source_key}/{result.version_label}/diff.json", diff_doc, "application/json"
            )

        change_event_id = conn.execute(
            change_events.insert()
            .values(
                source_id=source_id,
                kind=change_kind,
                old_version=previous.version_label if previous else None,
                new_version=result.version_label,
                clause_refs=changed_refs,
                diff_s3_uri=diff_uri,
                clause_ids=changed_clause_ids,
                effective_date=effective.value,
                effective_date_basis=effective.basis,
            )
            .returning(change_events.c.id)
        ).scalar_one()
        event_payload = {
            "source": meta.source_key,
            "change": change_kind,
            "old_version": previous.version_label if previous else None,
            "new_version": result.version_label,
            "clause_refs": changed_refs,
            "content_hash": content_hash,
            "content_hash_method": "artifact-set-v2",
        }
        l0_events.emit(
            conn,
            layer="l1",
            kind="SourceChanged",
            subject_ref=meta.source_key,
            payload=event_payload,
            producer=f"l1.pipeline.{meta.adapter}",
        )
        # HLD v2 §3/§4.1: the bus event downstream fleets (L2 change
        # inferencers, L7 linkers) subscribe to — clause ids + dates.
        l0_events.publish_layer_event(
            conn,
            layer="l1",
            event="changed",
            subject_ref=meta.source_key,
            payload={
                **event_payload,
                "change_event_id": change_event_id,
                "source_version_id": version_id,
                "clause_ids": changed_clause_ids,
                "added": diff["added"],
                "removed": diff["removed"],
                "amended": diff["amended"],
                "effective_date": effective.value.isoformat() if effective.value else None,
                "effective_date_basis": effective.basis,
                "as_of_date": result.as_of_date.isoformat() if result.as_of_date else None,
                "detected_at": datetime.now(timezone.utc).isoformat(),
            },
            producer=f"l1.pipeline.{meta.adapter}",
        )

        now = datetime.now(timezone.utc)
        if hints_used:
            conn.execute(
                parse_hints.update()
                .where(parse_hints.c.id.in_(hints_used))
                .values(times_used=parse_hints.c.times_used + 1, last_used_at=now, last_needed_at=now)
            )
        if new_llm_hints:
            # Learn once: persist gate-passing hints for all future runs, and
            # file a proposal so a maintainer ratifies a permanent adapter fix.
            proposal_id = l0_proposals.create_proposal(
                conn,
                layer="l1",
                kind="parse_hint",
                subject_ref=meta.source_key,
                draft={"hints": new_llm_hints},
                rationale=(
                    f"LLM-proposed parse hints repaired the {meta.source_key} ingest "
                    "(gate-validated). Approve to keep + fold into the adapter; reject to retire."
                ),
            )
            for hint in new_llm_hints:
                conn.execute(
                    parse_hints.insert().values(
                        source_id=source_id,
                        hint={k: hint[k] for k in ("match", "node_type", "label", "ref") if k in hint},
                        origin="llm",
                        status="candidate",
                        proposal_id=proposal_id,
                        times_used=1,
                        last_used_at=now,
                        last_needed_at=now,
                    )
                )

    node_count = sum(1 for n in tree for _ in n.walk())
    recorder.stage("persist", version=result.version_label, nodes=node_count, clauses=len(clause_rows))
    recorder.stage("annotate", annotations=annotation_count)
    recorder.stage("index", search_units=unit_count)
    recorder.stage("citations", **citation_counts)
    recorder.stage(
        "diff",
        old_version=previous.version_label if previous else None,
        new_version=result.version_label,
        added=len(diff["added"]),
        removed=len(diff["removed"]),
        amended=len(diff["amended"]),
        effective_date=effective.value.isoformat() if effective.value else None,
        effective_date_basis=effective.basis,
    )
    summary = {
        "source": meta.source_key,
        "version": result.version_label,
        "source_version_id": version_id,
        "version_kind": result.version_kind,
        "nodes": node_count,
        "clauses": len(clause_rows),
        "normative_clauses": sum(1 for row in clause_rows if row.get("normative")),
        "coverage": round(report.coverage, 5),
        "diff": diff,
        "change_event_id": change_event_id,
        "effective_date": effective.value.isoformat() if effective.value else None,
        "effective_date_basis": effective.basis,
        "artifacts": artifact_uris,
        "artifact_manifest": artifact_manifest,
        "parser_identity": parser,
        "original_verification": readback_proof,
        "canonical_text_hash": sha256(spans.canonical_text(tree).encode()),
        "content_hash": content_hash,
        "content_hash_method": "artifact-set-v2",
        "freshness": freshness,
    }
    if permission_checks:
        summary["permission_decisions"] = permission_checks
    if hints_used:
        summary["hints_used"] = hints_used
    if recovered_spans:
        summary["recovered_spans"] = recovered_spans
    if llm_assisted:
        summary["llm_assisted"] = True
    degraded = bool(hints_used or recovered_spans or llm_assisted)
    if degraded:
        log.warning("ingest of %s needed repair (hints=%s salvage=%d llm=%s) — fix the adapter",
                    meta.source_key, hints_used, recovered_spans, llm_assisted)
    # Vector index (HLD v2 I7): embed the new version's public clauses. An index
    # over the record, rebuilt nightly as well; a failure here never fails ingest.
    try:
        from app.clhear.platform import embeddings, graph

        if index_embeddings:
            vec = embeddings.rebuild_index(engine, trigger="ingest", release=result.version_label)
            summary["embeddings"] = {"embedded": vec["embedded"], "model": vec["model"]}
        else:
            reason = "embed permission not granted" if protected and not (permission_checks or {}).get("embed", {}).get("allowed") else "L1-only import"
            summary["embeddings"] = {"status": "skipped", "reason": reason}
        graph.invalidate(engine)
    except Exception:  # pragma: no cover - the index is a projection; ingest succeeded
        log.exception("embedding index update failed for %s", meta.source_key)
    outputs = recorder.finish("warning" if degraded else "succeeded", {**summary, "change": change_kind})
    log.info(
        "ingested %s %s: %d nodes / %d clauses (%s, coverage %.4f)",
        meta.source_key, result.version_label, node_count, len(clause_rows), change_kind, report.coverage,
    )
    return {**summary, "status": change_kind, "run_id": recorder.run_id, "stages": outputs["stages"],
            **({"authorized_artifact_check": recorder.artifact_check} if recorder.artifact_check else {})}


def persist_tree(
    conn: Connection,
    version_id: int,
    tree: list[DocNode],
    public_ok: bool,
    *,
    derived_by: str = "l1.pipeline",
    valid_from: date | None = None,
) -> list[dict]:
    """Insert the DocNode tree; return clause-projection rows (not yet inserted).

    Clause rows carry ``span_start``/``span_end`` offsets into the version's
    canonical text (l1.spans) and the deterministic ``normative`` flag.
    """
    seq = 0
    clause_rows: list[dict] = []
    layout = spans.span_layout(tree)

    def visit(node: DocNode, parent_id: int | None, depth: int, path_parts: list[str]) -> None:
        nonlocal seq
        seq += 1
        node_seq = seq
        payload = "\n".join([node.node_type, node.ref, node.label, node.heading, node.raw_text]).encode()
        node_id = conn.execute(
            doc_nodes.insert()
            .values(
                source_version_id=version_id,
                parent_id=parent_id,
                seq=seq,
                depth=depth,
                node_type=node.node_type,
                ref=node.ref,
                label=node.label,
                heading=node.heading,
                raw_text=node.raw_text,
                source_fragment=node.source_fragment,
                source_locator=node.source_locator,
                text_hash=sha256(payload),
                public_ok=public_ok,
            )
            .returning(doc_nodes.c.id)
        ).scalar_one()
        node.db_id = node_id  # dynamic attr: the search indexer maps tree -> rows

        crumb = _path_crumb(node)
        child_path = path_parts + (
            [crumb] if crumb and node.node_type in {"part", "chapter", "section", "group", "schedule"} else []
        )
        for child in node.children:
            visit(child, node_id, depth + 1, child_path)

        if node.node_type in CLAUSE_TYPES and node.ref:
            clause_text = node.subtree_text()
            start, end = layout.get(id(node), (None, None))
            clause_rows.append(
                {
                    "source_version_id": version_id,
                    "doc_node_id": node_id,
                    "ref": node.ref,
                    "path": " > ".join(p for p in path_parts if p),
                    "ordering": node_seq,
                    "text": clause_text,
                    "text_hash": sha256(clause_text.encode()),
                    "public_ok": public_ok,
                    "span_start": start,
                    "span_end": end,
                    "derived_by": derived_by,
                    "valid_from": valid_from,
                    "normative": spans.is_normative(
                        "\n".join(t for t in (node.raw_text, *(c.subtree_text() for c in node.children)) if t),
                        status_hint=getattr(node, "status", ""),
                    ),
                }
            )

    for root in tree:
        visit(root, None, 0, [])
    return clause_rows


def _path_crumb(node: DocNode) -> str:
    """Spine crumb for clauses.path: 'TITLE VI — …' when both exist."""
    label = (node.label or "").strip()
    heading = (node.heading or node.source_locator.get("display_heading") or "").strip()
    if label and heading and heading != label:
        return f"{label} — {heading}"
    return heading or label or node.ref


def _clear_version_tree(conn: Connection, version_id: int) -> None:
    """Drop nodes/clauses/index rows for a version so persist_tree can reuse it."""
    unit_ids = [
        r[0]
        for r in conn.execute(sa.select(search_units.c.id).where(search_units.c.source_version_id == version_id))
    ]
    if unit_ids:
        # Never swallow a database error here: on PostgreSQL the transaction
        # would already be aborted and the rebuild below would fail anyway.
        if record.fts_available(conn, "search_units_fts"):
            record.drop_fts_rows(conn, "search_units_fts", unit_ids)
        record.rebuild_projection(conn, search_units, search_units.c.source_version_id == version_id)
    clause_ids = [
        r[0] for r in conn.execute(sa.select(clauses.c.id).where(clauses.c.source_version_id == version_id))
    ]
    if clause_ids:
        record.rebuild_projection(conn, citations, citations.c.from_clause_id.in_(clause_ids))
        record.rebuild_projection(conn, clause_annotations, clause_annotations.c.clause_id.in_(clause_ids))
        record.rebuild_projection(conn, clauses, clauses.c.id.in_(clause_ids))
    conn.execute(
        doc_nodes.update().where(doc_nodes.c.source_version_id == version_id).values(parent_id=None)
    )
    record.rebuild_projection(conn, doc_nodes, doc_nodes.c.source_version_id == version_id)
