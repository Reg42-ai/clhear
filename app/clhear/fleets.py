# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Nightly AI fleet orchestrator — one pass after L1 ingest."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear.models import runs

log = logging.getLogger("clhear.fleets")

FLEET_RUN = "ai.nightly"


def _already_ran_today(engine: Engine) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    import sqlalchemy as sa

    with engine.connect() as conn:
        for row in conn.execute(
            sa.select(runs).where(runs.c.fleet == FLEET_RUN).order_by(runs.c.id.desc()).limit(8)
        ):
            ts = str(row.created_at)
            if not ts.startswith(today):
                continue
            # Safe FakeProvider rehearsals must not block the real nightly run.
            if (row.trigger or "") == "rehearsal":
                continue
            return True
    return False


def compose_stored_profiles(engine: Engine, profile_ids: list[str] | None = None) -> dict:
    """L6 composers: a current blueprint for every stored valid L4 profile (idempotent).

    ``profile_ids`` limits the pass to those profiles. None composes every
    valid stored profile, which is the nightly fleet. A scoped build passes
    the profiles it just stored and leaves every other profile's blueprint.
    """
    import sqlalchemy as sa

    from app.clhear.derived_models import blueprints, profiles
    from app.clhear.l6.composer import compose_for_profile

    composed = stored = 0
    try:
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(sa.select(profiles.c.id).where(profiles.c.status == "valid").order_by(profiles.c.id))]
    except sa.exc.OperationalError:  # pre-m0012 database
        return {"composed": 0, "stored": 0}
    if profile_ids is not None:
        wanted = set(profile_ids)
        ids = [i for i in ids if i in wanted]
    for pid in ids:
        with engine.connect() as conn:
            before = {r[0] for r in conn.execute(sa.select(blueprints.c.stable_id).where(blueprints.c.profile_id == pid))}
        bp = compose_for_profile(engine, pid, requested_by="l6.compose:nightly")
        composed += 1
        if bp.get("blueprint_id") not in before:
            stored += 1
    return {"composed": composed, "stored": stored}


