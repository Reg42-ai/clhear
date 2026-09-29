# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Build a scoped corpus from L1 up, one layer at a time.

    python -m app.clhear.scope_build --scope <name> [--skip-import]
        [--profile profile.json] [--publish-release]

Run with ``CLHEAR_SOURCE_SCOPE`` set. The build reads and writes only that
scope's sources, so the database may also hold the rest of the corpus.
L1 imports each scoped document through the same adapters, verification and
acceptance as every other import. Each later layer runs the production
derivation for that layer — no curated blocks, activities, profiles, concepts,
register snapshot or alias tables — and records a build (``layer_builds``)
naming the input revisions it read. A layer whose input changed after that
input's last build does not run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear import layer_builds
from app.clhear.l1 import scopes

log = logging.getLogger("clhear.scope_build")


def import_sources(engine: Engine, llm, scope: dict) -> dict:
    """Import every source the scope names; report each source's outcome.

    Returns ``{"lanes": {adapter: result}, "sources": {key: status},
    "failed_sources": [{"source_key", "adapter", "status", "error"}]}``.
    """
    import uuid

    from app.clhear import notify
    from app.clhear.hoststore import registry_entries
    from app.clhear.l1 import source_registry
    from app.clhear.workers import AdapterRunIncomplete, run_adapter_fleet

    keys = list(scope.get("sources") or [])
    entries = registry_entries(engine, keys)
    missing = sorted(set(keys) - {entry["key"] for entry in entries})
    if missing:
        raise RuntimeError("The scope names sources that are not registered: " + ", ".join(missing))
    source_registry.install(entries)
    source_registry.seed(engine)
    groups: dict[str, list[str]] = {}
    declared = scope.get("imports") or {}
    if declared:
        groups = {adapter: list(adapter_keys) for adapter, adapter_keys in declared.items()}
    else:
        for entry in source_registry.S:
            if entry.get("enabled", True):
                groups.setdefault(entry["adapter"], []).append(entry["key"])
    run_id = uuid.uuid4().hex[:12]
    lanes, statuses, failed = {}, {}, []
    for adapter, adapter_keys in groups.items():
        try:
            result = run_adapter_fleet(engine, adapter, source_keys=list(adapter_keys), discover=False, host=True,
                                       trigger="scope_build", event_key=f"scope-build:{scopes.active_name()}:{adapter}:{run_id}")
        except AdapterRunIncomplete as exc:
            result = getattr(exc, "result", None) or {"sources": {key: "failed" for key in adapter_keys}, "failures": [str(exc)[:300]]}
        lanes[adapter] = {"statuses": result.get("statuses"), "job_id": result.get("job_id")}
        for key in adapter_keys:
            status = (result.get("sources") or {}).get(key, "failed")
            statuses[key] = status
            if status not in {"added", "amended", "unchanged", "up-to-date"}:
                error = _task_error(engine, result.get("job_id"), key)
                failed.append({"source_key": key, "adapter": adapter, "status": status, "error": error})
                notify.emit(engine, "source.failed", {"source_key": key, "adapter": adapter, "status": status,
                                                      "error": error})
    return {"lanes": lanes, "sources": statuses, "failed_sources": failed}


def _task_error(engine: Engine, job_id: str | None, source_key: str) -> str:
    """The recorded reason a source task did not import, without source text."""
    if not job_id:
        return ""
    import sqlalchemy as sa

    from app.clhear.l1 import workflow

    with engine.connect() as conn:
        row = conn.execute(sa.select(workflow.tasks.c.summary, workflow.tasks.c.error)
                           .where(workflow.tasks.c.job_id == job_id, workflow.tasks.c.source_key == source_key)).first()
    if row is None:
        return ""
    summary = row.summary or {}
    failure = summary.get("failure") or {}
    detail = (summary.get("error") or (failure.get("message") if isinstance(failure, dict) else "")
              or summary.get("note") or (row.error if isinstance(row.error, str) else ""))
    findings = (summary.get("original_verification") or {}).get("findings") or []
    if not detail and findings:
        detail = "; ".join(f["code"] for f in findings)
    return str(detail or summary.get("status") or "")[:300]


