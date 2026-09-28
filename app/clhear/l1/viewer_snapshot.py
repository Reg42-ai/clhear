# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L0-owned private candidate projection from the authoritative database.

The viewer is a separately generated SQLite projection, never a copy of the
operational database or an accepted release. Its full empty schema preserves
read-route compatibility. L1 and its evidence are copied for the corpus.
When the database holds a scoped derivation, that scope's
L2–L8 rows are copied with it; otherwise those layers stay empty. Sessions,
accounts, API credentials, model prompts and lease tokens do not cross this
boundary. No CLI or request-time export path is provided.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import sqlalchemy as sa

from app.clhear.db import make_engine, run_migrations
from app.clhear.l1 import cycles, discovery, inventory, models, origin, permissions, rights, workflow
from app.clhear.l1 import translation
from app.clhear.l1.translation_models import TABLES as ENGLISH_TABLES
from app.clhear.models import eval_runs, llm_calls, runs
from app.clhear.platform import record

CORPUS_TABLES = (models.source_families, models.sources, models.family_members,
                 models.source_versions, models.doc_nodes, models.clauses,
                 models.clause_annotations, models.citations, models.rights_records,
                 models.change_events, models.search_units)
EVIDENCE_TABLES = (permissions.source_permissions, inventory.inventory_snapshots,
                   inventory.inventory_audits, inventory.inventory_reviews, inventory.artifact_reviews,
                   workflow.jobs, workflow.tasks, workflow.steps, origin.origin_reviews,
                   cycles.cycles, cycles.children, cycles.queue, cycles.slot,
                   discovery.cycles, discovery.pages, runs, eval_runs, llm_calls)
STATE = sa.Table(
    "viewer_snapshot_state", sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("manifest", sa.JSON, nullable=False),
)
_REDACTED_FIELDS = {"error", "errors", "traceback", "stack", "prompt", "system_prompt", "completion",
                    "raw_response", "reasoning", "missing_preview", "missing_spans", "owner_token",
                    "password", "secret", "api_key", "access_token", "refresh_token", "authorization",
                    "raw_text", "source_fragment", "text", "expected_text", "parsed_text", "excerpt"}
_STRING_FIELDS = {
    "status", "stored_status", "stage", "fleet", "trigger", "source_key", "source", "adapter", "adapter_key",
    "job_id", "task_id", "step_id", "event_id", "event_key", "consumer", "subject_ref", "worker",
    "version_label", "version", "version_kind", "scope", "scope_version", "inventory_hash", "content_hash",
    "sha256", "implementation_sha256", "canonical_text_hash", "projection_hash", "bindings_hash",
    "audience", "dataset_kind", "origin", "freshness", "freshness_basis", "publisher_checked_at", "artifact_checked_at", "checked_at", "audited_at",
    "started_at", "finished_at", "created_at", "retrieved_at", "expires_at", "valid_from", "recorded_at", "ts", "measurement",
    "operation", "reason", "code", "name", "url", "uri", "artifact_uri", "canonical_url",
    "publisher_edition", "expected_edition", "evidence_ref", "approved_by", "reviewed_at", "coverage",
    "method", "model", "provider", "prompt_hash", "error_type", "readiness", "acceptance", "publication",
    "downstream", "key", "family", "short_name", "kind", "issuer", "jurisdiction", "license", "license_ref",
    "rights_basis", "publisher", "instrument", "relation", "tier", "topics", "registry_ids", "modules",
    "source_keys", "blocked_operations", "reasons", "depends_on", "channel", "doc", "celex", "celex_version",
    "ecfr_title", "ecfr_sections", "usc_title", "usc_sections", "as_of", "edition", "chapter", "chapters", "part",
    "sourcebook", "language", "allowed_origins", "hash", "permission_id", "finding_codes",
    "verification_id", "phase", "evidence_mode", "nightly_schedule_validation",
    "before_bindings_hash", "after_bindings_hash", "successful_sources", "failed_sources",
    "cycle_id", "child_id", "publisher_id", "profile_hash", "profile_version", "cycle_date", "role",
    "code_revision", "worker_image_digest", "parser_configuration_digest", "request_event_id", "event_time", "scheduled_for", "last_job_id",
    "adapters", "adapter_keys", "publication_cutoff", "frozen_at", "inventory_hashes", "snapshot_revision",
    "unverified_sources", "expected_source_keys", "manifest_hash", "command_event_id", "normalization_version", "parser_digest",
    "discovery_cycle_date", "progress_hash", "final_discovery_audit_id",
    "content_hash_method", "artifact_set_hash_method",
    "queued_at", "heartbeat_at", "lease_until", "active_cycle_id", "english_ready",
    "authority_type", "expiry_policy", "display_label", "evaluation_scope",
}


