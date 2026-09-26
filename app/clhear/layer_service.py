# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Layer registry service over REAL data: derived L2, curated L3/L4/L5,
computed L6/L7, locked L8 — plus the lineage resolver that walks any item
down to verbatim L1 clauses (clauses_public discipline: restricted sources
resolve to refs + hashes only, never text).
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear import snapshot_cache
from app.clhear.curated import load as load_curated
from app.clhear.derived_models import activities as activities_t
from app.clhear.derived_models import attribute_schema as attribute_schema_t
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import blueprints, obligations
from app.clhear.derived_models import sample_profiles as sample_profiles_t
from app.clhear.l1.models import change_events, clauses, source_families, source_versions, sources
from app.clhear.l1.public import clause_refs_select, clauses_public_select
from app.clhear.layers import LAYER_CATALOG, LAYER_ORDER, layer_public_meta, status_banner
from app.clhear.models import events, llm_calls, proposals, runs

# ------------------------------------------------------------------ registry


def _has_table(conn, table) -> bool:
    """A published release carries the derived layers only, not community or
    curated sample tables."""
    schema = table.schema if conn.dialect.name == "postgresql" else None
    return sa.inspect(conn).has_table(table.name, schema=schema)


def _count(conn, table, *where) -> int:
    stmt = sa.select(sa.func.count()).select_from(table)
    for clause in where:
        stmt = stmt.where(clause)
    return int(conn.execute(stmt).scalar() or 0)


def layer_counts(engine: Engine) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with engine.connect() as conn:
        out["L0"] = {
            "events": _count(conn, events),
            "runs": _count(conn, runs),
            "proposals": _count(conn, proposals),
            "llm_calls": _count(conn, llm_calls),
        }
        ingested = conn.execute(
            sa.select(sa.func.count(sa.func.distinct(source_versions.c.source_id))).where(
                source_versions.c.status == "in_force"
            )
        ).scalar()
        out["L1"] = {
            "families": _count(conn, source_families),
            "sources": _count(conn, sources),
            "sources_ingested": int(ingested or 0),
            "clauses": _count(conn, clauses),
            "clauses_public": _count(conn, clauses, clauses.c.public_ok.is_(True)),
            "change_events": _count(conn, change_events),
            "current_clauses": int(conn.execute(sa.select(sa.func.count()).select_from(
                clauses.join(source_versions, source_versions.c.id == clauses.c.source_version_id))
                .where(source_versions.c.status == "in_force")).scalar() or 0),
        }
        from app.clhear.derived_models import concept_members, concepts

        out["L2"] = {
            "obligations": _count(conn, obligations, obligations.c.status.in_(("derived", "validated"))),
            "validated": _count(conn, obligations, obligations.c.status == "validated"),
            "derived_unreviewed": _count(conn, obligations, obligations.c.status == "derived"),
            "stale": _count(conn, obligations, obligations.c.status == "stale"),
            "concepts": _count(conn, concepts, concepts.c.status != "proposed"),
            "consolidated_obligations": _count(conn, concept_members),
        }
        out["L3"] = {"building_blocks": _count(conn, blocks_t)}
        from app.clhear.derived_models import license_types as license_types_t
        from app.clhear.models import cohorts as cohorts_t

        out["L4"] = {
            "profile_attributes": _count(conn, attribute_schema_t),
            "sample_profiles": _count(conn, sample_profiles_t),
            "license_types": _count(conn, license_types_t),
        }
        try:
            from app.clhear.derived_models import applies_to as applies_to_t
            from app.clhear.derived_models import licences as licences_t
            from app.clhear.derived_models import profiles as profiles_t
            from app.clhear.derived_models import validity_rules as validity_rules_t

            out["L4"].update({
                "licences": _count(conn, licences_t, licences_t.c.valid_to.is_(None)),
                "validity_rules": _count(conn, validity_rules_t, validity_rules_t.c.valid_to.is_(None)),
                "profiles": _count(conn, profiles_t, profiles_t.c.valid_to.is_(None)),
                "applies_to_edges": _count(conn, applies_to_t, applies_to_t.c.valid_to.is_(None)),
            })
        except sa.exc.OperationalError:  # pre-m0012 database
            pass
        out["L5"] = {"activities": _count(conn, activities_t)}
        try:
            from app.clhear.derived_models import implies as implies_t
            from app.clhear.derived_models import mitigates as mitigates_t
            from app.clhear.derived_models import operates as operates_t

            out["L5"].update({
                "business_activities": _count(conn, activities_t, activities_t.c.side == "business", activities_t.c.valid_to.is_(None)),
                "compliance_activities": _count(conn, activities_t, activities_t.c.side == "compliance", activities_t.c.valid_to.is_(None)),
                "implies_edges": _count(conn, implies_t, implies_t.c.valid_to.is_(None)),
                "operates_edges": _count(conn, operates_t, operates_t.c.valid_to.is_(None)),
                "mitigates_edges": _count(conn, mitigates_t, mitigates_t.c.valid_to.is_(None)),
            })
        except sa.exc.OperationalError:  # pre-m0013 database
            pass
        out["L6"] = {
            "sample_programs": _count(conn, sample_profiles_t),
            "blueprints_requested": _count(conn, blueprints),
        }
        try:
            from app.clhear.derived_models import blueprint_items as items_t
            from app.clhear.derived_models import minimality_proofs as proofs_t

            out["L6"].update({
                "blueprints_current": _count(conn, blueprints, blueprints.c.status == "current", blueprints.c.stable_id.isnot(None)),
                "blueprints_superseded": _count(conn, blueprints, blueprints.c.status == "superseded", blueprints.c.stable_id.isnot(None)),
                "items": _count(conn, items_t),
                "minimality_proofs": _count(conn, proofs_t),
            })
        except sa.exc.OperationalError:  # pre-m0014 database
            pass
        from app.clhear.l7.models import risk_scores
        out["L7"] = {"risk_scores": _count(conn, risk_scores, risk_scores.c.valid_to.is_(None))}
        out["L8"] = {
            "benchmark_definitions": len(load_curated("l8_benchmarks")),
            "cohorts": _count(conn, cohorts_t),
            "aggregates_published": _count(conn, cohorts_t, cohorts_t.c.published.is_(True), cohorts_t.c.synthetic.is_(False)),
        }
        from app.clhear.l1.viewer_snapshot import derived_reference_keys

        reference_keys = derived_reference_keys(engine)
        if reference_keys:
            from app.clhear.l8.reference import derived_reference_rows

            out["L8"]["reference_rows"] = len(derived_reference_rows(conn, reference_keys))
    try:
        from app.clhear.governance import audit_coverage as _cov

        for code in ("L2", "L3", "L4", "L5", "L7"):
            extra = _cov(engine, code)
            out.setdefault(code, {})["audit_coverage"] = extra.get("coverage", 0)
    except Exception:
        pass
    return out