def _stored_clause_count(engine: Engine, keys: list[str]) -> int:
    import sqlalchemy as sa

    from app.clhear.l1.models import clauses, source_versions, sources

    if not keys:
        return 0
    with engine.connect() as conn:
        return conn.execute(sa.select(sa.func.count()).select_from(
            clauses.join(source_versions, clauses.c.source_version_id == source_versions.c.id)
            .join(sources, source_versions.c.source_id == sources.c.id)).where(sources.c.key.in_(keys))).scalar_one()


def _clear_gaps(engine: Engine, layer: str) -> None:
    from app.clhear import evidence

    with engine.begin() as conn:
        evidence.clear_gaps(conn, scope=scopes.active_name() or "", layer=layer)


def derive_l2(engine: Engine, llm) -> dict:
    from app.clhear.l2.consolidate import draft_and_propose
    from app.clhear.l2.dedupe import consolidate
    from app.clhear.l2.extract import run_extraction
    from app.clhear.l2.review import review_obligations
    from app.clhear.l2.structured import refine_structured
    from app.clhear.l2.triage import triage_duties

    # One source at a time: an unscoped extraction stales every obligation it
    # did not just derive, including rows that belong to other sources.
    from app.clhear import evidence

    chosen = scopes.source_keys()
    extraction = [run_extraction(engine, source_key=key) for key in chosen] if chosen else run_extraction(engine)
    with engine.begin() as conn:
        # A new L1 version gives unchanged clauses new ids: quotes of the same words follow them.
        reanchored = evidence.reanchor(conn)
    _clear_gaps(engine, "L2")
    _no_duties_gap(engine, chosen)
    return {"extraction": extraction, "reanchored": reanchored, "triage": triage_duties(engine, llm),
            "structured": refine_structured(engine, llm), "consolidation": draft_and_propose(engine, llm),
            "dedupe": consolidate(engine), "review": review_obligations(engine, llm)}


def _source_gaps(engine: Engine, failed: list[dict]) -> None:
    """A source that could not be read: say what to register instead."""
    from app.clhear import evidence

    _clear_gaps(engine, "L1")
    with engine.begin() as conn:
        for item in failed:
            evidence.record_gap(conn, scope=scopes.active_name() or "", layer="L1", kind="no_text",
                                subject=item["source_key"], source_key=item["source_key"],
                                missing=f"readable text ({item.get('error') or item.get('status')})")


def _no_duties_gap(engine: Engine, keys) -> None:
    """Texts were read but no clause states a duty: say which text would."""
    import sqlalchemy as sa

    from app.clhear import evidence
    from app.clhear.derived_models import obligations

    with engine.begin() as conn:
        live = conn.execute(sa.select(sa.func.count()).select_from(obligations).where(
            obligations.c.source_key.in_(list(keys or [])), obligations.c.status.in_(("derived", "validated")))).scalar()
        if keys and not live:
            evidence.record_gap(conn, scope=scopes.active_name() or "", layer="L2", kind="no_duties", subject="L2",
                                missing="a clause that states a duty", detail={"sources": list(keys)})


def derive_l3(engine: Engine, llm) -> dict:
    from app.clhear.curated import seed_data_model
    from app.clhear.l3.characterize import characterize
    from app.clhear.l3.decompose import decompose
    from app.clhear.l3.generate import generate_blocks
    from app.clhear.l3.harmonize import harmonize

    _clear_gaps(engine, "L3")
    return {"data_model": seed_data_model(engine), "generated": generate_blocks(engine, llm),
            "decomposition": decompose(engine), "harmonisation": harmonize(engine),
            "characterisation": characterize(engine, llm)}


def _defines(texts: list[str], label: str) -> bool:
    """True when a clause in scope defines ``label`` ("'controller' means ...")."""
    import re

    words = r"\W+".join(re.escape(w) for w in label.split())
    pattern = re.compile(rf"\b{words}s?\b[\"'”’)]*\s*(?:,[^.;]{{0,60}},\s*)?(?:means|includes|shall mean|is defined|refers to)\b"
                         rf"|\b(?:term|expression)\s+[\"'“‘]?{words}", re.I)
    return any(pattern.search(t) for t in texts)