def run_nightly_stack(engine: Engine, llm, *, force: bool = False) -> dict:
    """Extract → triage → consolidate → L3/L4/L5/L6/L7/L8 → eval gates."""
    from app.clhear.curated import seed_data_model
    from app.clhear.l2.concepts import flag_stale_concepts
    from app.clhear.l2.consolidate import draft_and_propose
    from app.clhear.l2.extract import run_extraction
    from app.clhear.l2.triage import triage_duties
    from app.clhear.l3.generate import generate_blocks
    from app.clhear.l4.licenses import extract_licenses
    from app.clhear.l5.map import map_activities
    from app.clhear.l7.narrate import narrate_risk
    from app.clhear.l8.cohorts import refresh_cohorts
    from app.clhear.platform import evals as ev

    from app.clhear.l2.change import nightly_change_pass
    from app.clhear.l2.dedupe import consolidate as l2_consolidate
    from app.clhear.l2.review import review_obligations
    from app.clhear.l2.structured import refine_structured
    from app.clhear.l3.characterize import characterize as l3_characterize
    from app.clhear.l3.decompose import decompose as l3_decompose
    from app.clhear.l3.harmonize import harmonize as l3_harmonize

    started = datetime.now(timezone.utc)
    seeded = seed_data_model(engine)
    extraction = run_extraction(engine)
    triage = triage_duties(engine, llm)
    structured = refine_structured(engine, llm)
    l2_changes = nightly_change_pass(engine, llm)
    flagged = flag_stale_concepts(engine)
    consolidation = draft_and_propose(engine, llm)
    registry_consolidation = l2_consolidate(engine)
    reviews = review_obligations(engine, llm)
    # I4: what a maintainer accepted must survive the cycle — re-asserted on the
    # same basis, escalated to the console when the basis moved.
    from app.clhear.platform import console

    human_edits = {"L2": console.reproduce_human_edits(engine, layer="L2")}
    blocks = generate_blocks(engine, llm)
    decomposition = l3_decompose(engine)
    harmonisation = l3_harmonize(engine)
    characterisation = l3_characterize(engine, llm)
    human_edits["L3"] = console.reproduce_human_edits(engine, layer="L3")
    licenses = extract_licenses(engine, llm)
    # L4 (HLD v2 §4.4): register-backed ontology, applicability predicates, profile re-validation.
    from app.clhear.l4.ontology import build_ontology
    from app.clhear.l4.predicates import extract_predicates
    from app.clhear.l4.validate import revalidate_profiles

    ontology = build_ontology(engine)
    predicates = extract_predicates(engine, llm)
    profile_revalidation = revalidate_profiles(engine)
    human_edits["L4"] = console.reproduce_human_edits(engine, layer="L4")
    # L5 (HLD v2 §4.5): deterministic mapping of every obligation, router refinement, junction build, orphan check.
    from app.clhear.l5.check import check_junction

    activities = map_activities(engine, llm)
    junction = check_junction(engine)
    human_edits["L5"] = console.reproduce_human_edits(engine, layer="L5")
    # L6 (HLD v2 §4.6): composers for every stored profile (diff engine supersedes
    # changed blueprints and publishes clhear.l6.changed), explainers on the
    # current blueprints (rubric-gated), citation-checked program rationale.
    from app.clhear import layer_service
    from app.clhear.l6 import composer as l6_composer
    from app.clhear.l6.diff import recompose as l6_recompose
    from app.clhear.l6.explain import refine_explanations
    from app.clhear.l6.rationale import narrate_blueprint

    blueprints_out = compose_stored_profiles(engine)
    recomposition = l6_recompose(engine, cause="nightly")
    rationales = []
    explanations = []
    with engine.connect() as conn:
        current = l6_composer.list_blueprints(conn, status="current", limit=3)
        comps = [l6_composer.get_blueprint(conn, r["blueprint_id"]) for r in current]
    for bp in comps:
        if not bp or not bp["composition"].get("items"):
            continue
        comp = dict(bp["composition"], blueprint_id=bp["blueprint_id"])
        explanations.append(refine_explanations(engine, llm, comp))
        rationales.append(narrate_blueprint(engine, llm, comp))
    # L7 (HLD v2 §4.7): enforcement outcomes read from the L1 enforcement sources,
    # linked to obligations by printed citation, then the calibrated scorer —
    # calibration on the held-out year first so every score names its run.
    from app.clhear.l7 import enforcement as l7_enforcement
    from app.clhear.l7 import score as l7_score

    l7 = {"events": l7_enforcement.ingest_events(engine)}
    l7["links"] = l7_enforcement.link_events(engine, llm)
    l7["calibration"] = l7_score.calibrate(engine)
    l7["obligation_scores"] = l7_score.score_obligations(engine)
    l7["item_scores"] = l7_score.score_items(engine)
    narratives = [narrate_risk(engine, llm, it) for it in layer_service.risk_score_items(engine)[:4]]
    cohorts = refresh_cohorts(engine)
    # L8 (HLD v2 §4.8): drift first so a changed block re-derives its fills before new
    # slots are drafted; then the member aggregates (k ≥ 5, DP noise) — nothing here
    # waits on a contribution.
    from app.clhear.l8 import aggregate as l8_aggregate
    from app.clhear.l8 import fills as l8_fills

    l8 = {}
    for name, fn in (("drift", lambda: l8_fills.detect_drift(engine)),
                     ("fills", lambda: l8_fills.generate_fills(engine, llm)),
                     ("aggregates", lambda: l8_aggregate.aggregate(engine, release=started.strftime("%Y%m%dT%H%M%SZ")))):
        try:
            l8[name] = fn()
        except Exception as exc:
            log.exception("l8 %s failed", name)
            l8[name] = {"error": str(exc)[:200]}
    gates = {}
    for suite in (
        "l2_basis_integrity", "l2_extraction_quality", "l2_concept_integrity",
        "l2_coverage", "l2_precision", "l2_dedupe", "l2_change_inference",
        "l3_completeness", "l3_characteristics", "l3_reuse", "l3_precision",
        "l3_l5_referential", "l4_applicability", "l4_grounding",
        "l5_completeness",
        "l6_completeness", "l6_minimality", "l6_reference", "l6_explanation", "l6_citation",
        "l7_linker", "l7_brier", "l7_number_echo",
        "l8_k_anonymity", "l8_reidentification", "l8_traceability", "l8_fill_rubric",
    ):
        try:
            gates[suite] = ev.run_suite(engine, suite, release=started.strftime("%Y%m%dT%H%M%SZ"))
        except Exception as exc:
            log.exception("suite %s failed", suite)
            gates[suite] = {"suite": suite, "passed": False, "error": str(exc)[:200]}
    # HLD v2 I7: projections are rebuilt from the record after the derivation
    # passes — the query graph (Neo4j or in-process) and the clause vector index.
    from app.clhear.platform import embeddings as _embeddings
    from app.clhear.platform import graph as _graph

    # HLD v2 §6 / I12: every checked community contribution gets its fleet verdict
    # tonight (agree / disagree / unverified) so reviewers decide on evidence; the
    # change digest goes to the newsletter when beehiiv is configured (inert otherwise).
    from app.clhear.platform import contributions as _contributions
    from app.clhear.platform import newsletter as _newsletter

    try:
        community = {"rederived": _contributions.rederive_pending(engine)}
    except Exception as exc:
        log.exception("contribution re-derivation failed")
        community = {"rederived": {"error": str(exc)[:200]}}
    try:
        community["digest"] = _newsletter.send_digest(engine)
    except Exception as exc:
        log.exception("newsletter digest failed")
        community["digest"] = {"sent": False, "reason": str(exc)[:200]}
    nightly_release = started.strftime("%Y%m%dT%H%M%SZ")
    projections = {
        "graph": _graph.rebuild(engine, release=nightly_release, trigger="nightly"),
        "index": _embeddings.rebuild_index(engine, release=nightly_release, trigger="nightly"),
    }
    outputs = {
        "extraction": extraction,
        "triage": triage,
        "projections": {"graph": {k: projections["graph"].get(k) for k in ("backend", "nodes", "edges", "status", "duration_ms")},
                        "index": {k: projections["index"].get(k) for k in ("backend", "model", "embedded", "skipped", "duration_ms")}},
        "structured": structured,
        "l2_changes": l2_changes,
        "registry_consolidation": registry_consolidation,
        "reviews": reviews,
        "human_edits": {lay: {k: v for k, v in out.items() if k != "details"} for lay, out in human_edits.items()},
        "data_model": seeded,
        "consolidation": consolidation,
        "flagged_concepts": flagged,
        "blocks": blocks,
        "decomposition": decomposition,
        "harmonisation": harmonisation,
        "characterisation": characterisation,
        "licenses": licenses,
        "ontology": {"version": ontology["version"], "counts": ontology["counts"],
                     "registers": {k: v.get("freshness") for k, v in ontology["registers"].items()}},
        "predicates": predicates,
        "profile_revalidation": profile_revalidation,
        "activities": activities,
        "junction": {"activities": junction["activities"], "edges": junction["edges"], "orphans": len(junction["orphans"]),
                     "dangling": len(junction["dangling"]), "ok": junction["ok"]},
        "blueprints": blueprints_out,
        "recomposition": {k: v for k, v in recomposition.items() if k != "changes"},
        "explanations": explanations,
        "rationales": rationales,
        "narratives": [{"written": n.get("written"), "id": n.get("id")} for n in narratives],
        "l7": {"events": l7["events"], "links": l7["links"],
               "calibration": {k: l7["calibration"].get(k) for k in ("status", "id", "held_out_year", "brier", "baseline_brier", "beats_baseline")},
               "obligation_scores": {k: v for k, v in l7["obligation_scores"].items() if k != "bands"},
               "item_scores": l7["item_scores"]},
        "cohorts": cohorts,
        "l8": {"drift": {k: v for k, v in l8["drift"].items() if k != "details"},
               "fills": {k: v for k, v in l8["fills"].items() if k != "ids"},
               "aggregates": {k: v for k, v in l8["aggregates"].items() if k not in ("ids", "suppressed_detail")}},
        "community": community,
        "gates": {k: {"passed": v.get("passed")} for k, v in gates.items()},
    }
    reasoning = (
        f"Nightly fleets: {extraction.get('inserted', 0)} obligations extracted, "
        f"{triage.get('inserted', 0)} triaged, {registry_consolidation['dedupe'].get('merged', 0)} deduped, "
        f"{registry_consolidation['equivalences'].get('written', 0)} equivalences, "
        f"{reviews.get('reviewed', 0)} reviewed, {consolidation.get('applied', 0)} concepts applied, "
        f"{blocks.get('written', 0)} blocks generated, {decomposition.get('linked_curated', 0) + decomposition.get('linked_derived', 0)} "
        f"obligations decomposed, {harmonisation.get('merged', 0)} blocks harmonised, "
        f"{characterisation.get('backed', 0)} characteristics backed, {licenses.get('written', 0)} licenses, "
        f"ontology {ontology['version']} ({sum(c['added'] + c['updated'] for c in ontology['counts'].values())} rows changed), "
        f"{predicates.get('added', 0)} applicability edges added, {profile_revalidation.get('changed', 0)} profiles flipped, "
        f"{activities.get('written', 0)} obligations mapped to activities, junction "
        f"{'consistent' if junction['ok'] else str(len(junction['orphans'])) + ' orphan(s)'}, "
        f"{blueprints_out['composed']} blueprints composed ({blueprints_out['stored']} new), "
        f"{recomposition['changed']}/{recomposition['checked']} recomposed after lower-layer changes, "
        f"{sum(e['accepted'] for e in explanations)} explanations refined; "
        f"{sum(1 for g in gates.values() if g.get('passed'))}/{len(gates)} eval gates green; "
        f"graph projection {projections['graph'].get('status')} ({projections['graph'].get('nodes', 0)} nodes / "
        f"{projections['graph'].get('edges', 0)} edges), {projections['index'].get('embedded', 0)} clauses re-embedded; "
        f"human edits: {sum(o['reproduced'] for o in human_edits.values())} reproduced, "
        f"{sum(o['escalated'] for o in human_edits.values())} escalated"
    )
    import time

    # duration approximated
    ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    import sqlalchemy as sa

    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet=FLEET_RUN, trigger="schedule", inputs={"force": force},
                outputs=outputs, duration_ms=ms, reasoning=reasoning,
            )
        )
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L0", fleet=FLEET_RUN,
            reasoning=reasoning, detail={"gates": outputs["gates"]},
        )
    except Exception:
        log.exception("nightly ai_ops failed")
    return outputs