@snapshot_cache.cached("layer_index")
def layer_index(engine: Engine) -> list[dict]:
    from app.clhear.l1.inventory import inventory_summary
    from app.clhear.l1.workflow import workflow_summary
    from app.clhear.l1.viewer_snapshot import public_viewer_state

    counts = layer_counts(engine)
    inventory = inventory_summary(engine, scope="registered")
    workflow = workflow_summary(engine)
    viewer = public_viewer_state(engine)
    items = []
    for code in LAYER_ORDER:
        entry = layer_public_meta(code)
        entry["counts"] = counts.get(code, {})
        output_keys = {"L0": "runs", "L1": "current_clauses", "L2": "obligations", "L3": "building_blocks",
                       "L4": "profiles" if "profiles" in entry["counts"] else "profile_attributes",
                       "L5": "activities", "L6": "blueprints_current",
                       "L7": "risk_scores",
                       "L8": "reference_rows" if "reference_rows" in entry["counts"] else "aggregates_published"}
        unit = output_keys[code]
        count = entry["counts"].get(unit, 0)
        entry["overview"] = {
            "state": "published", "verification": "not_evaluated",
            "output_count": count, "output_unit": unit.replace("_", " "),
            "counts": entry["counts"], "scope": LAYER_CATALOG[code]["purpose"],
            "checked_at": None,
            "notice": LAYER_CATALOG[code]["purpose"],
            "detail": LAYER_CATALOG[code]["name"],
        }
        if code == "L1":
            entry["overview"].update({
                "inventory": {key: value for key, value in inventory.items() if key != "sources"},
                "workflow": {"status": workflow.get("status"), "jobs": workflow.get("jobs", [])[:1]},
                "checked_at": inventory.get("audited_at"),
                "job_id": inventory.get("job_id"),
                "viewer_snapshot": viewer,
                "verification": ("passed" if inventory.get("full_scope_verified") is True
                                 and inventory.get("current_binding_valid") is True
                                 else "failed" if inventory.get("status") == "gaps" else "not_evaluated"),
            })
        if LAYER_CATALOG[code]["status"] != "live":
            entry["banner"] = status_banner(code)
        items.append(entry)
    return items