def _role_gaps(engine: Engine, asked: dict, keys: list[str]) -> int:
    """An evidence gap per role the duties use but no text in scope defines."""
    import sqlalchemy as sa

    from app.clhear import evidence
    from app.clhear.l1.models import clauses, source_versions, sources

    with engine.begin() as conn:
        texts = [r[0] or "" for r in conn.execute(
            sa.select(clauses.c.text).join(source_versions, clauses.c.source_version_id == source_versions.c.id)
            .join(sources, source_versions.c.source_id == sources.c.id)
            .where(sources.c.key.in_(keys), source_versions.c.status == "in_force"))]
        found = 0
        for role in asked["roles"]:
            if _defines(texts, role["label"]):
                continue
            first = (role["quotes"] or [{}])[0]
            evidence.record_gap(conn, scope=scopes.active_name() or "", layer="L4", kind="role_undefined",
                                subject=f"role:{role['role']}", source_key=first.get("source_key", ""),
                                clause_ref=first.get("clause_ref", ""), missing=f"a definition of '{role['label']}'",
                                role=role["label"], detail={"duties": role["duties"]})
            found += 1
    return found


def derive_l4(engine: Engine, llm, profiles: list[dict]) -> dict:
    """Applicability read from the texts, the questions it raises, and each
    profile's answers checked against them."""
    from app.clhear.l4.licenses import extract_licenses
    from app.clhear.l4.ontology import build_ontology
    from app.clhear.l4.predicates import extract_predicates, questions
    from app.clhear.l4.validate import check_answers, create_profile, licence_questions

    _clear_gaps(engine, "L4")
    licenses = extract_licenses(engine, llm)
    ontology = build_ontology(engine, check_registers=False)
    predicates = extract_predicates(engine)
    keys = sorted(scopes.source_keys() or [])
    with engine.connect() as conn:
        asked = questions(conn, keys)
        asked["licences_named"] = [lic["name"] for lic in licence_questions(conn, keys)]
    undefined = _role_gaps(engine, asked, keys)
    stored = []
    for profile in profiles:
        row = create_profile(engine, profile["attributes"], name=profile.get("name", ""), source="api", allow_invalid=True)
        validity = row.get("validity") if isinstance(row.get("validity"), dict) else {}
        stored.append({"id": row["id"], "status": row.get("status"), "name": profile.get("name", ""),
                       "errors": validity.get("errors") or [],
                       "warnings": check_answers(asked, profile["attributes"] or {})})
    return {"licenses": licenses, "ontology": {"version": ontology["version"]}, "predicates": predicates,
            "questions": {"roles": len(asked["roles"]), "conditions": len(asked["conditions"]),
                          "licences": len(asked["licences_named"]), "roles_undefined": undefined},
            "profiles": stored}


def derive_l5(engine: Engine, llm) -> dict:
    from app.clhear.l5.check import check_junction
    from app.clhear.l5.map import map_activities

    _clear_gaps(engine, "L5")
    mapping = map_activities(engine, llm)
    junction = check_junction(engine)
    return {"mapping": mapping, "junction": {k: junction[k] for k in ("activities", "edges", "ok")},
            "orphans": len(junction["orphans"]), "dangling": len(junction["dangling"])}


def derive_l6(engine: Engine, llm, profile_ids: list[str] | None = None, withheld=None) -> dict:
    """Compose a blueprint for each profile this build stored, over this scope only.

    Rows the lineage check could not anchor to a clause are ``withheld``.
    Explanations are the deterministic, citation-carrying text of ``l6.explain``.
    """
    from app.clhear.fleets import compose_stored_profiles
    from app.clhear.l6 import composer

    if not scopes.active():
        return {"composed": compose_stored_profiles(engine), "compositions": {}}
    compositions = {}
    for pid in profile_ids or []:
        try:
            compositions[pid] = composer.compose_for_profile(engine, pid, requested_by="l6.compose:scope",
                                                             withheld=withheld)
        except KeyError:
            continue
    return {"compositions": compositions}


def _kinds_in_scope(engine: Engine) -> set[str]:
    import sqlalchemy as sa

    from app.clhear.l1.models import sources

    with engine.connect() as conn:
        return {r[0] for r in conn.execute(sa.select(sources.c.kind).where(sources.c.key.in_(scopes.source_keys())))}