def configured_uri():
    return os.environ.get("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "")


def request_refresh(engine, *, reason, job_id=None):
    """Durable L0 export request, joining a caller Connection's transaction."""
    if not configured_uri():
        return None
    from app.clhear.platform.events import emit
    with (engine.begin() if isinstance(engine, sa.engine.Engine) else nullcontext(engine)) as conn:
        return emit(conn, layer="l0", kind="ViewerSnapshotRequested", subject_ref="viewer/current",
                    payload={"reason": reason, "job_id": job_id}, producer="l1.worker")


def _metadata(value, key=""):
    """An allowlist for display evidence, not a generic secret-key scrubber.

    Unknown free text and explicit error/provider payloads are omitted. Numeric
    metric maps and known identifiers/status/timing fields remain inspectable.
    """
    if key.lower() in _REDACTED_FIELDS:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if key in _STRING_FIELDS else None
    if isinstance(value, dict):
        if key == "sources_by_adapter":
            return {name: _metadata(keys, "source_keys") for name, keys in value.items()
                    if name in models.FLEET_SCHEDULES and isinstance(keys, list)}
        return {str(k): clean for k, v in value.items() if (clean := _metadata(v, str(k))) is not None}
    if isinstance(value, list):
        return [clean for v in value if (clean := _metadata(v, key)) is not None]
    return None


def _empty_schema(target):
    # This helper may initialize only a brand-new disposable SQLite projection.
    # Reject an operational DB even if a future caller passes the wrong engine.
    if target.dialect.name != "sqlite":
        raise ValueError("Viewer schema initialization requires a new empty SQLite database")
    with target.connect() as conn:
        if sa.inspect(conn).get_table_names():
            raise ValueError("Viewer schema initialization requires a new empty SQLite database")
    # Existing numbered migrations define all compatibility tables. They run
    # only on the newly created local candidate, never on the source engine.
    run_migrations(target)
    with target.begin() as conn:
        conn.exec_driver_sql("PRAGMA secure_delete = ON")
        for name in sa.inspect(conn).get_table_names():
            # Deleting FTS5 shadow tables individually corrupts its index.
            # The virtual table's DELETE maintains its own shadow structures.
            if name not in {"schema_migrations", "sqlite_sequence"} and not name.startswith("search_units_fts_"):
                conn.exec_driver_sql('DELETE FROM "' + name.replace('"', '""') + '"')
        STATE.create(conn, checkfirst=True)


def _required_tables(conn, *, historical_manifest=None):
    inspector = sa.inspect(conn)
    required = (*CORPUS_TABLES, *EVIDENCE_TABLES, *ENGLISH_TABLES)
    if historical_manifest is not None:
        additions = {origin.origin_reviews, cycles.cycles, cycles.children, discovery.cycles, discovery.pages,
                     cycles.queue, cycles.slot, *ENGLISH_TABLES}
        declared = historical_manifest.get("table_allowlist", [])
        # Read older valid worker projections without fabricating new evidence.
        # A projection declaring the new contract must contain its actual tables.
        required = tuple(table for table in required if table not in additions or table.name in declared
                         or (historical_manifest.get("cycle_id") and table not in {cycles.queue, cycles.slot, *ENGLISH_TABLES}))
    missing = [table.fullname for table in required
               if not inspector.has_table(table.name, schema=table.schema if conn.dialect.name == "postgresql" else None)]
    if missing:
        raise RuntimeError("Viewer snapshot requires migrated evidence tables: " + ", ".join(missing))


def _corpus_query(table, permitted_sources, public_sources):
    permitted_versions = sa.select(models.source_versions.c.id).where(models.source_versions.c.source_id.in_(permitted_sources))
    public_versions = sa.select(models.source_versions.c.id).where(models.source_versions.c.source_id.in_(public_sources))
    permitted_clauses = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(permitted_versions))
    public_clauses = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(public_versions))
    # Text is selected conditionally in the source database so an unapproved
    # source's text is not fetched into the snapshot worker at all.
    allowed = None
    publicly_allowed = None
    fields = set()
    if table is models.doc_nodes:
        allowed = table.c.source_version_id.in_(permitted_versions)
        publicly_allowed = table.c.source_version_id.in_(public_versions)
        fields = {"raw_text", "source_fragment", "heading", "label"}
    elif table is models.clauses:
        allowed = table.c.source_version_id.in_(permitted_versions)
        publicly_allowed = table.c.source_version_id.in_(public_versions)
        fields = {"text", "path"}
    elif table is models.clause_annotations:
        allowed = table.c.clause_id.in_(permitted_clauses)
        fields = {"summary"}
    elif table is models.citations:
        allowed = table.c.from_clause_id.in_(permitted_clauses)
        fields = {"raw_text", "reason"}
    elif table is models.rights_records:
        allowed = table.c.source_id.in_(permitted_sources)
        fields = {"basis_ref"}
    elif table is models.search_units:
        # Search remains public-corpus-only; internal text is read explicitly
        # in the authenticated source inspector, not copied to a public index.
        allowed = table.c.source_version_id.in_(public_versions)
        publicly_allowed = allowed
        fields = {"text", "heading", "path", "short_name"}
    columns = []
    for column in table.c:
        if column.name.startswith("embedding") or column.name == "embedded_at":
            columns.append(sa.cast(sa.null(), column.type).label(column.name))
        elif column.name in fields:
            columns.append(sa.case((allowed, column), else_="").label(column.name))
        elif table is models.doc_nodes and column.name == "source_locator":
            columns.append(sa.case((allowed, column), else_=sa.literal({}, type_=column.type)).label(column.name))
        elif table is models.change_events and column.name == "clause_refs":
            columns.append(sa.case((table.c.source_id.in_(permitted_sources), column),
                else_=sa.literal([], type_=column.type)).label(column.name))
        elif column.name == "public_ok" and publicly_allowed is not None:
            columns.append(sa.and_(column, publicly_allowed).label(column.name))
        else:
            columns.append(column)
    query = sa.select(*columns)
    eligible_sources = sa.select(models.sources.c.id).where(origin.corpus_sources_predicate())
    eligible_versions = sa.select(models.source_versions.c.id).where(models.source_versions.c.source_id.in_(eligible_sources))
    eligible_clauses = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(eligible_versions))
    if table is models.sources:
        query = query.where(table.c.id.in_(eligible_sources))
    elif table is models.source_families:
        query = query.where(sa.or_(
            table.c.id.in_(sa.select(models.sources.c.family_id).where(models.sources.c.id.in_(eligible_sources))),
            table.c.id.in_(sa.select(models.family_members.c.family_id).where(models.family_members.c.source_id.in_(eligible_sources))),
        ))
    elif "source_id" in table.c:
        query = query.where(table.c.source_id.in_(eligible_sources))
    elif "source_version_id" in table.c:
        query = query.where(table.c.source_version_id.in_(eligible_versions))
    elif table is models.clause_annotations:
        query = query.where(table.c.clause_id.in_(eligible_clauses))
    elif table is models.citations:
        query = query.where(table.c.from_clause_id.in_(eligible_clauses))
    if table is models.clause_annotations:
        query = query.where(allowed)  # topic arrays can contain source excerpts
    return query