# ----------------------------------------------------------- clause resolver


def resolve_clause(engine: Engine, source_key: str, ref: str) -> dict:
    """Resolve (source_key, ref) to the real L1 clause in the current corpus."""
    with engine.connect() as conn:
        source = conn.execute(sa.select(sources).where(sources.c.key == source_key)).first()
        if source is None:
            return {"resolved": False, "source_key": source_key, "ref": ref, "note": "source not in this corpus snapshot"}
        version = conn.execute(
            sa.select(source_versions)
            .where(source_versions.c.source_id == source.id)
            .where(source_versions.c.status == "in_force")
            .order_by(source_versions.c.id.desc())
            .limit(1)
        ).first()
        base = {
            "resolved": False,
            "source_key": source_key,
            "ref": ref,
            "source_name": source.name,
            "short_name": source.short_name,
            "license": source.license,
            "locked": source.license != "open",
        }
        if version is None:
            base["note"] = (
                "restricted: refs only (no ingested text)"
                if source.license != "open"
                else "no ingested version in this corpus snapshot"
            )
            return base
        clause = conn.execute(
            clause_refs_select()
            .where(clauses.c.source_version_id == version.id)
            .where(clauses.c.ref == ref)
            .limit(1)
        ).first()
        if clause is None:
            base["version_label"] = version.version_label
            base["note"] = "ref not present in the ingested version"
            return base
        public = conn.execute(clauses_public_select(conn).where(clauses.c.id == clause.id)).first()
        return {
            **base,
            "resolved": True,
            "clause_id": clause.id,
            "doc_node_id": clause.doc_node_id,
            "locked": public is None,
            "path": public.path if public is not None else None,
            "text": public.text if public is not None else None,
            "text_hash": clause.text_hash,
            "version_label": version.version_label,
            "as_of_date": str(version.as_of_date) if version.as_of_date else None,
            "retrieved_at": str(version.retrieved_at),
            "content_hash": version.content_hash,
            "s3_uri": version.s3_uri,
            "permalink": f"/l1?source={source_key}&node={clause.doc_node_id}" if clause.doc_node_id else f"/l1?source={source_key}",
        }


# -------------------------------------------------------------------- items


def _obligation_dict(row) -> dict:
    d = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    d["confidence"] = float(d["confidence"])
    for k in ("derived_at", "validated_at"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def obligation_items(
    engine: Engine,
    q: str | None = None,
    source_key: str | None = None,
    status: str | None = None,
    limit: int = 60,
    offset: int = 0,
) -> dict:
    stmt = sa.select(obligations)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(sa.or_(obligations.c.title.ilike(like), obligations.c.statement.ilike(like), obligations.c.clause_ref.ilike(like)))
    if source_key:
        stmt = stmt.where(obligations.c.source_key == source_key)
    if status:
        stmt = stmt.where(obligations.c.status == status)
    else:
        stmt = stmt.where(obligations.c.status != "rejected")
    with engine.connect() as conn:
        total = int(conn.execute(sa.select(sa.func.count()).select_from(stmt.subquery())).scalar() or 0)
        rows = conn.execute(stmt.order_by(obligations.c.source_key, obligations.c.id).limit(limit).offset(offset)).all()
        per_source = [
            {"source_key": r.source_key, "count": r.n}
            for r in conn.execute(
                sa.select(obligations.c.source_key, sa.func.count().label("n"))
                .where(obligations.c.status.in_(("derived", "validated")))
                .group_by(obligations.c.source_key)
                .order_by(sa.desc("n"))
            )
        ]
    items = [_obligation_dict(r) for r in rows]
    from app.clhear.community import vote_tallies
    from app.clhear.community_models import votes

    with engine.connect() as conn:
        has_votes = _has_table(conn, votes)
    tallies = vote_tallies(engine, [i["id"] for i in items]) if has_votes else {}
    for item in items:
        item["community"] = tallies.get(item["id"], {"confirm": 0, "dispute": 0, "promotion_suggested": False})
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "per_source": per_source,
        "items": items,
    }