def _not_built(engine: Engine, layer: str, kind: str, missing: str) -> dict:
    """A layer with no source to derive from: no rows, no model call, one gap."""
    from app.clhear import evidence

    _clear_gaps(engine, layer)
    with engine.begin() as conn:
        gid = evidence.record_gap(conn, scope=scopes.active_name() or "", layer=layer, kind=kind, subject=layer,
                                  missing=missing)
    return {"built": False, "reason": evidence.recommendation(kind), "gap": gid}


def derive_l7(engine: Engine, llm) -> dict:
    from app.clhear.l7 import enforcement, score

    if "enforcement" not in _kinds_in_scope(engine):
        return _not_built(engine, "L7", "no_enforcement_sources", "an enforcement source (kind 'enforcement')")
    _clear_gaps(engine, "L7")
    return {"events": enforcement.ingest_events(engine), "links": enforcement.link_events(engine, llm),
            "calibration": {k: v for k, v in score.calibrate(engine).items() if k in ("status", "id", "held_out_year")},
            "obligation_scores": {k: v for k, v in score.score_obligations(engine).items() if k != "bands"}}


def item_priority(engine: Engine) -> dict:
    from app.clhear.l7 import score

    return score.score_items(engine)


def derive_l8(engine: Engine, llm) -> dict:
    from app.clhear.l8.reference import reference_rows

    if "guidance" not in _kinds_in_scope(engine):
        return _not_built(engine, "L8", "no_reference_sources", "a guidance or reference source (kind 'guidance')")
    _clear_gaps(engine, "L8")
    rows = reference_rows(engine)
    return {"reference_rows": len(rows), "mapped_to_blocks": sum(1 for r in rows if r["block_id"])}


def _counts(engine: Engine, layer: str) -> dict:
    with engine.connect() as conn:
        if layer == "L8":
            from app.clhear.l8.reference import derived_reference_rows

            return {"reference_rows": len(derived_reference_rows(conn))}
        return {t.name: len(layer_builds._rows(conn, t)) for t in layer_builds._tables(layer)}


def build(engine: Engine, llm, *, skip_import: bool = False, profiles: list[dict] | None = None,
          layers: tuple[str, ...] = layer_builds.ORDER) -> dict:
    name = scopes.active_name()
    if not name:
        raise RuntimeError(f"Set {scopes.SCOPE_ENV}; a scope build never runs against the full registry")
    try:
        scope = scopes.get(name)
    except KeyError as exc:
        raise RuntimeError(str(exc)) from exc
    held: dict = {"profiles": []}

    def run_l4() -> dict:
        detail = derive_l4(engine, llm, profiles or [])
        held["profiles"] = [p["id"] for p in detail.get("profiles") or []]
        held["profile_checks"] = detail.get("profiles") or []
        return detail

    def run_l6() -> dict:
        from app.clhear import lineage

        held["lineage"] = lineage.verify(engine, scope.get("sources") or [])
        kinds = _kinds_in_scope(engine)
        # L7 and L8 run after the blueprint: record now what they will lack, and clear what they no
        # longer lack, so the blueprint says so.
        if "enforcement" not in kinds:
            _not_built(engine, "L7", "no_enforcement_sources", "an enforcement source (kind 'enforcement')")
        else:
            _clear_gaps(engine, "L7")
        if "guidance" not in kinds:
            _not_built(engine, "L8", "no_reference_sources", "a guidance or reference source (kind 'guidance')")
        else:
            _clear_gaps(engine, "L8")
        detail = derive_l6(engine, llm, held["profiles"], withheld=held["lineage"]["withheld"])
        held["compositions"] = detail.get("compositions") or {}
        return {k: v for k, v in detail.items() if k != "compositions"} | {"blueprints": sorted(
            c.get("blueprint_id") or "" for c in held["compositions"].values())}

    steps = {
        "L1": (lambda: {"skipped": "import"}) if skip_import else (lambda: import_sources(engine, llm, scope)),
        "L2": lambda: derive_l2(engine, llm), "L3": lambda: derive_l3(engine, llm),
        "L4": run_l4, "L5": lambda: derive_l5(engine, llm),
        "L6": run_l6, "L7": lambda: derive_l7(engine, llm),
        "L8": lambda: derive_l8(engine, llm),
    }
    report = {"scope": name, "layers": {}}
    for layer in layer_builds.ORDER:
        if layer not in layers:
            continue
        with engine.connect() as conn:
            inputs = layer_builds.check_inputs(conn, layer, name)
        started = datetime.now(timezone.utc)
        before = dict(_llm_stats(llm))
        detail = steps[layer]()
        after = _llm_stats(llm)
        calls = {"ok": after["ok"] - before["ok"], "failed": after["failed"] - before["failed"]}
        if layer == "L1":
            report["sources"] = detail.get("sources") or {}
            report["failed_sources"] = detail.get("failed_sources") or []
            _source_gaps(engine, report["failed_sources"])
            if not _stored_clause_count(engine, list(scope.get("sources") or [])):
                reasons = "; ".join(f"{f['source_key']}: {f['error'] or f['status']}" for f in report["failed_sources"])
                raise RuntimeError("No text could be read from this scope's sources" + (f" ({reasons})" if reasons else ""))
        built = layer_builds.record(engine, layer, scope=name, inputs=inputs, counts=_counts(engine, layer),
                                    steps=detail, started_at=started)
        report["layers"][layer] = {"revision": built["revision"], "inputs": inputs, "counts": built["counts"],
                                   "model_calls": calls}
        if isinstance(detail, dict) and detail.get("built") is False:
            report["layers"][layer].update(built=False, reason=detail["reason"])
        if calls["failed"] and not calls["ok"]:
            raise RuntimeError(f"Every model call in {layer} failed ({calls['failed']}); last error: {after['last_error']}")
        log.info("built %s %s from %s", layer, built["revision"][:12], {k: v[:12] for k, v in inputs.items()})
    if "L7" in report["layers"] and report["layers"]["L7"].get("built", True):
        view = "L7 item priority"
        with engine.connect() as conn:
            inputs = layer_builds.check_inputs(conn, view, name)
        report["views"] = {view: {"inputs": inputs, "item_scores": item_priority(engine)}}
    report["profiles"] = held["profiles"]
    report["profile_checks"] = held.get("profile_checks") or []
    report["compositions"] = held.get("compositions") or {}
    if "lineage" in held:
        report["lineage"] = {k: v for k, v in held["lineage"].items() if k != "withheld"} | {
            "withheld": len(held["lineage"]["withheld"])}
    return report