def _evidence_query(table):
    query = sa.select(table)
    if table is runs:
        query = query.where(sa.or_(table.c.fleet.like("l1.%"), table.c.fleet == "worker"))
    elif table is llm_calls:
        query = query.where(table.c.fleet.like("l1.%"))
    elif table is eval_runs:
        from app.clhear.platform.evals import SOURCE_SUITES
        query = query.where(sa.or_(table.c.suite.like("l1_%"), table.c.suite.in_(SOURCE_SUITES)))
    return query


def _clean_row(table, row):
    out = dict(row)
    for key, value in list(out.items()):
        if key in {"owner_token", "lease_token", "error", "reasoning", "routing_reason"}:
            out[key] = None
        elif key in {"model_manifest", "review"}:
            # Free-form model/reviewer context can contain source excerpts or
            # provider payloads. Exact parser and formal reviewed evidence live
            # in the dedicated inventory/run ledgers copied separately.
            out[key] = None
        elif table in {runs, eval_runs, llm_calls, workflow.jobs, workflow.tasks, workflow.steps,
                       cycles.cycles, cycles.children, discovery.cycles, discovery.pages} and isinstance(value, (dict, list)):
            out[key] = _metadata(value, key) or ({} if isinstance(value, dict) else [])
    return out


def _authorization_binding(conn):
    """Current exact grant decisions and source rights labels, without text."""
    binding = []
    for source in conn.execute(sa.select(models.sources).order_by(models.sources.c.key)).mappings():
        protected = permissions.required_for(source)
        decisions = {op: {k: choice.get(k) for k in ("permission_id", "allowed", "reason", "expires_at", "authority_type", "release_eligible")}
                     for op in ("store", "parse", "display_internal", "display_public", "infer", "derive", "translate")
                     for choice in [(permissions.candidate_decision(conn, source["key"], op, canonical_url=source["canonical_url"])
                                     if op in {"store", "parse", "display_internal"}
                                     else permissions.decision(conn, source["key"], op))]}
        binding.append({"source_key": source["key"], "protected": protected,
                        "license": source["license"], "rights_basis": source["rights_basis"], "decisions": decisions})
    return binding


