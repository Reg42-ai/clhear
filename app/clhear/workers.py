# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""SQS consumer entrypoint (ECS clhear-workers): python -m app.clhear.workers

The worker loop does two jobs:
  1. relay the l0 outbox to SQS (events.relay_forever semantics, interleaved)
  2. consume envelopes from SQS and dispatch to per-kind handlers

Consumers are idempotent on event_id: each handled envelope is recorded in the
l0_platform.runs ledger and skipped if already present (HLD §6.1, §7.1).
"""
import json
import logging
import os
import time
import uuid
from contextlib import ExitStack
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.models import runs
from app.clhear.platform import events as l0_events
from app.clhear.platform import proposals as l0_proposals
from app.clhear.platform.events import Envelope
from app.clhear.platform.gateway import Gateway, Provider, parse_json_object
from app.clhear.settings import get_settings

log = logging.getLogger("clhear.workers")

DUMMY_FLEET = "dummy"


def run_dummy_fleet(engine: Engine, subject_ref: str = "dummy/rehearsal-1") -> str:
    """P0 rehearsal fleet: one data change + outbox event in the same transaction."""
    started = time.monotonic()
    with engine.begin() as conn:
        event_id = l0_events.emit(
            conn,
            layer="l0",
            kind="DummyChanged",
            subject_ref=subject_ref,
            payload={"note": "dummy-fleet rehearsal"},
            producer="fleet.dummy",
        )
        conn.execute(
            runs.insert().values(
                fleet=DUMMY_FLEET,
                trigger="manual",
                inputs={"subject_ref": subject_ref},
                outputs={"event_id": event_id},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    return event_id


def handle_dummy_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Rehearsal consumer: one routed call (triage-shaped) -> one proposal."""
    from app.clhear.platform.router import complete

    result = complete(
        gateway,
        "dummy.triage",
        prompt=f"Classify this candidate for {envelope.subject_ref}: {json.dumps(envelope.payload)}",
        system="Respond with JSON: {\"classification\": ..., \"confidence\": ...}",
        required_keys=["classification", "confidence"],
    )
    triage = parse_json_object(result.text)
    with engine.begin() as conn:
        proposal_id = l0_proposals.create_proposal(
            conn,
            layer="l0",
            kind="dummy_candidate",
            subject_ref=envelope.subject_ref,
            draft={"triage": triage, "event_id": envelope.event_id},
            rationale="dummy-fleet rehearsal proposal",
            confidence=float(triage.get("confidence", 0)),
        )
    return {"proposal_id": proposal_id, "cost_usd": result.cost_usd}


class AdapterRunIncomplete(RuntimeError):
    """Persisted candidate work is resumable; this event has not succeeded."""