def stored_programs(engine: Engine) -> list[dict]:
    """The current blueprint of every stored L4 profile: a tenant's program."""
    from app.clhear.derived_models import profiles as profiles_t

    with engine.connect() as conn:
        names = dict(conn.execute(sa.select(profiles_t.c.id, profiles_t.c.name).where(profiles_t.c.valid_to.is_(None))).all())
        rows = conn.execute(sa.select(blueprints).where(blueprints.c.status == "current", blueprints.c.profile_id.isnot(None),
                                                        blueprints.c.stable_id.isnot(None)).order_by(blueprints.c.id)).mappings().all()
    out = []
    for row in rows:
        if row["profile_id"] not in names:
            continue
        comp = row["composition"] if isinstance(row["composition"], dict) else json.loads(row["composition"] or "{}")
        out.append({
            "id": f"PRG:{row['stable_id']}",
            "name": f"Program — {names[row['profile_id']] or row['profile_id']}",
            "profile_id": row["profile_id"],
            "blueprint_id": row["stable_id"],
            "status": "current",
            "engine_version": row["engine_version"],
            "coverage_summary": comp.get("coverage_summary"),
            "obligations_triggered": comp.get("obligations_triggered"),
            "blocks": comp.get("blocks", []),
            "items": [{k: item.get(k) for k in ("block_id", "name", "kind", "basis", "obligations_satisfied", "load_bearing_for")}
                      for item in comp.get("items", [])],
            "coverage": comp.get("coverage", [])[:40],
            "unmapped_obligations": (comp.get("unmapped_obligations") or {}).get("count", 0),
        })
    return out


def _profile_blueprint(engine: Engine, profile_row) -> dict:
    """The profile's current stored blueprint from this engine version; composed only when none is stored."""
    from app.clhear.l6.composer import _current_for, compose
    from app.clhear.l6.models import ENGINE_VERSION, fingerprint

    profile = {
        "attributes": profile_row.attributes if isinstance(profile_row.attributes, dict) else json.loads(profile_row.attributes),
        "activities": profile_row.activities if isinstance(profile_row.activities, list) else json.loads(profile_row.activities),
    }
    with engine.connect() as conn:
        current = _current_for(conn, fingerprint(profile["attributes"], profile["activities"]))
    if current and current["engine_version"] == ENGINE_VERSION and current["composition"]:
        stored = current["composition"]
        return json.loads(stored) if isinstance(stored, str) else stored
    return compose(engine, profile, requested_by="stack-ui-sample", log_request=False)


