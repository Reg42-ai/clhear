# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Model manifest per release (HLD v2 I6, §7.4).

A release freezes, for every Infer task class CLHEAR uses, the exact model id that
produced its determinations, plus the ladder Infer would have used and the
procurement class of each model. The manifest is published with the release and
referenced from every why-trail written during that derivation cycle.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.clhear.platform.task_classes import (
    DERIVATION_CLASSES,
    TASK_CLASSES,
    default_ladder,
    is_procurement_clean,
    origin_of,
)

MANIFEST_VERSION = 2


def build_model_manifest(
    *,
    release_id: str,
    resolved: dict[str, str] | None = None,
    ladders: dict[str, list[str]] | None = None,
    seed: int = 0,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``resolved`` maps task class -> model id actually used (from Infer
    ``/v1/route/explain`` or the router ledger); missing classes fall back to
    the first rung of the default ladder so the manifest is always complete."""
    resolved = resolved or {}
    ladders = ladders or {}
    classes: dict[str, dict[str, Any]] = {}
    for tc in TASK_CLASSES:
        ladder = ladders.get(tc) or default_ladder(tc)
        model = resolved.get(tc) or ladder[0]
        classes[tc] = {
            "model_id": model,
            "ladder": ladder,
            "derivation_class": tc in DERIVATION_CLASSES,
            "origin": origin_of(model),
            "procurement_clean": is_procurement_clean(model),
        }
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "release": release_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "provider": "reg42-infer",
        "hosting": "aws-bedrock",
        "seed": seed,
        "params": params or {"temperature": 0.0},
        "task_classes": classes,
    }
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


def manifest_hash(manifest: dict[str, Any]) -> str:
    body = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def check_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return violations: unknown classes, non-clean derivation models, bad hash."""
    problems: list[str] = []
    if manifest.get("manifest_hash") != manifest_hash(manifest):
        problems.append("manifest_hash mismatch")
    classes = manifest.get("task_classes") or {}
    for tc in TASK_CLASSES:
        if tc not in classes:
            problems.append(f"missing task class {tc}")
    for tc, entry in classes.items():
        if tc in DERIVATION_CLASSES and not is_procurement_clean(entry.get("model_id", "")):
            problems.append(f"{tc}: derivation class uses non-procurement-clean model {entry.get('model_id')}")
        for rung in entry.get("ladder", []):
            if tc in DERIVATION_CLASSES and not is_procurement_clean(rung):
                problems.append(f"{tc}: ladder contains non-procurement-clean model {rung}")
    if manifest.get("hosting") != "aws-bedrock":
        problems.append("hosting must be aws-bedrock (I6)")
    return problems


def frozen_model_ids(manifest: dict[str, Any]) -> dict[str, str]:
    return {tc: e["model_id"] for tc, e in (manifest.get("task_classes") or {}).items()}


def freeze_from_infer(release_id: str) -> dict[str, Any]:
    """Ask the configured model provider for the ladder per task class and freeze the first rung.
    Falls back to the default ladders when Infer is not configured (still valid,
    still procurement-clean, and the manifest records ``source``)."""
    from app.clhear.settings import get_settings

    settings = get_settings()
    resolved: dict[str, str] = {}
    ladders: dict[str, list[str]] = {}
    source = "defaults"
    base_url = getattr(settings, "infer_base_url", "")
    token = getattr(settings, "infer_token", "")
    if base_url and token:
        from app.clhear.platform.gateway import InferProvider

        provider = InferProvider(base_url, token)
        for tc in TASK_CLASSES:
            try:
                explained = provider.route_explain(tc)
            except Exception:
                continue
            ladder = explained.get("ladder") or []
            if ladder:
                ladders[tc] = list(ladder)
                resolved[tc] = explained.get("selected") or ladder[0]
                source = "infer"
    manifest = build_model_manifest(release_id=release_id, resolved=resolved, ladders=ladders)
    manifest["source"] = source
    manifest["manifest_hash"] = manifest_hash(manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m app.clhear.platform.manifest freeze <release_id>"""
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "freeze":
        m = freeze_from_infer(argv[1])
        problems = check_manifest(m)
        print(json.dumps(m, indent=2))
        if problems:
            print("\n".join("PROBLEM " + p for p in problems), file=sys.stderr)
            return 1
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