def run_adapter_fleet(
    engine: Engine, adapter_key: str, gateway: Gateway | None = None, *,
    force_nightly: bool = False, nightly_only: bool = False,
    job_id: str | None = None, event_key: str | None = None, trigger: str = "manual",
    cycle_context: dict | None = None, source_keys: list[str] | None = None, discover: bool | None = None,
) -> dict:
    """Execute the same durable L1 workflow for manual and scheduled requests.

    Source tasks finish independently. Redelivery skips completed imports and
    reruns final audits/evals; a failed task never produces a handled marker.
    Downstream derivation has its own fleet and is never run inline here.

    ``source_keys`` fixes the scope to exactly those registered documents and
    ``discover=False`` keeps the inventory audit from expanding the frontier:
    a host scope build runs that way, so only the declared documents import.
    """
    from app.clhear.l1 import families, inventory, pipeline, source_registry, workflow
    from app.clhear.l1.adapters import CITATOR_KEYS
    from app.clhear.l1.fleet import adapter_for, fleet_plan

    if nightly_only:
        raise ValueError("AdapterRunRequested executes L1 only; submit downstream work to its owning fleet")
    settings = get_settings()
    if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" or settings.clhear_snapshot_s3_uri:
        store = pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
    else:
        store = pipeline.LocalStore(settings.clhear_artifacts_dir)
    source_registry.seed(engine)
    event_key = event_key or str(uuid.uuid4())
    job_id = job_id or workflow.job_id_for(event_key, adapter_key)
    job = workflow.ensure_job(engine, job_id, adapter_key, trigger, event_key)
    workflow.update_job(engine, job_id, "running")
    scope = cycle_context["scope"] if cycle_context else "registered"
    fixed_scope = sorted(set(source_keys)) if source_keys is not None else None
    if discover is None:
        discover = False
    statuses, failures = {}, []
    try:
        with workflow.bind_execution(engine, job_id):
            frozen_cycle = bool(cycle_context)
            with workflow.stage("discovery", {"scope": scope, "fixed_scope": fixed_scope, "discover": bool(discover),
                                              "operation": "frozen_cycle_inventory_reference" if frozen_cycle else
                                              "fixed_scope_reconciliation" if fixed_scope is not None else
                                              "discovery_and_database_reconciliation"}) as step:
                before = ({"inventory_hash": cycle_context["inventory_hash"], "audit_id": None} if frozen_cycle else
                          inventory.run_inventory_audit(engine, store, job_id=job_id, scope=scope, discover=bool(discover)))
                step.details.update(audit_id=before.get("audit_id"), inventory_hash=before.get("inventory_hash"))
        plan = list(fleet_plan(adapter_key))
        seen = {adapter.meta().source_key for _, adapter in plan}
        if fixed_scope is None:
            for entry in inventory.planned_entries(engine, scope=scope, adapter_key=adapter_key,
                        **({"audit_id": cycle_context["audit_id"]} if cycle_context and cycle_context.get("audit_id") else {})):
                if entry["key"] not in seen:
                    plan.append((entry, adapter_for(entry)))
                    seen.add(entry["key"])
        else:
            # Discovered documents are never pulled into a fixed scope; a missing
            # registered adapter is a failure of the scope, not a silent narrowing.
            missing = set(fixed_scope) - seen
            failures.extend(f"Fixed-scope document adapter unavailable: {key}" for key in sorted(missing))
            plan = [(entry, adapter) for entry, adapter in plan if adapter.meta().source_key in fixed_scope]
            seen = {adapter.meta().source_key for _, adapter in plan}
        if cycle_context and cycle_context["source_keys"] is not None:
            requested_keys = set(cycle_context["source_keys"])
            missing = requested_keys - {adapter.meta().source_key for _, adapter in plan}
            failures.extend(f"Frozen document adapter unavailable: {key}" for key in sorted(missing))
            plan = [(entry, adapter) for entry, adapter in plan if adapter.meta().source_key in requested_keys]
        frozen = job.get("summary") or {}
        if "source_keys" in frozen:
            expected_keys = set(frozen["source_keys"])
            plan = [(entry, adapter) for entry, adapter in plan if adapter.meta().source_key in expected_keys]
            missing_adapters = expected_keys - {adapter.meta().source_key for _, adapter in plan}
            failures.extend(f"Frozen document adapter unavailable: {key}" for key in sorted(missing_adapters))
            inventory_hash = frozen["inventory_hash"]
        else:
            inventory_hash = before.get("inventory_hash")
            workflow.update_job(engine, job_id, "running", {
                "source_keys": [adapter.meta().source_key for _, adapter in plan],
                "inventory_hash": inventory_hash, "frozen_at": workflow.utcnow().isoformat(),
                "scope": scope, "fixed_scope": fixed_scope, "discover": bool(discover),
            })
        if before.get("inventory_hash") != inventory_hash:
            failures.append("Inventory changed since this job was frozen; newly discovered documents require a new job")
        if not plan:
            failures.append("No importable document adapters in requested scope")
        if cycle_context:
            from app.clhear.l1 import cycles
            cycles.freeze_child(engine, cycle_context, job_id,
                                [adapter.meta().source_key for _, adapter in plan], inventory_hash)
        for entry, adapter in plan:
            source_key = adapter.meta().source_key
            task_id = workflow.ensure_task(engine, job_id, source_key, f"l1.{adapter.key}")
            token = None
            try:
                token = workflow.claim_task(engine, task_id)
                if token is None:
                    previous = workflow.task_info(engine, task_id)
                    status = previous["summary"].get("status", previous["status"])
                    if previous["status"] == "blocked":
                        failures.append(source_key)
                else:
                    with workflow.bind_execution(engine, job_id, task_id, token), workflow.heartbeat(
                        lambda: workflow.heartbeat_task(engine, task_id, token)
                    ):
                        if getattr(adapter, "declaration_gap", None):
                            with workflow.stage("permission", {"source": source_key, "declaration_gap": adapter.declaration_gap}) as step:
                                step.status = "blocked"
                            summary = {"status": "source-blocked", "source": source_key,
                                       "declaration_gap": adapter.declaration_gap, "freshness": "not_checked"}
                        else:
                            summary = pipeline.ingest(engine, adapter, store, trigger=trigger, gateway=gateway,
                                                      job_id=job_id, index_embeddings=False)
                        status = summary.get("status", "failed")
                        if status in {"added", "amended", "unchanged", "up-to-date"} and summary.get("source_version_id"):
                            _record_import_language(engine, entry, summary, store=store)
                        # Citator writes belong to the same source task and failure boundary.
                        if entry is None and adapter.key in CITATOR_KEYS and status in {"added", "amended", "unchanged", "up-to-date"}:
                            families.sync_citator(engine, adapter, trigger=trigger, job_id=job_id)
                    success = status in {"added", "amended", "unchanged", "up-to-date"}
                    skipped = status in {"out-of-scope", "not-published", "catalog-page"}
                    listed = status in {"rights-blocked", "source-blocked", "awaiting-artifact",
                                       "catalog-page", "out-of-scope", "not-published", "not-fully-successful"}
                    if not success and not skipped and status == "failed" and "failure" not in summary:
                        from app.clhear.platform import failures as failure_details
                        summary["failure"] = failure_details.describe(RuntimeError(summary.get("error") or status),
                                                                      source=source_key, worker="l1", task_id=task_id,
                                                                      stage=workflow.current_stage())
                    workflow.finish_task(engine, task_id, token,
                        status="completed" if success else "blocked" if listed else "failed",
                        summary=summary, error=None if success or skipped or status == "not-fully-successful" else (summary.get("failure") or summary.get("error", status)))
                    token = None
                    if not success and not skipped:
                        failures.append(source_key)
            except Exception as exc:
                log.exception("L1 source task failed for %s", source_key)
                status = "failed"
                failures.append(source_key)
                if token:
                    from app.clhear.platform import failures as failure_details
                    info = workflow.task_info(engine, task_id)
                    detail = failure_details.describe(exc, source=source_key, worker="l1", task_id=task_id,
                                                      stage=workflow.current_stage(), attempt=info.get("attempt"))
                    try:
                        workflow.finish_task(engine, task_id, token, status="failed", error=exc,
                                             summary={"status": "failed", "source": source_key, "failure": detail})
                    except workflow.LeaseLost:
                        log.warning("source task lease was lost: %s", task_id)
            statuses[status] = statuses.get(status, 0) + 1
        if cycle_context:
            # Aggregate acceptance belongs after every lane. An unrelated
            # publisher's unresolved licence is not a retryable adapter error.
            with engine.connect() as conn:
                task_rows = list(conn.execute(sa.select(workflow.tasks).where(workflow.tasks.c.job_id == job_id)).mappings())
            execution_failed = any(t["status"] not in {"completed", "blocked"} for t in task_rows)
            execution_failed = execution_failed or any(f not in seen and f != "No importable document adapters in requested scope" for f in failures)
            retryable = any(t["status"] in {"queued", "retrying", "running"} and t["attempt"] < t["max_attempts"] for t in task_rows)
            result = {"adapter": adapter_key, "job_id": job_id, "cycle_id": cycle_context["cycle_id"],
                      "ran": len(plan), "statuses": statuses, "failures": failures,
                      "execution_failed": execution_failed, "retryable": retryable,
                      "acceptance": "pending_cycle_evaluation", "publication": "blocked", "downstream": "held"}
            with workflow.bind_execution(engine, job_id), workflow.stage("readback_evals", {"role": "aggregate_after_all_children", "cycle_id": cycle_context["cycle_id"]}) as step:
                step.status = "deferred"
            workflow.update_job(engine, job_id, "retrying" if retryable else "failed" if execution_failed else
                                "completed_for_review" if failures else "completed", result)
            cycles.finish_child(engine, cycle_context, result, retryable=retryable)
            if execution_failed:
                raise AdapterRunIncomplete(f"{job_id}: source execution failed; inspect durable cycle evidence")
            return result
        with workflow.bind_execution(engine, job_id):
            with workflow.stage("database_reconciliation", {"scope": scope}) as step:
                after = inventory.run_inventory_audit(engine, store, job_id=job_id, scope=scope, discover=False)
                step.details.update(audit_id=after.get("audit_id"), inventory_hash=after.get("inventory_hash"))
            with workflow.stage("readback_evals") as step:
                from app.clhear.platform import evals as l1_evals
                completeness = l1_evals.run_suite(engine, "l1_completeness", release=job_id)
                global_acceptance = l1_evals.run_suite(engine, "l1_inventory_acceptance", release=job_id)
                boundary = l1_evals.run_suite(engine, "l1_boundary_f1", release=job_id)
                schedule = l1_evals.run_suite(engine, "l1_schedule_kept", release=job_id)
                _put_schedule_metric(schedule["scores"].get("missed_count", 0))
                source_results = {}
                for _entry, adapter in plan:
                    key = adapter.meta().source_key
                    source_results[key] = l1_evals.run_source_evals(engine, key, release=job_id)
                source_checks = [row for rows in source_results.values() for row in rows]
                def passed(checks):
                    return bool(checks) and all(row.get("passed") and not row.get("scores", {}).get("not_evaluated")
                                               and not row.get("scores", {}).get("n/a") for row in checks)
                # Global suites remain visible release blockers until all
                # registered L1 is ready. E1–E7 remain transparent diagnostic evidence. Some legitimately
                # have no historical amendment fixture on a first version. The
                # acceptance audit supplies mandatory original/binding checks.
                mandatory = [boundary]
                if scope == "registered":
                    mandatory += [completeness, global_acceptance, schedule]
                evals_passed = passed(mandatory)
                scope_acceptance = inventory.acceptance_status(engine, scope=scope)
                registered_acceptance = scope_acceptance if scope == "registered" else inventory.acceptance_status(engine, scope="registered")
                step.details.update(completeness=completeness, inventory_acceptance=global_acceptance,
                                    boundary=boundary, schedule=schedule, sources=source_results,
                                    source_suites_role="diagnostic; original/binding checks are mandatory in scope acceptance",
                                    scope_acceptance={k: v for k, v in scope_acceptance.items() if k != "evidence"},
                                    passed=evals_passed)
                if not evals_passed:
                    step.status = "blocked"
                    failures.append("Mandatory scope evaluations failed or were not evaluated")
                if not scope_acceptance.get("passed"):
                    step.status = "blocked"
                    failures.append("Scope acceptance unresolved: " + ", ".join(scope_acceptance.get("reasons", [])))
                if after.get("inventory_hash") != inventory_hash:
                    failures.append("Post-import inventory differs from the frozen job inventory")
                if scope_acceptance.get("inventory_hash") != inventory_hash:
                    failures.append("Scope acceptance is not bound to the frozen job inventory")
            ready = not failures and registered_acceptance.get("passed") and passed([completeness, global_acceptance, boundary, schedule])
            with workflow.stage("publication", {"accepted": False, "readiness": "ready_for_l0" if ready else "blocked",
                    "reason": "Only the L0 release worker may promote an immutable, gated release."}) as step:
                step.status = "ready" if ready else "blocked"
        verified = not failures and scope_acceptance.get("passed") and evals_passed
        result = {"adapter": adapter_key, "job_id": job_id, "ran": len(plan), "statuses": statuses,
                  "fixed_scope": fixed_scope, "discover": bool(discover),
                  "failures": failures, "inventory_audit_id": after.get("audit_id"),
                  "downstream": "held", "acceptance": "candidate_verified" if verified else "awaiting_verification",
                  "evals_passed": evals_passed, "publication": "ready_for_l0" if ready else "blocked", "error": None}
        workflow.update_job(engine, job_id, "candidate_verified" if verified else "blocked", result)
        if failures:
            raise AdapterRunIncomplete(f"{job_id}: {len(failures)} unresolved source tasks; inspect workflow evidence")
        return result
    except Exception as exc:
        if not isinstance(exc, AdapterRunIncomplete):
            workflow.update_job(engine, job_id, "failed", {"error": str(exc)[:1000], "failures": failures})
        raise
    finally:
        # Candidate evidence is useful even when rights/evals block acceptance.
        # L0 refreshes the reviewer projection; Aurora remains the record.
        if not cycle_context:
            from app.clhear.l1.viewer_snapshot import request_refresh
            request_refresh(engine, reason="adapter_job_finished", job_id=job_id)