@snapshot_cache.cached("layer_items")
def layer_items(engine: Engine, layer: str, **filters) -> list[dict] | dict:
    if layer == "L2":
        return obligation_items(engine, **filters)
    if layer == "L3":
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]
        for b in rows:
            b["updated_at"] = str(b.get("updated_at"))
            resolved = 0
            for sel in b["satisfies"]:
                from app.clhear.l6.composer import resolve_anchor

                resolved += len(resolve_anchor(engine, {"source_key": sel["source_key"], "refs": sel.get("refs")}))
            b["satisfies_resolved_obligations"] = resolved
        return rows
    if layer == "L4":
        from app.clhear.l4.licenses import list_license_types

        from app.clhear.derived_models import profiles as profiles_t

        with engine.connect() as conn:
            schema_rows = [dict(r) for r in conn.execute(sa.select(attribute_schema_t)).mappings()]
            profile_rows = ([dict(r) for r in conn.execute(sa.select(sample_profiles_t)).mappings()]
                            if _has_table(conn, sample_profiles_t) else [])
            stored = [{k: (str(v) if k in ("valid_from", "valid_to", "created_at", "updated_at") and v is not None else v)
                       for k, v in dict(r).items()}
                      for r in conn.execute(sa.select(profiles_t).where(profiles_t.c.valid_to.is_(None))
                                            .order_by(profiles_t.c.id)).mappings()]
        out = {
            "attribute_schema": schema_rows,
            "profiles": stored,
            "sample_profiles": profile_rows,
            "license_types": list_license_types(engine),
            "authorisations_enum": sorted({r["name"] for r in list_license_types(engine)}),
        }
        try:
            from app.clhear.l4.ontology import ontology as l4_ontology

            onto = l4_ontology(engine)
            out["ontology"] = {k: len(onto[k]) for k in ("licences", "products_services", "client_types", "channels", "permits", "validity_rules")}
            out["ontology_version"] = onto["version"]
            out["authorisations_enum"] = sorted(set(out["authorisations_enum"]) | {r["name"] for r in onto["licences"]})
        except sa.exc.OperationalError:  # pre-m0012 database
            pass
        return out
    if layer == "L5":
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(sa.select(activities_t)).mappings()]
        for a in rows:
            a["updated_at"] = str(a.get("updated_at"))
            for k in ("valid_from", "valid_to", "derived_at"):
                if a.get(k) is not None:
                    a[k] = str(a[k])
            if a.get("confidence") is not None:
                a["confidence"] = float(a["confidence"])
        return rows
    if layer == "L6":
        out = stored_programs(engine)
        with engine.connect() as conn:
            profiles = conn.execute(sa.select(sample_profiles_t)).all() if _has_table(conn, sample_profiles_t) else []
        for p in profiles:
            bp = _profile_blueprint(engine, p)
            out.append(
                {
                    "id": f"PRG:{p.id}",
                    "name": f"Sample program — {p.name}",
                    "profile_id": p.id,
                    "status": "computed-sample",
                    "engine_version": bp["engine_version"],
                    "coverage_summary": bp["coverage_summary"],
                    "obligations_triggered": bp["obligations_triggered"],
                    "blocks": bp["blocks"],
                    "coverage": bp["coverage"][:40],
                    "unmapped_obligations": bp["unmapped_obligations"]["count"],
                }
            )
        return out
    if layer == "L7":
        return risk_score_items(engine)
    if layer == "L8":
        from app.clhear.l8.cohorts import list_cohorts
        from app.clhear.l8.reference import reference_rows

        from app.clhear.l1.viewer_snapshot import read_viewer_state

        state = read_viewer_state(engine)
        if state.get("viewer_snapshot"):
            keys = list((state.get("derived_scope") or {}).get("reference") or [])
            reference = reference_rows(engine, source_keys=keys) if keys else []
        else:
            reference = reference_rows(engine)
        return {"definitions": load_curated("l8_benchmarks"), "cohorts": list_cohorts(engine),
                "reference": reference}
    raise KeyError(layer)


@snapshot_cache.cached("risk_scores")
def risk_score_items(engine: Engine, limit: int = 200) -> list[dict]:
    """Published L7 obligation scores, riskiest first, each named by its L2 obligation."""
    from app.clhear.l7.score import list_scores

    with engine.connect() as conn:
        scores = list_scores(conn, kind="obligation", limit=limit)
        ids = [s["subject_ref"] for s in scores]
        named = {r.id: r for r in conn.execute(
            sa.select(obligations.c.id, obligations.c.title, obligations.c.source_key, obligations.c.clause_ref)
            .where(obligations.c.id.in_(ids)))} if ids else {}
    out = []
    for score in scores:
        ob = named.get(score["subject_ref"])
        evidence = score.get("evidence") or {}
        out.append({**score,
                    "title": ob.title if ob else score["subject_ref"],
                    "summary": f"{ob.source_key} · {ob.clause_ref}" if ob else "",
                    "result": {"score": score["composite"], "band": score["band"], "components": score["dimensions"]},
                    "inputs": {"event_count": evidence.get("event_count", 0),
                               "total_amount": evidence.get("total_amount", 0)}})
    return out


# ------------------------------------------------------------------ lineage


def _node(layer, kind, item_id, title, detail="", meta=None, children=None) -> dict:
    return {"layer": layer, "kind": kind, "id": item_id, "title": title, "detail": detail,
            "meta": meta or {}, "children": children or []}


def _clause_leaf(engine: Engine, source_key: str, ref: str, role: str = "") -> dict:
    resolved = resolve_clause(engine, source_key, ref)
    title = f"{resolved.get('short_name') or source_key} · {ref}"
    return _node("L1", "clause", f"{source_key}#{ref}", title, role, meta=resolved)


def _obligation_row(engine: Engine, obligation_id: str):
    with engine.connect() as conn:
        return conn.execute(sa.select(obligations).where(obligations.c.id == obligation_id)).first()