def _llm_stats(llm) -> dict:
    gateway = getattr(llm, "gateway", llm)
    stats = getattr(gateway, "stats", None) or {}
    return {"ok": stats.get("ok", 0), "failed": stats.get("failed", 0), "last_error": stats.get("last_error", "")}


def _router(engine: Engine, *, allow_fake: bool = False):
    from app.clhear.platform.router import Router, build_providers

    providers = build_providers()
    if not providers:
        raise RuntimeError("No model provider is configured")
    if set(providers) == {"fake"} and not allow_fake:
        raise RuntimeError("A live run needs anthropic, openai_compatible, or bedrock")
    return Router(engine, providers=providers)


def _read_profile(location: str) -> dict:
    if location.startswith("s3://"):
        import boto3

        bucket, _, key = location[len("s3://"):].partition("/")
        return json.loads(boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())
    with open(location) as fh:
        return json.load(fh)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.clhear.scope_build")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--layers", default=",".join(layer_builds.ORDER))
    parser.add_argument("--profile", action="append", default=[],
                        help="JSON file or s3:// object of a tenant-submitted L4 profile")
    parser.add_argument("--publish-release", action="store_true")
    parser.add_argument("--refresh-viewer", action="store_true",
                        help="Ask L0 to republish the viewer snapshot after the build")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    import os

    if os.environ.get(scopes.SCOPE_ENV, "") != args.scope:
        parser.error(f"{scopes.SCOPE_ENV} must equal --scope before the registry loads")
    logging.basicConfig(level=logging.INFO)
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    profiles = [_read_profile(location) for location in args.profile]
    report = build(engine, _router(engine), skip_import=args.skip_import, profiles=profiles,
                   layers=tuple(x.strip() for x in args.layers.split(",") if x.strip()))
    if args.publish_release:
        from app.clhear.scope_release import publish

        report["release"] = publish(engine, args.scope)
    if args.refresh_viewer:
        from app.clhear.l1.viewer_snapshot import request_refresh

        report["viewer"] = request_refresh(engine, reason="scope-build")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