def _put_schedule_metric(missed_count: int) -> None:
    """CLHEAR/ScheduleMissedSources: alarmed in CloudWatch when > 0."""
    try:
        import boto3

        boto3.client("cloudwatch", region_name=get_settings().aws_region).put_metric_data(
            Namespace="CLHEAR",
            MetricData=[{"MetricName": "ScheduleMissedSources", "Value": float(missed_count), "Unit": "Count"}],
        )
    except Exception:
        log.exception("could not publish ScheduleMissedSources metric")


def handle_adapter_run(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    payload = envelope.payload or {}
    event_key = delivery_event_key(envelope)
    from app.clhear.l1 import cycles
    cycle_id, child_id = payload.get("cycle_id"), payload.get("child_id")
    if envelope.producer == "eventbridge":
        cycle_id, child_id = cycles.scheduled_child(engine, envelope)
        return {"cycle_id": cycle_id, "child_id": child_id, "status": "scheduled_receipt_recorded",
                "scheduler_event_id": envelope.event_id, "scheduled_for": envelope.ts,
                "acceptance": "pending_cycle_execution"}
    try:
        context = cycles.child_context(engine, cycle_id, child_id, envelope) if cycle_id else None
    except cycles.CycleRevisionChanged:
        return {"cycle_id": cycle_id, "status": "failed", "reason": "worker_revision_changed_requires_new_cycle",
                "acceptance": "not_accepted", "downstream": "held"}
    source_keys = payload.get("source_keys")
    if source_keys is not None and not (isinstance(source_keys, list) and source_keys
                                        and all(isinstance(k, str) and k for k in source_keys)):
        raise ValueError("AdapterRunRequested.source_keys must be a non-empty list of source keys")
    try:
        result = run_adapter_fleet(
            engine, payload.get("adapter", envelope.subject_ref), gateway,
            force_nightly=bool(payload.get("force_nightly") or payload.get("force")),
            nightly_only=bool(payload.get("nightly_only")),
            job_id=payload.get("job_id") or None, event_key=event_key,
            trigger="schedule" if context and context["origin"] == "scheduled" else "manual",
            cycle_context=context, source_keys=source_keys,
            discover=None if payload.get("discover") is None else bool(payload.get("discover")),
        )
    except Exception as exc:
        if context:
            cycles.unhandled_child_error(engine, context, envelope, exc)
        raise
    return result


def handle_l1_cycle_requested(engine, gateway, envelope):
    from app.clhear.l1 import cycles
    return cycles.start(engine, envelope)


def handle_l1_cycle_advance(engine, gateway, envelope):
    from app.clhear.l1 import cycles
    return cycles.advance(engine, envelope.payload["cycle_id"])


def _cycle_store():
    from app.clhear.l1 import pipeline
    settings = get_settings()
    return (pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
            if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" else pipeline.LocalStore(settings.clhear_artifacts_dir))


def handle_l1_cycle_discovery(engine, gateway, envelope):
    from app.clhear.l1 import cycles, inventory, workflow
    cycle_id = envelope.payload["cycle_id"]
    state = cycles.cycle_summary(engine, cycle_id)["cycles"][0]
    if not cycles.verify_runtime(engine, cycle_id):
        return {"cycle_id": cycle_id, "status": "failed", "reason": "worker_revision_changed_requires_new_cycle"}
    if state["status"] != "discovering":
        return {"cycle_id": cycle_id, "status": state["status"]}
    # Same logical discovery job across page batches; a denied/failed page is
    # retried by a new cycle, not repeatedly by every continuation command.
    job_id = workflow.job_id_for(cycle_id, "cycle.discovery")
    workflow.ensure_job(engine, job_id, "cycle.discovery", state["origin"], cycle_id)
    workflow.update_job(engine, job_id, "running")
    try:
        with workflow.bind_execution(engine, job_id), workflow.stage("discovery", {"cycle_id": cycle_id}) as step:
            audit = inventory.run_inventory_audit(engine, _cycle_store(), job_id=job_id, scope=state["scope"], discover=True,
                                                  discovery_cycle_date=cycles.discovery_date(state))
            step.details.update(audit_id=audit["audit_id"], inventory_hash=audit["inventory_hash"])
        result = cycles.discovered(engine, cycle_id, audit)
        workflow.update_job(engine, job_id, "running" if result["status"] == "discovering" else "completed_for_review", result)
        return result
    except Exception as exc:
        workflow.update_job(engine, job_id, "failed", {"error_type": type(exc).__name__})
        raise


def handle_l1_cycle_evaluation(engine, gateway, envelope):
    from app.clhear.l1 import cycles, inventory, workflow
    from app.clhear.platform import evals
    cycle_id = envelope.payload["cycle_id"]
    state = cycles.cycle_summary(engine, cycle_id)["cycles"][0]
    if not cycles.verify_runtime(engine, cycle_id):
        return {"cycle_id": cycle_id, "status": "failed", "reason": "worker_revision_changed_requires_new_cycle"}
    if state["status"] in cycles.TERMINAL_CYCLE:
        return state["result"]
    job_id = workflow.job_id_for(delivery_event_key(envelope), "cycle.evaluation")
    workflow.ensure_job(engine, job_id, "cycle.evaluation", state["origin"], delivery_event_key(envelope))
    workflow.update_job(engine, job_id, "running")
    try:
        with workflow.bind_execution(engine, job_id):
            with workflow.stage("database_reconciliation", {"cycle_id": cycle_id}) as step:
                audit = inventory.run_inventory_audit(engine, _cycle_store(), job_id=job_id, scope=state["scope"], discover=False)
                step.details.update(audit_id=audit["audit_id"], inventory_hash=audit["inventory_hash"])
            english = _prepare_cycle_english(engine, gateway, cycle_id, audit)
            with workflow.stage("readback_evals", {"cycle_id": cycle_id}) as step:
                suites = {name: evals.run_suite(engine, name, release=cycle_id,
                          source_key=cycle_id if name == "l1_schedule_kept" and state["origin"] == "scheduled" else None) for name in
                          ("l1_completeness", "l1_inventory_acceptance", "l1_boundary_f1", "l1_schedule_kept")}
                source_evals = {s["source_key"]: evals.run_source_evals(engine, s["source_key"], release=cycle_id) for s in audit["sources"]}
                acceptance = inventory.acceptance_status(engine, scope=state["scope"])
                acceptance_audit_current = acceptance.get("audit_id") == audit["audit_id"]
                frozen_hash = state["manifest"].get("inventory_hash")
                manifest_current = frozen_hash is None or frozen_hash == audit["inventory_hash"]
                required = ["l1_completeness", "l1_inventory_acceptance", "l1_boundary_f1"]
                if state["origin"] == "scheduled":
                    required.append("l1_schedule_kept")
                identity_recorded = bool(state["code_revision"] and state["worker_image_digest"])
                output_bindings = cycles.output_bindings(engine, cycle_id, audit)
                passed = identity_recorded and output_bindings["passed"] and manifest_current and acceptance_audit_current and acceptance["passed"] and all(suites[name]["passed"] for name in required)
                step.status = "completed" if passed else "blocked"
                step.details.update(suites=suites, acceptance_passed=passed, manifest_current=manifest_current,
                                    acceptance_audit_current=acceptance_audit_current)
            result = {"audit_id": audit["audit_id"], "inventory_hash": audit["inventory_hash"],
                      "verified": audit["verified"], "unresolved": audit["unresolved"],
                      "known_expected": audit["known_expected"], "acceptance_passed": passed,
                      "manifest_current": manifest_current, "identity_recorded": identity_recorded, "evals": suites, "source_evals": source_evals,
                      "acceptance_audit_current": acceptance_audit_current,
                      "output_bindings": output_bindings,
                      "english": english,
                      "acceptance_reasons": acceptance["reasons"],
                      "nightly_schedule_validation": "observed_delivery" if state["origin"] == "scheduled" else "pending"}
        workflow.update_job(engine, job_id, "candidate_verified" if passed else "completed_for_review", result)
        return cycles.finish_cycle(engine, cycle_id, result)
    except Exception as exc:
        workflow.update_job(engine, job_id, "failed", {"error_type": type(exc).__name__})
        raise


def _prepare_cycle_english(engine, gateway, cycle_id, audit):
    """L1 tasks follow original readback and precede the corpus acceptance gate."""
    from app.clhear.l1 import translation, workflow
    job_id = workflow.job_id_for(cycle_id, "cycle.english")
    workflow.ensure_job(engine, job_id, "english_view", "cycle", cycle_id)
    workflow.update_job(engine, job_id, "running")
    for source in audit["sources"]:
        version_id = source.get("source_version_id")
        if version_id is None:
            continue
        task_id = workflow.ensure_task(engine, job_id, source["source_key"], "l1.english_view")
        token = workflow.claim_task(engine, task_id)
        if token is None:
            continue
        try:
            with workflow.bind_execution(engine, job_id, task_id, token), workflow.heartbeat(
                    lambda: workflow.heartbeat_task(engine, task_id, token)):
                with workflow.stage("english_view", {"cycle_id": cycle_id, "source_version_id": version_id}) as step:
                    result = translation.build_english_view(engine, gateway, version_id, job_id=job_id)
                    step.status = "completed" if result.get("english_ready") else "blocked"
                    step.details.update(result)
            workflow.finish_task(engine, task_id, token,
                                 status="completed" if result.get("english_ready") else "blocked", summary=result)
        except Exception as error:
            workflow.finish_task(engine, task_id, token, status="failed", error=error)
            raise
    result = translation.english_acceptance(engine, [s["source_version_id"] for s in audit["sources"] if s.get("source_version_id") is not None])
    result["job_id"] = job_id
    workflow.update_job(engine, job_id, "completed" if result["passed"] else "completed_for_review", result)
    return result


SNAPSHOT_LOCAL = "/tmp/clhear.db"


def handle_publish_release(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Prepare or strictly verify and promote a private L1 release through L0."""
    from app.clhear.releases import publish_release, promote_release

    payload = envelope.payload or {}
    action = payload.get("action", "prepare")
    if action == "prepare":
        return publish_release(engine, release_id=payload.get("release_id"), sbom_path=payload.get("sbom_path"))
    if action == "promote":
        return promote_release(engine, payload.get("release_id", ""))
    raise ValueError("Unknown release action; expected prepare or promote")


def handle_l1_inventory_audit(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Audit-only execution uses the same authoritative engine and L1 store."""
    from app.clhear.l1 import inventory, pipeline, workflow

    settings = get_settings()
    scope = (envelope.payload or {}).get("scope", "registered")
    store = (pipeline.S3Store(settings.clhear_datalake_bucket, settings.aws_region)
             if os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3" or settings.clhear_snapshot_s3_uri
             else pipeline.LocalStore(settings.clhear_artifacts_dir))
    event_key = delivery_event_key(envelope)
    job_id = workflow.job_id_for(event_key, f"inventory.{scope}")
    workflow.ensure_job(engine, job_id, f"inventory.{scope}", "manual", event_key)
    workflow.update_job(engine, job_id, "running")
    try:
        with workflow.bind_execution(engine, job_id), workflow.stage("database_reconciliation", {"scope": scope}) as step:
            result = inventory.run_inventory_audit(engine, store, job_id=job_id, scope=scope,
                                                   discover=bool((envelope.payload or {}).get("discover", False)))
            step.details.update(audit_id=result["audit_id"], inventory_hash=result["inventory_hash"],
                                verified=result["verified"], unresolved=result["unresolved"])
            if result["status"] != "verified":
                step.status = "blocked"
        workflow.update_job(engine, job_id, "verified" if result["status"] == "verified" else "blocked", result)
        return result
    except Exception as exc:
        workflow.update_job(engine, job_id, "failed", {"error_type": type(exc).__name__})
        raise
    finally:
        from app.clhear.l1.viewer_snapshot import request_refresh
        request_refresh(engine, reason="inventory_audit_finished", job_id=job_id)


def handle_l1_evidence_review(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Record explicit reviewer evidence from the trusted L0 command channel.

    This is an operator's reviewed decision, never an inferred licence grant.
    Artifact and scope reviews do not grant source-operation permissions.
    """
    from app.clhear.l1 import inventory, permissions

    payload = dict(envelope.payload or {})
    kind = payload.pop("review_kind", None)
    from app.clhear.l1.viewer_snapshot import request_refresh
    # The review and its refresh request commit together. A revoked grant must
    # never become durable without the outbox work that updates the viewer.
    with engine.begin() as conn:
        if kind == "permissions":
            record = permissions.record_permission(conn, **payload)
        elif kind == "artifact":
            record = inventory.record_artifact_review(conn, **payload)
        elif kind == "scope":
            record = inventory.record_scope_review(conn, **payload)
        elif kind == "language":
            from app.clhear.l1.translation import record_language_binding
            record = record_language_binding(conn, **payload)
            record = {key: value.isoformat() if isinstance(value, (datetime, date)) else value for key, value in record.items()}
        else:
            raise ValueError("review_kind must be permissions, artifact, scope or language")
        request_refresh(conn, reason="source_evidence_updated")
    return {"review_kind": kind, "record": record, "requires_new_audit": True}


def _record_import_language(engine, entry, summary, *, store=None):
    """Bind reviewed publisher metadata only to the exact imported bytes."""
    from app.clhear.l1.translation import record_language_binding
    evidence = (entry or {}).get("language_evidence") or {}
    manifest = summary.get("artifact_manifest", [])
    hashes = {item.get("sha256") for item in manifest}
    if store is not None and len(manifest) == 1:
        import hashlib
        from app.clhear.l1 import publishers
        from app.clhear.l1.publisher_catalogs import language_metadata
        item = manifest[0]
        if item.get("key") and item.get("content_type") in {"text/html", "application/xhtml+xml"}:
            body = store.get(item["key"])
            # Artifact metadata alone cannot certify bytes changed in storage.
            if not body or hashlib.sha256(body).hexdigest() != item.get("sha256"):
                return
            record = entry or {"key": summary.get("source", "")}
            for publisher_id in publishers.publisher_ids(record):
                observed = language_metadata(body, publisher_id=publisher_id, document_key=record.get("key", ""),
                                             url=record.get("canonical_url", ""))
                if observed:
                    evidence = observed
                    break
    if (evidence.get("method") not in {"publisher_contract", "publisher_metadata"}
            or evidence.get("authority") != "authoritative"
            or not evidence.get("artifact_sha256") or evidence["artifact_sha256"] not in hashes):
        return
    with engine.begin() as conn:
        record_language_binding(conn, source_version_id=summary["source_version_id"],
            language=evidence["language"], document_key=evidence["document_key"], authority=evidence["authority"],
            evidence_ref=evidence["evidence_ref"], approved_by="l1.publisher_contract", approved=True)


def handle_l1_translation(engine, gateway, envelope):
    from app.clhear.l1 import translation, workflow
    version_id = envelope.payload.get("source_version_id")
    if type(version_id) is not int or version_id <= 0:
        raise ValueError("Translation requires an exact source_version_id")
    job_id = workflow.job_id_for(delivery_event_key(envelope), "english_view")
    workflow.ensure_job(engine, job_id, "english_view", "manual", delivery_event_key(envelope))
    with workflow.bind_execution(engine, job_id), workflow.stage("english_view", {"source_version_id": version_id}) as step:
        result = translation.build_english_view(engine, gateway, version_id, job_id=job_id)
        step.status = "completed" if result.get("english_ready") else "blocked"
        step.details.update(result)
    workflow.update_job(engine, job_id, "completed" if result.get("english_ready") else "completed_for_review", result)
    from app.clhear.l1.viewer_snapshot import request_refresh
    request_refresh(engine, reason="english_view_finished", job_id=job_id)
    return {**result, "job_id": job_id, "accepted_release": False}


def handle_viewer_snapshot(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """L0 projects allowlisted candidate evidence into the private web viewer."""
    from app.clhear.l1.viewer_snapshot import configured_uri, publish_viewer_snapshot
    uri = configured_uri()
    if not uri:
        raise ValueError("CLHEAR_VIEWER_SNAPSHOT_S3_URI must identify the private reviewer object")
    return publish_viewer_snapshot(engine, uri, get_settings().aws_region,
                                   job_id=(envelope.payload or {}).get("job_id"), requested_at=envelope.ts)


def handle_community_write(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """Apply a community op from the read-only web app (single-writer rule)."""
    from app.clhear import community_writes

    return community_writes.apply_op(engine, envelope.payload)


def handle_l1_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.2: an L1 clause change -> L2 change inference for that source."""
    from app.clhear.l2.change import on_l1_changed
    from app.clhear.platform.router import Router, is_router

    llm = gateway
    if gateway is not None and not is_router(gateway):
        llm = Router(engine, providers={getattr(gateway._provider, "name", "fake"): gateway._provider})
    return on_l1_changed(engine, envelope.payload or {}, llm)


def handle_l2_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.3 / §4.4 / §4.5: an L2 obligation change -> L3 requires / characteristics
    propagation, L4 applicability-edge re-stamping and L5 junction re-derivation
    (I1: derive downward)."""
    from app.clhear.l3.decompose import on_l2_changed
    from app.clhear.l4.predicates import on_l2_changed as l4_on_l2_changed
    from app.clhear.l5.map import on_l2_changed as l5_on_l2_changed

    out = on_l2_changed(engine, envelope.payload or {})
    out["l4"] = l4_on_l2_changed(engine, envelope.payload or {})
    out["l5"] = l5_on_l2_changed(engine, envelope.payload or {})
    out["l6"] = l6_on_changed(engine, envelope.payload or {}, layer="L2")
    return out


def handle_l4_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.4 / §4.5 / §4.6: an ontology change re-judges every stored profile
    (never deletes), re-derives the L5 implies edges (products may have come or
    gone) and recomposes the stored blueprints."""
    from app.clhear.l4.validate import revalidate_profiles
    from app.clhear.l5.map import on_l4_changed

    out = revalidate_profiles(engine)
    out["l5"] = on_l4_changed(engine, envelope.payload or {})
    out["l6"] = l6_on_changed(engine, envelope.payload or {}, layer="L4")
    return out


def handle_l5_changed(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §4.6: a junction change recomposes the stored blueprints (diff engine)."""
    return {"l6": l6_on_changed(engine, envelope.payload or {}, layer="L5")}


def handle_graph_rebuild(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 I7: rebuild the query graph and the vector index from the record
    (scheduled nightly; also on demand). Idempotent by construction."""
    from app.clhear.platform import embeddings, graph

    payload = envelope.payload or {}
    out = {"graph": graph.rebuild(engine, release=payload.get("release", ""), trigger="event")}
    if payload.get("index", True):
        out["index"] = embeddings.rebuild_index(engine, release=payload.get("release", ""), trigger="event",
                                                force=bool(payload.get("force")))
    return out


def handle_dr_drill(engine: Engine, gateway: Gateway, envelope: Envelope) -> dict:
    """HLD v2 §7.1 (item 17): nightly restore drill — backup the record, restore it
    into the scratch target, verify record + graph + datalake replica, log RPO/RTO."""
    from app.clhear.platform import dr

    payload = envelope.payload or {}
    return dr.run(engine, release=payload.get("release", ""), scratch_url=payload.get("scratch_url"),
                  neo4j_database=payload.get("neo4j_database"), trigger="event",
                  skip_datalake=bool(payload.get("skip_datalake", False)))


def l6_on_changed(engine: Engine, payload: dict, *, layer: str) -> dict:
    from app.clhear.l6.diff import on_lower_layer_changed

    return on_lower_layer_changed(engine, payload, layer=layer)


HANDLERS = {
    "DummyChanged": handle_dummy_changed,
    "AdapterRunRequested": handle_adapter_run,
    "L1CycleRequested": handle_l1_cycle_requested,
    "L1CycleAdvanceRequested": handle_l1_cycle_advance,
    "L1CycleDiscoveryRequested": handle_l1_cycle_discovery,
    "L1CycleEvaluationRequested": handle_l1_cycle_evaluation,
    "PublishReleaseRequested": handle_publish_release,
    "L1InventoryAuditRequested": handle_l1_inventory_audit,
    "L1EvidenceReviewRecorded": handle_l1_evidence_review,
    "L1TranslationRequested": handle_l1_translation,
    "ViewerSnapshotRequested": handle_viewer_snapshot,
    "GraphRebuildRequested": handle_graph_rebuild,
    "DrDrillRequested": handle_dr_drill,
    "QueueRecoveryRequested": lambda engine, gateway, envelope: __import__(
        "app.clhear.platform.queue_recovery", fromlist=["handle_queue_recovery"]).handle_queue_recovery(engine, gateway, envelope),
    "CommunityWrite": handle_community_write,
    "clhear.l1.changed": handle_l1_changed,
    "clhear.l2.changed": handle_l2_changed,
    "clhear.l4.changed": handle_l4_changed,
    "clhear.l5.changed": handle_l5_changed,
    # Later layers: add kinds here. handle_envelope already ignores unknown kinds.
}

class L1AcceptanceHold(RuntimeError):
    """Retryable event held until an operator accepts the L1 scope."""


class MalformedDelivery(ValueError):
    """The body is not a resolvable envelope, or a scheduled occurrence has no
    identity; raised before any handler runs so the message can be quarantined."""


class WrongFleet(RuntimeError):
    """An event must not be acknowledged by a fleet that does not own it."""


def delivery_event_key(envelope):
    if envelope.producer == "eventbridge":
        # Old static EventBridge inputs lack an occurrence identity. Never
        # synthesize today's date: delayed retries would become a new job.
        if not envelope.ts:
            raise ValueError("Scheduled event is missing occurrence timestamp; deploy the EventBridge input transformer")
        return f"{envelope.event_id}:{envelope.ts}"
    return envelope.event_id


class UnknownKind(WrongFleet):
    """No fleet owns this kind: it is not in the routing table."""


class AuditOnlyKind(WrongFleet):
    """An audit record reached a queue; nothing consumes it."""


def _owned_handler(kind, fleet):
    from app.clhear.platform import routing
    category, owner = routing.classify(kind)
    if category == "unknown":
        raise UnknownKind(f"No route or handler for {kind}; quarantine with evidence")
    if category == "audit":
        raise AuditOnlyKind(f"{kind} is an audit record; no fleet consumes it")
    if fleet == "all":  # Explicit local/test worker compatibility.
        handler = HANDLERS.get(kind)
        if handler is None:
            raise WrongFleet(f"No implemented handler for {kind}")
        return handler
    if kind == "clhear.l2.changed":
        if fleet == "l3":
            from app.clhear.l3.decompose import on_l2_changed
        elif fleet == "l4":
            from app.clhear.l4.predicates import on_l2_changed
        elif fleet == "l5":
            from app.clhear.l5.map import on_l2_changed
        else:
            raise WrongFleet(f"{fleet} has no implemented consumer for {kind}")
        return lambda engine, gateway, envelope: on_l2_changed(engine, envelope.payload or {})
    if fleet not in routing.consumers_for(kind):
        raise WrongFleet(f"{fleet} does not own {kind}; retain for correct routing or handler implementation")
    if kind in {"clhear.l4.changed", "clhear.l5.changed"}:
        return lambda engine, gateway, env: {"l6": l6_on_changed(engine, env.payload or {}, layer=env.layer.upper())}
    return HANDLERS[kind]


def handle_envelope(engine: Engine, gateway: Gateway, body: str) -> dict | None:
    from app.clhear.l1 import cycles, workflow
    from app.clhear.platform import routing
    try:
        envelope = l0_events.resolve_envelope(engine, body)
    except ValueError as exc:  # includes pydantic ValidationError
        raise MalformedDelivery(str(exc)) from exc
    if get_settings().clhear_l1_only and envelope.kind in routing.DOWNSTREAM_HELD_KINDS:
        raise L1AcceptanceHold("L1 acceptance hold: retain this event for replay after verification")
    fleet = os.environ.get("CLHEAR_FLEET", "all").lower()
    handler = _owned_handler(envelope.kind, fleet)
    try:
        event_key = delivery_event_key(envelope)
    except ValueError as exc:
        raise MalformedDelivery(str(exc)) from exc
    consumer = f"fleet.{fleet}:{envelope.kind}"
    # Completed deliveries can arrive after a cycle has advanced to another
    # phase. Return before the phase guard; the claim below remains the atomic
    # idempotency check if a concurrent delivery finishes after this read.
    with engine.connect() as conn:
        completed = conn.execute(sa.select(workflow.deliveries.c.status).where(
            workflow.deliveries.c.consumer == consumer,
            workflow.deliveries.c.event_key == event_key)).scalar_one_or_none()
    if completed == "completed":
        return None
    with ExitStack() as guard:
        try:
            guard.enter_context(cycles.operation_guard(engine, envelope))
        except cycles.CycleRevisionChanged:
            # ACK late delivery only after L0 has committed terminal cycle
            # evidence. A revision mismatch alone is not retirement.
            cycle_id = envelope.payload.get("cycle_id")
            with engine.connect() as conn:
                terminal = conn.execute(sa.select(cycles.cycles.c.status).where(
                    cycles.cycles.c.cycle_id == cycle_id)).scalar_one_or_none()
            if terminal not in cycles.TERMINAL_CYCLE:
                raise
            def handler(engine, gateway, envelope):
                return {"cycle_id": cycle_id, "status": "terminal_delivery_ignored",
                        "cycle_status": terminal, "accepted_release": False, "source_work_performed": False}
        # Waiting for the cycle fence is not an execution attempt. Hold the
        # fence through the handler and its durable delivery/result records.
        return _execute_delivery(engine, gateway, envelope, handler, consumer, event_key)


def _execute_delivery(engine, gateway, envelope, handler, consumer, event_key):
    from app.clhear.l1 import workflow
    token = workflow.claim_delivery(engine, consumer, event_key)
    if token is None:
        return None
    try:
        with workflow.heartbeat(lambda: workflow.heartbeat_delivery(engine, consumer, event_key, token)):
            started = time.monotonic()
            outputs = handler(engine, gateway, envelope)
            outputs["duration_ms"] = int((time.monotonic() - started) * 1000)
        # Only successfully completed handlers receive a durable handled marker.
        workflow.finish_delivery(engine, consumer, event_key, token)
        with engine.begin() as conn:
            conn.execute(runs.insert().values(fleet="worker", trigger=envelope.kind,
                inputs={"event_id": envelope.event_id, "event_key": event_key, "consumer": consumer,
                        "subject_ref": envelope.subject_ref}, outputs=outputs, duration_ms=outputs.get("duration_ms")))
        return outputs
    except Exception as exc:
        if envelope.kind in {"L1CycleDiscoveryRequested", "L1CycleEvaluationRequested"}:
            from app.clhear.l1 import cycles
            with engine.connect() as conn:
                attempt = conn.execute(sa.select(workflow.deliveries.c.attempt).where(
                    workflow.deliveries.c.consumer == consumer, workflow.deliveries.c.event_key == event_key)).scalar_one()
            cycles.failed_phase(engine, envelope.payload["cycle_id"], envelope.kind, exc, attempt)
        try:
            workflow.finish_delivery(engine, consumer, event_key, token, error=exc)
        except workflow.LeaseLost:
            log.warning("delivery ownership lost: %s", event_key)
        raise


class RoutedOutboxTransport:
    """L0 sends commands to the owning queue and layer events to the event bus."""
    def __init__(self, queue_urls, region):
        import boto3
        from app.clhear.platform.events import EventBridgeTransport
        self.queues = queue_urls
        self.sqs = boto3.client("sqs", region_name=region)
        self.bus = EventBridgeTransport(region, bus_name=os.environ.get("CLHEAR_EVENT_BUS_NAME", "clhear"))

    def send(self, body):
        from app.clhear.platform import routing
        env = l0_events.parse_transport(body)
        category, owner = routing.classify(env.kind)
        if category == "layer_event":
            # EventBridge's HTTP 200 can contain per-entry errors; do not let
            # relay_once mark an event relayed unless the entry was accepted.
            result = self.bus._client.put_events(Entries=[{"EventBusName": self.bus._bus,
                "Source": "clhear", "DetailType": env.kind, "Detail": body}])
            entries = result.get("Entries", [])
            if result.get("FailedEntryCount") or len(entries) != 1 or not entries[0].get("EventId") or entries[0].get("ErrorCode"):
                raise RuntimeError(f"EventBridge did not confirm acceptance of {env.kind}")
            return
        if category != "command":
            # relay_once disposes of audit-only and unknown kinds itself; reaching
            # here means a caller bypassed it, and there is no queue to default to.
            raise WrongFleet(f"{env.kind} is not a routable command ({category}); retain with its evidence")
        if owner not in self.queues:
            raise WrongFleet(f"No configured queue for {owner}; retain outbox row")
        self.sqs.send_message(QueueUrl=self.queues[owner], MessageBody=body)


def deferral_reason(exc: BaseException) -> tuple[str, str]:
    """Map a refusal to its ledger reason; the detail is our own fixed wording."""
    from app.clhear.platform import failures
    text = failures.redact(str(exc))
    if isinstance(exc, L1AcceptanceHold):
        return "downstream_held", text
    if isinstance(exc, AuditOnlyKind):
        return "audit_only", text
    if isinstance(exc, UnknownKind):
        return "unknown_kind", text
    if isinstance(exc, WrongFleet):
        return "wrong_owner", text
    if "occurrence timestamp" in str(exc):
        return "unidentifiable_schedule", text
    if "Referenced outbox event" in str(exc):
        return "unresolvable_reference", text
    return "malformed", text


def defer_message(engine: Engine, *, fleet: str, queue_url: str, message: dict, reason: str, detail: str = "",
                  channel: str = "sqs", status: str | None = None) -> dict:
    """Write one SQS delivery to the deferred ledger and commit before the caller
    deletes it. Redelivery of the same MessageId is a no-op (unique per queue)."""
    from app.clhear.platform import deferred
    attributes = message.get("Attributes") or {}
    metadata = {k: attributes.get(k) for k in ("ApproximateReceiveCount", "SentTimestamp", "ApproximateFirstReceiveTimestamp")
                if attributes.get(k) is not None}
    metadata["md5_of_body"] = message.get("MD5OfBody")
    if status is None:
        status = "quarantined" if reason in {"unknown_kind", "malformed", "unidentifiable_schedule", "unresolvable_reference"} else "deferred"
    with engine.begin() as conn:
        return deferred.record(conn, channel=channel, queue=queue_url.rsplit("/", 1)[-1], message_id=message["MessageId"],
                               fleet=fleet, body=message["Body"], reason=reason, detail=detail,
                               queue_metadata=metadata, status=status)


def _snapshot_pull(uri: str, region: str) -> None:
    import boto3

    bucket, key = uri[len("s3://") :].split("/", 1)
    boto3.client("s3", region_name=region).download_file(bucket, key, SNAPSHOT_LOCAL)
    log.info("snapshot pulled from %s", uri)


def _snapshot_push(uri: str, region: str) -> None:
    import boto3

    bucket, key = uri[len("s3://") :].split("/", 1)
    boto3.client("s3", region_name=region).upload_file(SNAPSHOT_LOCAL, bucket, key)
    log.info("snapshot published to %s", uri)


def recover_queues_once(recovery_id: str, *, max_messages=None, max_seconds=None, queues=None) -> dict:
    """One bounded recovery pass as a durable, idempotent L0 delivery: the same
    ``recovery_id`` never runs twice, so a retried task cannot double-process."""
    import re
    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.events import Envelope
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", recovery_id or ""):
        return {"status": "failed", "error_type": "ValueError", "reason": "invalid recovery id"}
    engine = get_engine()
    run_migrations(engine)
    payload = {k: v for k, v in {"max_messages": max_messages, "max_seconds": max_seconds, "queues": queues or None}.items() if v}
    envelope = Envelope(event_id=f"queue-recovery:{recovery_id}", layer="l0", kind="QueueRecoveryRequested",
                        subject_ref="queues", payload=payload, producer="operator.recovery",
                        ts=datetime.now(timezone.utc).isoformat())
    try:
        report = handle_envelope(engine, None, envelope.model_dump_json())
    except Exception as exc:
        from app.clhear.platform import failures
        return {"status": "failed", "recovery_id": recovery_id, **{k: v for k, v in failures.describe(exc).items()
                                                                  if k in {"error_type", "error_code", "sqlstate"}}}
    if report is None:
        return {"status": "already_recovered", "recovery_id": recovery_id, "reason": "this recovery id completed earlier; use a new id for another pass"}
    return {"status": "recovered", "recovery_id": recovery_id, **report}


def dispatch_once(envelope_file: str) -> dict | None:
    """Manual operational entrypoint: the same envelope handler as SQS."""
    from pathlib import Path
    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.router import Router, build_providers

    engine = get_engine()
    run_migrations(engine)
    settings = get_settings()
    gateway = Router(engine, build_providers(settings))
    return handle_envelope(engine, gateway, Path(envelope_file).read_text())


def main() -> None:
    """Long-running ECS worker: relay outbox + consume SQS.

    Snapshot mode (CLHEAR_SNAPSHOT_S3_URI set): the corpus SQLite is pulled
    from S3 at startup, every scheduled fleet run mutates it, and it is
    published back after each handled batch — the public explorer picks the
    new snapshot up on its next TTL check.
    """
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    fleet = os.environ.get("CLHEAR_FLEET", "all").lower()
    if settings.clhear_l1_only and fleet in {f"l{layer}" for layer in range(2, 9)}:
        # A dispatch-time rejection is too late: ReceiveMessage increments the
        # retry count and can send intentionally held work to the dead-letter queue.
        # Keep the service alive without touching its queue, DB, or providers.
        # The deployment setting is fixed for this process; releasing the hold
        # requires starting the task with CLHEAR_L1_ONLY disabled.
        log.warning("L1 acceptance hold: fleet %s paused before startup and queue polling", fleet)
        while True:
            time.sleep(60)

    import boto3

    from app.clhear import db
    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.events import SqsTransport, relay_once
    from app.clhear.platform.router import Router, build_providers, record_missing_providers

    from app.clhear.platform import errors

    errors.init(component=f"fleet-{os.environ.get('CLHEAR_FLEET', 'L0').lower()}")

    snapshot_uri = settings.clhear_snapshot_s3_uri
    # Aurora remains authoritative even if an old snapshot setting survives.
    if snapshot_uri and not settings.database_url.startswith("sqlite"):
        log.warning("Ignoring snapshot database override for authoritative PostgreSQL")
        snapshot_uri = ""
    if snapshot_uri:
        try:
            _snapshot_pull(snapshot_uri, settings.aws_region)
        except Exception:
            log.exception("no existing snapshot at %s; starting fresh", snapshot_uri)
        os.environ["DATABASE_URL"] = f"sqlite:///{SNAPSHOT_LOCAL}"
        get_settings.cache_clear()
        settings = get_settings()
        db.dispose_engine()

    engine = get_engine()
    run_migrations(engine)

    providers = build_providers(settings)
    if not providers:
        record_missing_providers(engine)
    gateway = Router(engine, providers)
    if fleet == "l0":
        transport = RoutedOutboxTransport(json.loads(os.environ.get("CLHEAR_FLEET_QUEUE_URLS", "{}")), settings.aws_region)
    else:
        transport = SqsTransport(settings.clhear_events_queue_url, settings.aws_region)
    should_relay = fleet in {"l0", "all"}
    sqs = boto3.client("sqs", region_name=settings.aws_region)

    log.info("clhear worker started (providers=%s, snapshot=%s)", ",".join(providers) or "none", snapshot_uri or "off")
    while True:
        try:
            if should_relay:
                from app.clhear.l1 import cycles, progress
                cycles.reconcile(engine)
                relay_once(engine, transport, fleet=fleet)
                try:
                    progress.publish(engine)  # small status record between full snapshots; rate-limited
                except Exception:
                    log.exception("progress record not published")
            resp = sqs.receive_message(
                QueueUrl=settings.clhear_events_queue_url,
                MaxNumberOfMessages=1,
                VisibilityTimeout=180,
                WaitTimeSeconds=10,
                AttributeNames=["ApproximateReceiveCount", "SentTimestamp", "ApproximateFirstReceiveTimestamp"],
            )
            messages = resp.get("Messages", [])
            for message in messages:
                try:
                    from app.clhear.l1.workflow import heartbeat
                    with heartbeat(lambda: sqs.change_message_visibility(
                        QueueUrl=settings.clhear_events_queue_url, ReceiptHandle=message["ReceiptHandle"],
                        VisibilityTimeout=180,
                    )):
                        outputs = handle_envelope(engine, gateway, message["Body"])
                        if snapshot_uri:
                            if should_relay:
                                relay_once(engine, transport)
                            # Persist candidate and delivery marker before ACK. A failed
                            # upload leaves the envelope available for recovery.
                            _snapshot_push(snapshot_uri, settings.aws_region)
                    sqs.delete_message(QueueUrl=settings.clhear_events_queue_url,
                                       ReceiptHandle=message["ReceiptHandle"])
                except (L1AcceptanceHold, WrongFleet, MalformedDelivery) as exc:
                    # Held, misrouted, unknown or malformed: preserve the exact message
                    # with its reason in the deferred ledger, then acknowledge it so it
                    # stops circulating through visibility timeouts into the DLQ.
                    reason, detail = deferral_reason(exc)
                    try:
                        entry = defer_message(engine, fleet=fleet, queue_url=settings.clhear_events_queue_url,
                                              message=message, reason=reason, detail=detail)
                        sqs.delete_message(QueueUrl=settings.clhear_events_queue_url,
                                           ReceiptHandle=message["ReceiptHandle"])
                        log.warning("Message deferred (%s, ledger id %s): %s", reason, entry["id"], detail)
                    except Exception:
                        log.exception("Could not persist the deferred message; retained for redelivery")
                except Exception:
                    log.exception("Message failed; retained for retry/dead-letter policy")
                    if snapshot_uri:
                        try:
                            _snapshot_push(snapshot_uri, settings.aws_region)
                        except Exception:
                            log.exception("Could not persist resumable candidate state")
        except Exception:
            log.exception("worker iteration failed; backing off")
            time.sleep(10)


def cli(argv=None) -> int:
    import argparse
    import sys

    class WorkerArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            # Exit 2 is reserved for a completed review-ready verification.
            # Invalid ECS command arguments must never look like that result.
            self.print_usage(sys.stderr)
            self.exit(1, f"{self.prog}: error: {message}\n")

    parser = WorkerArgumentParser(description="CLHEAR worker: SQS consumer or one durable manual envelope")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--envelope-file")
    parser.add_argument("--verification-id")
    parser.add_argument("--request-l1-cycle", action="store_true")
    parser.add_argument("--unchanged-repeat", action="store_true")
    parser.add_argument("--scope", choices=("all_publishers", "registered"), default="all_publishers")
    parser.add_argument("--recover-queues", action="store_true")
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--max-seconds", type=int, default=None)
    parser.add_argument("--queues", default="")
    args = parser.parse_args(argv)
    exclusive = [bool(args.recover_queues), bool(args.request_l1_cycle), bool(args.once)]
    if sum(exclusive) > 1:
        parser.error("choose one worker action")
    if args.recover_queues:
        if not args.verification_id:
            parser.error("--recover-queues requires --verification-id and cannot be combined with other actions")
        if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
            parser.error("--recover-queues must run on the L0 worker")
        result = recover_queues_once(args.verification_id, max_messages=args.max_messages, max_seconds=args.max_seconds,
                                     queues=[q for q in args.queues.split(",") if q])
        print(json.dumps(result, default=str))
        return 0 if result.get("status") == "recovered" else 1
    if args.request_l1_cycle:
        if not args.verification_id:
            parser.error("--request-l1-cycle requires --verification-id and cannot be combined with other actions")
        if os.environ.get("CLHEAR_FLEET", "").lower() != "l0":
            parser.error("--request-l1-cycle must run on the L0 worker")
        from app.clhear.db import get_engine, run_migrations
        from app.clhear.l1.cycles import request_cycle
        engine = get_engine()
        run_migrations(engine)
        print(json.dumps(request_cycle(engine, args.verification_id, scope=args.scope, unchanged_repeat=args.unchanged_repeat)))
        return 0
    if args.verification_id:
        parser.error("--verification-id requires --request-l1-cycle or --recover-queues")
    if args.once != bool(args.envelope_file):
        parser.error("--once and --envelope-file must be supplied together")
    if args.once:
        print(json.dumps(dispatch_once(args.envelope_file), default=str))
    else:
        main()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
