# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Pull the published corpus snapshot from S3 into a local SQLite file.

Used by the Lambda explorer (read-only) and kept independent of the FastAPI
import graph so the refresh contract can be unit-tested without Mangum.
"""
from __future__ import annotations

import os
import time
from typing import Any

DB_LOCAL_PATH = "/tmp/clhear.db"
REFRESH_TTL_S = 300


def parse_uri(uri: str) -> tuple[str, str]:
    bucket, key = uri[len("s3://") :].split("/", 1)
    return bucket, key


def sync_snapshot(
    uri: str,
    state: dict[str, Any],
    *,
    local_path: str = DB_LOCAL_PATH,
    ttl_s: float = REFRESH_TTL_S,
    now: float | None = None,
    force: bool = False,
    s3_client: Any = None,
) -> bool:
    """Download `uri` when the local file is missing or the S3 ETag changed.

    Returns True when the local file was replaced. `state` is a mutable
    ``{"etag": str, "checked": float}`` dict owned by the caller so Lambda
    warm containers keep their last-seen ETag across invocations.
    """
    if not uri.startswith("s3://"):
        return False
    now = time.time() if now is None else now
    if not force and now - float(state.get("checked") or 0) < ttl_s:
        return False
    state["checked"] = now

    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3")
    bucket, key = parse_uri(uri)
    etag = s3_client.head_object(Bucket=bucket, Key=key)["ETag"]
    if (
        not force
        and state.get("etag") == etag
        and os.path.exists(local_path)
    ):
        return False
    tmp = f"{local_path}.new"
    # A download cut off by the Lambda init deadline leaves staging files that
    # would otherwise compete with this one for the ephemeral disk.
    directory, prefix = os.path.dirname(local_path) or ".", os.path.basename(tmp)
    for name in os.listdir(directory):
        if name.startswith(prefix):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
    s3_client.download_file(bucket, key, tmp)
    os.replace(tmp, local_path)
    state["etag"] = etag
    return True
