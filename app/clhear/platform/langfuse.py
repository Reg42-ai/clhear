# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Langfuse export (HLD v2 §3 "Evals": self-hosted Langfuse + per-layer golden sets).

Every eval run is mirrored into the self-hosted Langfuse (``infra/langfuse.tf``)
as a trace with one score per metric, so suite history, per-layer regressions and
golden-set drift are browsable by maintainers. Public consumers never read
Langfuse: the evals dashboard reads :func:`gates.publish_summary` — the published
summary table — and nothing else (HLD v2 §3, "never from Langfuse directly").

Inert unless ``LANGFUSE_HOST`` + ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY``
are set; failures are logged and never fail a gate (the run is already in
``eval_runs``, which is the record of truth).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

log = logging.getLogger("clhear.langfuse")

_INGEST = "/api/public/ingestion"


def configured() -> bool:
    vals = [os.environ.get(k, "") for k in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")]
    return all(v and v != "CHANGEME" for v in vals)  # CHANGEME = SSM placeholder before the project exists


def _numeric_scores(scores: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (scores or {}).items():
        if isinstance(v, bool):
            out[k] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)):
            out[k] = float(v)
    return out


def build_batch(record: dict) -> list[dict]:
    """Langfuse ingestion events for one eval run: a trace + one score per numeric metric.

    Pure (no I/O) so the shape is testable; the trace carries the layer, suite,
    release and source key as tags/metadata and never any clause text.
    """
    now = datetime.now(timezone.utc).isoformat()
    trace_id = str(uuid.uuid4())
    suite = record["suite"]
    layer = suite.split("_", 1)[0].upper() if suite[:1] in "le" and suite[1:2].isdigit() else "L0"
    events = [{
        "id": str(uuid.uuid4()), "type": "trace-create", "timestamp": now,
        "body": {"id": trace_id, "name": f"eval:{suite}", "timestamp": record.get("ran_at") or now,
                 "tags": ["eval", layer, f"release:{record.get('release') or 'live'}"],
                 "metadata": {"suite": suite, "layer": layer, "release": record.get("release"),
                              "source_key": record.get("source_key"), "passed": bool(record.get("passed"))},
                 "release": record.get("release") or None, "public": False},
    }]
    for name, value in _numeric_scores(record.get("scores") or {}).items():
        events.append({"id": str(uuid.uuid4()), "type": "score-create", "timestamp": now,
                       "body": {"id": str(uuid.uuid4()), "traceId": trace_id, "name": name, "value": value, "dataType": "NUMERIC",
                                "comment": f"{suite} on {record.get('source_key') or 'corpus'}"}})
    events.append({"id": str(uuid.uuid4()), "type": "score-create", "timestamp": now,
                   "body": {"id": str(uuid.uuid4()), "traceId": trace_id, "name": "passed", "value": 1.0 if record.get("passed") else 0.0,
                            "dataType": "NUMERIC", "comment": "gate outcome"}})
    return events


def export_run(record: dict, *, client=None) -> bool:
    """Mirror one eval run. Returns True when accepted, False when skipped/failed."""
    if not configured():
        return False
    try:
        import httpx

        client = client or httpx.Client(timeout=10.0)
        resp = client.post(os.environ["LANGFUSE_HOST"].rstrip("/") + _INGEST, json={"batch": build_batch(record)},
                           auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]))
        if resp.status_code >= 300:
            log.warning("langfuse ingestion rejected (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception:  # noqa: BLE001 — telemetry never fails a gate
        log.warning("langfuse export failed", exc_info=True)
        return False