def _obligation_node(engine: Engine, row) -> dict:
    return _node(
        "L2", "obligation", row.id, row.title,
        row.statement or "(restricted or non-public basis: statement withheld)",
        meta={
            "status": row.status, "confidence": float(row.confidence), "modality": row.modality,
            "addressee": row.addressee, "method": row.method, "jurisdiction": row.jurisdiction,
            "derivation": {
                "status": row.status,
                "method": f"{row.method} deterministic extraction from the anchored clause",
                "confidence": float(row.confidence),
                "validated_by": row.validated_by,
            },
        },
        children=[_clause_leaf(engine, row.source_key, row.clause_ref, "basis (hash-anchored)")],
    )


def _anchor_nodes(engine: Engine, anchor: dict, detail: str = "") -> list[dict]:
    from app.clhear.l6.composer import resolve_anchor

    resolved = resolve_anchor(engine, anchor)
    nodes = []
    for ob in resolved:
        row = _obligation_row(engine, ob["id"])
        if row is not None:
            node = _obligation_node(engine, row)
            if detail:
                node["detail"] = detail
            nodes.append(node)
    if not resolved:
        for ref in anchor.get("refs", []) or ["(all)"]:
            leaf = _clause_leaf(engine, anchor["source_key"], ref, "anchor (no derived obligation)")
            nodes.append(leaf)
    return nodes


