# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Per-layer publication gates (HLD v2 I10, §4 "Evals gate").

A layer enters a public release only when every suite in its gate passed on the
latest run. Drift below gate freezes that layer's publication, emits
``clhear.<layer>.below_gate`` and a CloudWatch metric the alarm watches. The public
evals dashboard reads :func:`publish_summary` output (a published summary table),
never the evals store directly (§3).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import eval_runs
from app.clhear.platform import events as l0_events

log = logging.getLogger("clhear.gates")

# suite -> threshold semantics live in the suite; a gate is the set of suites that
# must all pass for the layer to publish. Later items extend these tuples.
LAYER_GATES: dict[str, tuple[str, ...]] = {
    "L0": ("l0_smoke",),
    "L1": ("l1_inventory_acceptance", "l1_boundary_f1"),
    "L2": ("l2_coverage", "l2_precision", "l2_dedupe", "l2_change_inference", "l2_basis_integrity"),
    "L3": ("l3_completeness", "l3_characteristics", "l3_reuse", "l3_precision", "l3_l5_referential"),
    "L4": ("l4_applicability", "l4_grounding"),
    "L5": ("l5_completeness", "l3_l5_referential"),
    "L6": ("l6_completeness", "l6_minimality", "l6_reference", "l6_explanation", "l6_citation"),
    "L7": ("l7_brier", "l7_linker", "l7_number_echo"),
    "L8": ("l8_reidentification", "l8_traceability", "l8_fill_rubric", "l8_k_anonymity"),
}

# Public gate thresholds (HLD §4) — published with the dashboard so the method is citable.
GATE_THRESHOLDS: dict[str, dict[str, str]] = {
    "L1": {"byte_fidelity": "100%", "family_completeness": ">=99%", "currency_tier_a": "<=24h",
           "clause_boundary_f1": ">=0.98"},
    "L2": {"coverage": ">=99%", "precision": ">=95%", "duplicate_rate": "<1%", "change_inference": ">=95%"},
    "L3": {"obligation_to_block": "100%", "characteristic_completeness": ">=95%", "expert_precision": ">=92%"},
    "L4": {"profile_validity": ">=99%", "applicability_pr": ">=95%"},
    "L5": {"junction_completeness": "100%", "expert_precision": ">=92%"},
    "L6": {"completeness": "100%", "minimality": "checked", "reference_agreement": ">=90%",
           "explanation_quality": ">=90%"},
    "L7": {"calibration": "Brier published", "linker_precision": ">=90%"},
    "L8": {"endorsed_fill_rubric": ">=85%", "reidentification": "pass", "traceability": "100%"},
}


def latest_suite_runs(engine: Engine, suites: tuple[str, ...], release: str | None = None) -> dict[str, dict]:
    """Most recent run per suite (optionally scoped to one release tag)."""
    out: dict[str, dict] = {}
    with engine.connect() as conn:
        for suite in suites:
            q = sa.select(eval_runs).where(eval_runs.c.suite == suite)
            if release:
                q = q.where(eval_runs.c.release == release)
            row = conn.execute(q.order_by(eval_runs.c.ran_at.desc(), eval_runs.c.id.desc()).limit(1)).mappings().first()
            if row:
                out[suite] = {
                    "passed": bool(row["passed"]),
                    "scores": row["scores"],
                    "ran_at": str(row["ran_at"]),
                    "release": row["release"],
                    "run_id": row["id"],
                }
    return out


def gate_status(engine: Engine, layer: str, release: str | None = None) -> dict:
    layer = layer.upper()
    suites = LAYER_GATES.get(layer, ())
    latest = latest_suite_runs(engine, suites, release)
    missing = [s for s in suites if s not in latest]
    failed = [s for s, r in latest.items() if not r["passed"]]
    evidence = None
    if layer == "L1":
        from app.clhear.l1.inventory import acceptance_status
        evidence = acceptance_status(engine)
        if not evidence["passed"] and "l1_inventory_acceptance" not in failed:
            failed.append("l1_inventory_acceptance")
    passed = not missing and not failed
    return {
        "layer": layer,
        "passed": passed,
        "suites": latest,
        "missing": missing,
        "failed": failed,
        "thresholds": GATE_THRESHOLDS.get(layer, {}),
        "inventory_acceptance": evidence,
    }


def publishable_layers(engine: Engine, candidate_layers: list[str] | tuple[str, ...], release: str | None = None) -> dict[str, dict]:
    """Layers whose gate passed. L0 is rails and always publishes its ledger."""
    result: dict[str, dict] = {}
    for layer in candidate_layers:
        result[layer.upper()] = gate_status(engine, layer, release)
    return result


def freeze_below_gate(engine: Engine, layer: str, status: dict, *, producer: str = "gates") -> str | None:
    """Emit `clhear.<layer>.below_gate` and publish the metric (alarm target)."""
    layer = layer.upper()
    if status.get("passed"):
        return None
    with engine.begin() as conn:
        event_id = l0_events.publish_layer_event(
            conn,
            layer=layer,
            event="below_gate",
            subject_ref=f"gate/{layer}",
            payload={"failed": status.get("failed", []), "missing": status.get("missing", [])},
            producer=producer,
        )
    _put_gate_metric(layer, 1.0)
    log.warning("layer %s below gate: failed=%s missing=%s", layer, status.get("failed"), status.get("missing"))
    return event_id


def _put_gate_metric(layer: str, value: float) -> None:
    try:
        import boto3

        from app.clhear.settings import get_settings

        boto3.client("cloudwatch", region_name=get_settings().aws_region).put_metric_data(
            Namespace="CLHEAR",
            MetricData=[{"MetricName": "LayerBelowGate", "Dimensions": [{"Name": "Layer", "Value": layer}],
                         "Value": value, "Unit": "Count"}],
        )
    except Exception:
        log.debug("gate metric not published (no AWS)", exc_info=True)


def publish_summary(engine: Engine, release: str | None = None) -> dict:
    """The summary table the public evals dashboard reads (never Langfuse directly)."""
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release": release,
        "layers": {},
    }
    for layer in LAYER_GATES:
        st = gate_status(engine, layer, release)
        summary["layers"][layer] = {
            "passed": st["passed"],
            "thresholds": st["thresholds"],
            "suites": {
                s: {"passed": r["passed"], "scores": r["scores"], "ran_at": r["ran_at"]} for s, r in st["suites"].items()
            },
            "missing": st["missing"],
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m app.clhear.platform.gates summary [release] | status <layer>"""
    import json
    import sys

    from app.clhear.db import get_engine, run_migrations

    argv = list(sys.argv[1:] if argv is None else argv)
    engine = get_engine()
    run_migrations(engine)
    cmd = argv[0] if argv else "summary"
    if cmd == "summary":
        print(json.dumps(publish_summary(engine, argv[1] if len(argv) > 1 else None), indent=2, default=str))
        return 0
    if cmd == "status":
        st = gate_status(engine, argv[1])
        print(json.dumps(st, indent=2, default=str))
        return 0 if st["passed"] else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