def compile_viewer_snapshot(engine, destination: Path, *, job_id=None):
    """Compile one consistent, private candidate; fail before any publication."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    target = make_engine(f"sqlite:///{destination}")
    try:
        _empty_schema(target)
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                if engine.dialect.name == "postgresql":
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                elif engine.dialect.name == "sqlite":
                    conn.exec_driver_sql("BEGIN")  # pin SQLite's legacy driver to a real read transaction
                _required_tables(conn)
                authorization_binding = _authorization_binding(conn)
                source_rows = list(conn.execute(sa.select(models.sources).where(origin.corpus_sources_predicate())).mappings())
                excluded_test_count = conn.execute(sa.select(sa.func.count()).select_from(models.sources)
                                                  .where(~origin.corpus_sources_predicate())).scalar_one()
                permitted, publicly_allowed, redacted = [], [], []
                for source in source_rows:
                    if permissions.required_for(source):
                        internal = permissions.candidate_decision(conn, source["key"], "display_internal", canonical_url=source["canonical_url"])["allowed"]
                        public = permissions.decision(conn, source["key"], "display_public")["allowed"]
                        store = permissions.candidate_decision(conn, source["key"], "store", canonical_url=source["canonical_url"])["allowed"]
                        may_copy = store and (internal or public)
                    else:
                        public = source["license"] == "open" and rights.republishable(source["rights_basis"])
                        may_copy = public
                    if may_copy:
                        permitted.append(source["id"])
                    else:
                        redacted.append(source["key"])
                    if may_copy and public:
                        publicly_allowed.append(source["id"])
                counts = {}
                eligible_versions = conn.execute(sa.select(models.source_versions.c.id).where(
                    models.source_versions.c.source_id.in_([s["id"] for s in source_rows]))).scalars().all()
                english_queries = translation.snapshot_queries(conn, eligible_versions)
                with target.begin() as out:
                    for table in (*CORPUS_TABLES, *EVIDENCE_TABLES, *ENGLISH_TABLES):
                        query = (english_queries[table.name] if table in ENGLISH_TABLES else
                                 cycles.read_query(conn) if table is cycles.cycles else
                                 _corpus_query(table, permitted, publicly_allowed) if table in CORPUS_TABLES else _evidence_query(table))
                        count = 0
                        result = conn.execute(query).mappings()
                        while batch := result.fetchmany(250):
                            rows = [_clean_row(table, row) for row in batch]
                            out.execute(table.insert(), rows)
                            count += len(rows)
                        counts[table.name] = count
                    if record.fts_available(out, "search_units_fts"):  # the SQLite candidate only
                        out.exec_driver_sql("INSERT INTO search_units_fts(rowid, text) SELECT id, text FROM search_units WHERE text <> ''")
                    from app.clhear.scope_projection import project as project_scope

                    projected = project_scope(conn)
                    omitted_layers = [f"L{n}" for n in range(2, 9)]
                    derived_scope = None
                    allowlist = [t.name for t in (*CORPUS_TABLES, *EVIDENCE_TABLES, *ENGLISH_TABLES)]
                    if projected:
                        for table, rows in projected["tables"]:
                            for start in range(0, len(rows), 200):
                                out.execute(table.insert(), rows[start:start + 200])
                            counts[table.name] = len(rows)
                            if table.name not in allowlist:
                                allowlist.append(table.name)
                        omitted_layers = projected["omitted_layers"]
                        derived_scope = projected["derived_scope"]
                    completed_cycle = conn.execute(cycles.read_query(conn).where(
                        cycles.cycles.c.cycle_id == job_id,
                        cycles.cycles.c.status.in_(cycles.TERMINAL_CYCLE))).mappings().first() if job_id else None
                    manifest = {"status": "available", "kind": "candidate_viewer", "viewer_snapshot": True,
                                "revision": str(uuid.uuid4()), "generated_at": datetime.now(timezone.utc).isoformat(),
                                "database_backend": engine.dialect.name,
                                "source_environment": "authoritative_postgresql" if engine.dialect.name == "postgresql" else "local_sqlite_test",
                                "worker_job_id": job_id, "cycle_id": completed_cycle["cycle_id"] if completed_cycle else None,
                                "cycle_finished_at": str(completed_cycle["finished_at"]) if completed_cycle else None,
                                "cycle_status": completed_cycle["status"] if completed_cycle else None,
                                "accepted_release": False, "audience": "restricted-reviewers",
                                "authorization_binding": authorization_binding,
                                "counts": counts, "redacted_source_keys": sorted(redacted),
                                "excluded_test_sources": excluded_test_count,
                                "omitted_layers": omitted_layers,
                                **({"derived_scope": derived_scope} if derived_scope else {}),
                                "omitted_operational_data": ["accounts", "sessions", "API credentials", "model prompts", "lease tokens", "private runtime error details"],
                                "table_allowlist": allowlist}
                    out.execute(STATE.insert().values(id=1, manifest=manifest))
        with target.connect() as check:
            if check.exec_driver_sql("PRAGMA integrity_check").scalar_one() != "ok":
                raise RuntimeError("Candidate viewer SQLite integrity verification failed")
        return manifest
    except Exception:
        target.dispose()
        destination.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()


def _aware(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def publish_viewer_snapshot(engine, uri, region, *, job_id=None, s3_client=None, requested_at=None):
    """L0 publishes only after local compilation and integrity verification.

    A single atomic S3 PutObject replaces the private viewer object. A failed
    compilation/upload leaves the previous object intact. This never changes
    accepted-release pointers or the authoritative DATABASE_URL.

    ``requested_at`` is the refresh request's event time. Every finished task
    requests a refresh, and one compile of the 2 GB projection takes minutes,
    so a burst of requests is answered by the first compile that *started*
    after the request; the rest are coalesced instead of rebuilt.
    """
    parsed = urlparse(uri)
    if (parsed.scheme != "s3" or not parsed.netloc or parsed.username or parsed.password
            or not parsed.path.startswith("/webui/") or parsed.query or parsed.fragment):
        raise ValueError("A configured private s3://bucket/webui/... viewer URI is required")
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3", region_name=region)
    # Compare-and-swap prevents a slower event from replacing a newer viewer.
    # Capture the ETag before compilation, not just before uploading.
    from botocore.exceptions import ClientError
    started_at = datetime.now(timezone.utc)
    try:
        previous = s3_client.head_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
        expected_etag = previous["ETag"]
        condition = {"IfMatch": expected_etag}
        metadata = {str(k).lower(): v for k, v in (previous.get("Metadata") or {}).items()}
        compiled_from, wanted = _aware(metadata.get("compiled-from")), _aware(requested_at)
        if compiled_from and wanted and compiled_from > wanted:
            return {"status": "coalesced", "coalesced": True, "kind": "candidate_viewer", "viewer_snapshot": True,
                    "accepted_release": False, "snapshot_uri": uri, "revision": metadata.get("revision"),
                    "sha256": metadata.get("sha256"), "requested_at": wanted.isoformat(),
                    "compiled_from": compiled_from.isoformat()}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        condition = {"IfNoneMatch": "*"}
    with tempfile.TemporaryDirectory(prefix="clhear-viewer-") as directory:
        path = Path(directory) / "candidate.db"
        manifest = compile_viewer_snapshot(engine, path, job_id=job_id)
        hasher = hashlib.sha256()
        with path.open("rb") as content:
            for chunk in iter(lambda: content.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        with engine.connect() as conn:
            if _authorization_binding(conn) != manifest["authorization_binding"]:
                raise PermissionError("Source permissions changed while compiling the viewer; rerun required")
        put_conditionally(s3_client, parsed.netloc, parsed.path.lstrip("/"), path, condition=condition,
            ContentType="application/vnd.sqlite3", CacheControl="private, no-store", ServerSideEncryption="AES256",
            Metadata={"revision": manifest["revision"], "sha256": digest, "kind": "candidate-viewer",
                      "source-environment": manifest["source_environment"],
                      "compiled-from": started_at.isoformat()})
        return {**manifest, "snapshot_uri": uri, "sha256": digest, "byte_count": path.stat().st_size}


# S3 rejects a single PutObject above 5 GiB. Larger snapshots go up in parts and
# become visible only when the conditional CompleteMultipartUpload succeeds, so
# a concurrent writer still cannot replace a newer viewer.
SINGLE_PUT_LIMIT = 4 * 1024 ** 3
PART_SIZE = 256 * 1024 ** 2


def put_conditionally(s3_client, bucket: str, key: str, path: Path, *, condition: dict, **fields) -> None:
    size = Path(path).stat().st_size
    if size <= SINGLE_PUT_LIMIT:
        with Path(path).open("rb") as body:
            s3_client.put_object(Bucket=bucket, Key=key, Body=body, **fields, **condition)
        return
    upload = s3_client.create_multipart_upload(Bucket=bucket, Key=key, **fields)["UploadId"]
    parts = []
    try:
        with Path(path).open("rb") as body:
            for number, chunk in enumerate(iter(lambda: body.read(PART_SIZE), b""), 1):
                etag = s3_client.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=number, Body=chunk)["ETag"]
                parts.append({"PartNumber": number, "ETag": etag})
        s3_client.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload,
                                            MultipartUpload={"Parts": parts}, **condition)
    except Exception:
        s3_client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
        raise


def derived_reference_keys(engine) -> list[str]:
    """Reference source keys recorded when a scope's derived rows were copied."""
    scope = read_viewer_state(engine).get("derived_scope") or {}
    return list(scope.get("reference") or [])


def read_viewer_state(engine):
    with engine.connect() as conn:
        if sa.inspect(conn).has_table(STATE.name):
            value = conn.execute(sa.select(STATE.c.manifest).where(STATE.c.id == 1)).scalar_one_or_none()
            if value:
                return value
    return {"status": "not_a_viewer_snapshot", "viewer_snapshot": False, "kind": "direct_database",
            "database_backend": engine.dialect.name,
            "source_environment": "direct_postgresql" if engine.dialect.name == "postgresql" else "local_sqlite",
            "accepted_release": False, "omitted_layers": []}


def public_viewer_state(engine) -> dict:
    """The viewer state for pages and APIs. The per-source authorization binding
    (several MB) is publish-time evidence; readers get its size and digest."""
    state = dict(read_viewer_state(engine))
    binding = state.pop("authorization_binding", None)
    if binding is not None:
        encoded = json.dumps(binding, sort_keys=True, default=str).encode()
        state["authorization_binding"] = {"sources": len(binding), "sha256": hashlib.sha256(encoded).hexdigest()}
    return state