def run_nightly_if_due(engine: Engine, llm, *, force: bool = False, **_legacy) -> dict | None:
    """Run the nightly stack once per UTC day. Inference is remote (the configured model provider on
    Bedrock, I6) so there is nothing to provision or tear down around the run."""
    if not force and _already_ran_today(engine):
        return None
    return run_nightly_stack(engine, llm, force=force)


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m app.clhear.fleets nightly [--release YYYY.MM.DD] [--force] [--fake]

    Runs the layer fleets once against the configured store using whatever
    inference providers are configured (the configured model provider in production; the
    FakeProvider with ``--fake`` or when nothing is configured, which is the
    replay posture used by CI). No GPU is launched: inference is remote (I6).
    """
    import argparse
    import json
    import sys

    from app.clhear.db import get_engine, run_migrations
    from app.clhear.platform.gateway import FakeProvider
    from app.clhear.platform.router import Router, build_providers

    parser = argparse.ArgumentParser(prog="python -m app.clhear.fleets")
    sub = parser.add_subparsers(dest="cmd", required=True)
    nightly = sub.add_parser("nightly")
    nightly.add_argument("--release", default=None, help="release id the run is attributed to")
    nightly.add_argument("--force", action="store_true")
    nightly.add_argument("--fake", action="store_true", help="use the deterministic FakeProvider")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    engine = get_engine()
    run_migrations(engine)
    providers = {} if args.fake else build_providers()
    if not providers:
        log.warning("no inference providers configured; using FakeProvider (replay posture)")
        providers = {"fake": FakeProvider()}
    llm = Router(engine, providers=providers)
    if not args.force and _already_ran_today(engine):
        print(json.dumps({"skipped": "already ran today", "release": args.release}))
        return 0
    outputs = run_nightly_stack(engine, llm, force=args.force)
    outputs["release"] = args.release
    print(json.dumps({k: v for k, v in outputs.items() if k in ("gates", "release", "cohorts")}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
