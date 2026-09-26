# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Current, text-free L0 control for private operator-exception reads.

The snapshot is historical evidence. It cannot authorize an exception read by
itself: each such read checks the current S3 object, without a freshness cache.
Only the original reviewer routes call this gate; public/model/export decisions
continue to use the publisher-permission ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from botocore.exceptions import ClientError

from app.clhear.l1 import operator_exceptions

SCHEMA = "clhear.operator-access-control.v1"
LABEL = "Internal-use override · publisher permission unverified"
MAX_BYTES = 8 * 1024 * 1024


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def control_uri(snapshot_uri):
    parsed = urlparse(snapshot_uri)
    if (parsed.scheme != "s3" or not parsed.netloc or parsed.username or parsed.password
            or not parsed.path.startswith("/webui/") or parsed.query or parsed.fragment
            or any(part in {".", "..", ""} for part in parsed.path.split("/")[1:])):
        raise ValueError("A private s3://bucket/webui/... snapshot URI is required")
    return f"s3://{parsed.netloc}{parsed.path.rsplit('/', 1)[0]}/operator-access-control.json"


def _location(snapshot_uri, region, s3_client):
    uri = control_uri(snapshot_uri)
    parsed = urlparse(uri)
    if s3_client is None:
        import boto3
        s3_client = boto3.client("s3", region_name=region)
    return uri, s3_client, {"Bucket": parsed.netloc, "Key": parsed.path.lstrip("/")}


def _read(s3, location):
    response = s3.get_object(**location)
    body = response["Body"]
    try:
        raw = body.read(MAX_BYTES + 1)
    finally:
        body.close()
    if len(raw) > MAX_BYTES:
        raise ValueError("Operator access control exceeds its size limit")
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get("schema") != SCHEMA
            or record.get("status") not in {"available", "updating"}
            or not isinstance(record.get("revision"), str)
            or not isinstance(record.get("published_at"), str)
            or record.get("sha256") != _hash({k: v for k, v in record.items() if k != "sha256"})):
        raise ValueError("Operator access control is invalid")
    if not response.get("ETag"):
        raise ValueError("Operator access control has no atomic object identity")
    return record, response["ETag"]


def _previous(s3, location):
    try:
        return _read(s3, location)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        return None, None


def _write(s3, location, uri, record, etag):
    record = {"schema": SCHEMA, "revision": str(uuid.uuid4()),
              "published_at": datetime.now(timezone.utc).isoformat(), **record}
    record["sha256"] = _hash(record)
    body = _bytes(record)
    if len(body) > MAX_BYTES:
        raise ValueError("Operator access control exceeds its size limit")
    s3.put_object(**location, Body=body, ContentType="application/json", CacheControl="private, no-store",
                  ServerSideEncryption="AES256", Metadata={"revision": record["revision"], "sha256": record["sha256"]},
                  **({"IfMatch": etag} if etag else {"IfNoneMatch": "*"}))
    return {**record, "uri": uri}


def invalidate_control(snapshot_uri, region, *, s3_client=None, mutation_id=None):
    """Deny exception reads BEFORE changing an activation/revocation ledger.

    The returned token is needed to publish the committed replacement. An
    unrelated snapshot publisher cannot clear a pending invalidation.
    """
    uri, s3, location = _location(snapshot_uri, region, s3_client)
    if mutation_id is not None and (not isinstance(mutation_id, str) or not mutation_id or len(mutation_id) > 512):
        raise ValueError("Operator mutation identity must be bounded nonempty text")
    previous, etag = _previous(s3, location)
    if previous and previous["status"] == "updating":
        if mutation_id and previous.get("mutation_id") == mutation_id:
            return {**previous, "uri": uri}  # Same durable command resumes its pending invalidation.
        raise RuntimeError("A different operator ledger mutation is pending")
    return _write(s3, location, uri, {"status": "updating", "invalidation_token": str(uuid.uuid4()),
                                    "mutation_id": mutation_id, "exceptions": [], "bindings": {}, "ledger_digest": None}, etag)