def lineage(engine: Engine, layer: str, item_id: str) -> dict:
    if layer == "L2" and item_id.startswith("CON:"):
        from app.clhear.l2.concepts import get_concept

        concept = get_concept(engine, item_id)
        if concept is None:
            raise KeyError(item_id)
        facet_nodes = []
        by_jur: dict[str, list[dict]] = {}
        for m in concept.get("members", []):
            by_jur.setdefault(m["jurisdiction"] or "unspecified", []).append(m)
        for jur, members in sorted(by_jur.items()):
            children = []
            for m in members:
                row = _obligation_row(engine, m["obligation_id"])
                if row is not None:
                    node = _obligation_node(engine, row)
                    if m.get("note"):
                        node["detail"] = f"{jur} facet — {m['note']}"
                    children.append(node)
            facet_nodes.append(
                _node("L2", "jurisdiction_facet", f"{item_id}/{jur}", f"{jur} facet",
                      f"{len(children)} clause-anchored obligation(s)", children=children)
            )
        return _node(
            "L2", "concept", concept["id"], concept["name"],
            concept["canonical_statement"],
            meta={"status": concept["status"], "jurisdictions": concept["jurisdictions"],
                  "drafted_by": concept["drafted_by"], "approved_by": concept["approved_by"],
                  "derivation": {"status": concept["status"],
                                 "method": "consolidation overlay: members stay clause-anchored; "
                                           "resolution is parameterized by your jurisdiction set"}},
            children=facet_nodes,
        )

    if layer == "L2":
        row = _obligation_row(engine, item_id)
        if row is None:
            raise KeyError(item_id)
        return _obligation_node(engine, row)

    if layer == "L3":
        with engine.connect() as conn:
            row = conn.execute(sa.select(blocks_t).where(blocks_t.c.id == item_id)).first()
        if row is None:
            raise KeyError(item_id)
        children = []
        for sel in row.satisfies:
            children.extend(_anchor_nodes(engine, {"source_key": sel["source_key"], "refs": sel.get("refs")}, "satisfied by this block"))
        for control in row.implements_controls:
            children.append(_clause_leaf(engine, control["source_key"], control["ref"], "implements control"))
        return _node("L3", "building_block", row.id, row.name, row.description,
                     meta={"status": row.status, "capability": row.capability}, children=children)

    if layer == "L5":
        with engine.connect() as conn:
            row = conn.execute(sa.select(activities_t).where(activities_t.c.id == item_id)).first()
        if row is None:
            raise KeyError(item_id)
        children = []
        for trig in row.triggers:
            cond = ", ".join(f"{k}={v}" for k, v in (trig.get("when") or {}).items()) or "always"
            children.extend(_anchor_nodes(engine, trig["anchor"], f"triggered when {cond}"))
        return _node("L5", "activity", row.id, row.name, row.description,
                     meta={"status": row.status}, children=children)

    if layer == "L4":
        with engine.connect() as conn:
            row = conn.execute(sa.select(sample_profiles_t).where(sample_profiles_t.c.id == item_id)).first()
        if row is None:
            raise KeyError(item_id)
        children = [lineage(engine, "L5", act_id) for act_id in row.activities]
        return _node("L4", "profile", row.id, row.name, row.description,
                     meta={"attributes": row.attributes, "status": row.status}, children=children)

    if layer == "L6":
        profile_id = item_id.split(":", 1)[1] if item_id.startswith("PRG:") else item_id
        stored = next((prog for prog in stored_programs(engine) if prog["blueprint_id"] == profile_id), None)
        if stored is not None:
            with engine.connect() as conn:
                block_ids = [b["id"] for b in stored["blocks"]
                             if conn.execute(sa.select(blocks_t.c.id).where(blocks_t.c.id == b["id"])).first() is not None]
            summary = stored["coverage_summary"] or {}
            return _node(
                "L6", "program", item_id, stored["name"],
                f"{summary.get('covered')}/{summary.get('total')} obligations covered · engine {stored['engine_version']}",
                meta={"coverage_summary": summary, "gaps": [c for c in stored["coverage"] if c.get("state") == "gap"][:15],
                      "unmapped_obligations": stored["unmapped_obligations"], "status": "current",
                      "blueprint_id": stored["blueprint_id"], "profile_id": stored["profile_id"]},
                children=[lineage(engine, "L3", b) for b in block_ids],
            )
        with engine.connect() as conn:
            row = (conn.execute(sa.select(sample_profiles_t).where(sample_profiles_t.c.id == profile_id)).first()
                   if _has_table(conn, sample_profiles_t) else None)
        if row is None:
            raise KeyError(item_id)
        bp = _profile_blueprint(engine, row)
        children = []
        with engine.connect() as conn:
            for b in bp["blocks"]:
                block_row = conn.execute(sa.select(blocks_t).where(blocks_t.c.id == b["id"])).first()
                if block_row is not None:
                    children.append(lineage(engine, "L3", block_row.id))
        gaps = [c for c in bp["coverage"] if c["state"] == "gap"]
        return _node(
            "L6", "program", item_id, f"Sample program — {row.name}",
            f"{bp['coverage_summary']['covered']}/{bp['coverage_summary']['total']} obligations covered · engine {bp['engine_version']}",
            meta={"coverage_summary": bp["coverage_summary"], "gaps": gaps[:15],
                  "unmapped_obligations": bp["unmapped_obligations"], "status": "computed-sample"},
            children=children,
        )

    if layer == "L7":
        from app.clhear.l7.models import enforcement_events
        from app.clhear.l7.score import get_score

        with engine.connect() as conn:
            score = get_score(conn, item_id)
            if score is None or score["subject_kind"] != "obligation":
                raise KeyError(item_id)
            event_ids = (score.get("evidence") or {}).get("events", [])[:20]
            events_rows = conn.execute(sa.select(enforcement_events).where(
                enforcement_events.c.id.in_(event_ids), enforcement_events.c.valid_to.is_(None))).mappings().all() if event_ids else []
        children = []
        row = _obligation_row(engine, score["subject_ref"])
        if row is not None:
            children.append(_obligation_node(engine, row))
        for event in events_rows:
            detail = " · ".join(str(v) for v in (event["respondent"], event["decided_on"], event["amount"]) if v)
            children.append(_node("L7", "enforcement_event", event["id"], event["title"] or event["id"], detail,
                                  children=[_clause_leaf(engine, event["source_key"], event["clause_ref"], "published outcome")]))
        title = row.title if row is not None else score["subject_ref"]
        return _node("L7", "risk_score", item_id, f"{title} — {score['composite']} ({score['band']})",
                     f"{score['method_version']}: weighted sum of published dimensions",
                     meta={"dimensions": score["dimensions"], "weights": score["weights"], "evidence": score["evidence"],
                           "calibration": score["calibration_set_ref"]},
                     children=children)

    if layer == "L8":
        for item in load_curated("l8_benchmarks"):
            if item["id"] == item_id:
                return _node("L8", "benchmark", item["id"], item["name"], item.get("definition", ""),
                             meta={"locked": True, "k_anonymity": item.get("k_anonymity"), "cluster": item.get("cluster"),
                                   "note": "Closed by design: raw peer data never leaves the enclave; no aggregates are published today."})
        raise KeyError(item_id)

    raise KeyError(layer)