def publish_control(engine, snapshot_uri, region, *, s3_client=None, invalidation_token=None):
    uri, s3, location = _location(snapshot_uri, region, s3_client)
    previous, etag = _previous(s3, location)
    if previous and previous["status"] == "updating" and (
            not invalidation_token or invalidation_token != previous.get("invalidation_token")):
        raise RuntimeError("Operator access control has a pending ledger mutation")
    with engine.connect() as conn:
        state = operator_exceptions.control_state(conn)
    if state["status"] != "available":
        raise RuntimeError("Operator exception evidence is unavailable")
    # Preserve the complete append-only ledger digest as provenance, but only
    # the latest exact source identity binding can authorize current reads.
    # Adding another source leaves existing identities untouched; replacing a
    # manifest for this identity invalidates its older snapshot decision.
    current_bindings = {}
    for binding in state["bindings"]:
        identity = (binding["activation_id"], binding["source_key"], binding["canonical_url"])
        previous_binding = current_bindings.get(identity)
        if previous_binding is None or binding["binding_id"] > previous_binding["binding_id"]:
            current_bindings[identity] = binding
    record = {"status": "available", "ledger_digest": state["digest"],
              "exceptions": [{k: event[k] for k in ("exception_id", "activation_id", "action")}
                             for event in state["exceptions"]],
              "bindings": {str(binding["binding_id"]): binding["binding_hash"] for binding in current_bindings.values()}}
    published = _write(s3, location, uri, record, etag)
    return {**{k: published[k] for k in ("schema", "status", "revision", "published_at", "sha256", "uri", "ledger_digest")},
            "exception_count": len(record["exceptions"]), "binding_count": len(record["bindings"])}


def _configured():
    from app.clhear.l1.origin import production_worker
    from app.clhear.settings import get_settings
    uri = os.environ.get("CLHEAR_VIEWER_SNAPSHOT_S3_URI", "")
    if not uri and production_worker():
        raise RuntimeError("Production operator control requires the private viewer snapshot URI")
    return uri, get_settings().aws_region


def publish_configured_control(engine, *, invalidation_token=None):
    uri, region = _configured()
    return (publish_control(engine, uri, region, invalidation_token=invalidation_token) if uri
            else {"status": "not_configured", "invalidation_token": None})


def invalidate_configured_control(*, mutation_id=None):
    uri, region = _configured()
    return (invalidate_control(uri, region, mutation_id=mutation_id) if uri
            else {"status": "not_configured", "invalidation_token": None})


def verify_current_access(choice, *, snapshot_uri=None, region=None, s3_client=None):
    """Require this exact snapshot activation and source binding to be current.

    Unrelated new bindings may change the ledger digest. They do not revoke an
    unchanged binding. No error, absent configuration, or older cached response
    can authorize text. The result contains no publisher text or credentials.
    """
    denied = {**choice, "allowed": False, "display_label": LABEL,
              "publisher_permission_verified": False, "release_eligible": False}
    if (choice.get("authority_type") != "operator_exception" or not choice.get("allowed")
            or choice.get("operation") != "display_internal"):
        return {**denied, "reason": "operator_exception_display_not_authorized"}
    from app.clhear.settings import get_settings
    settings = get_settings()
    snapshot_uri = snapshot_uri or os.environ.get("CLHEAR_DB_S3_URI", "") or settings.clhear_preview_snapshot_s3_uri
    if not snapshot_uri:
        return {**denied, "reason": "operator_control_not_configured"}
    try:
        uri, s3, location = _location(snapshot_uri, region or settings.aws_region, s3_client)
        record, _ = _read(s3, location)  # A real current read on EVERY authorization, no stale fallback.
        if record["status"] != "available":
            return {**denied, "reason": "operator_control_updating"}
        active = any(event.get("exception_id") == choice["exception_id"]
                     and event.get("activation_id") == choice["activation_id"] and event.get("action") == "activate"
                     for event in record["exceptions"])
        # binding_hash covers the source key, canonical URL, activation and
        # complete scope/manifest identity, validated in the snapshot ledger.
        bound = (isinstance(record["bindings"], dict) and bool(choice.get("binding_hash"))
                 and record["bindings"].get(str(choice.get("binding_id"))) == choice["binding_hash"])
        if not active or not bound:
            return {**denied, "reason": "operator_control_binding_revoked_or_changed"}
        return {**denied, "allowed": True, "reason": "operator_exception_active",
                "control_revision": record["revision"], "control_ledger_digest": record["ledger_digest"],
                "control_checked_at": datetime.now(timezone.utc).isoformat(), "control_uri": uri}
    except Exception:
        return {**denied, "reason": "operator_control_unavailable"}
