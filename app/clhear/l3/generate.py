# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 block generation — synthesis grounded at the edges.

Every block must declare `satisfies` anchors that resolve to live obligations.
Near-duplicates are merged by Jaccard similarity. Free-form fields are labeled
AI-designed and prioritized for Eval Studio sampling.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import obligations
from app.clhear.l2.consolidate import _jaccard, _tokens
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l3.generate")

MAX_BLOCKS = 40      # model calls per run; obligations beyond them are reported as remaining
CLUSTER_SIZE = 10
DEDUP_SIM = 0.55


def _unlinked(engine: Engine) -> list[dict]:
    """Live obligations in scope that no measure satisfies yet."""
    from app.clhear.derived_models import requires
    from app.clhear.l1.scopes import in_scope

    with engine.connect() as conn:
        linked = {r[0] for r in conn.execute(sa.select(requires.c.obligation_id).where(requires.c.valid_to.is_(None)))}
        return [
            dict(r)
            for r in conn.execute(
                sa.select(obligations).where(obligations.c.status.in_(("derived", "validated")))
                .order_by(obligations.c.source_key, obligations.c.id)
            ).mappings()
            if in_scope(r["source_key"]) and r["id"] not in linked
        ]


def _clusters(engine: Engine) -> list[list[dict]]:
    """Unlinked obligations grouped by duty type, in batches the model can design one measure for."""
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in _unlinked(engine):
        by_type[r.get("obligation_type") or "other"].append(r)
    clusters = []
    for kind in sorted(by_type):
        group = by_type[kind]
        clusters.extend(group[i:i + CLUSTER_SIZE] for i in range(0, len(group), CLUSTER_SIZE))
    return clusters


def _existing_blocks(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]


def _duplicate_of(name: str, existing: list[dict]) -> str | None:
    """The id of an existing measure with (nearly) the same name."""
    tok = _tokens(name)
    for b in existing:
        if b.get("id") and _jaccard(tok, _tokens(b["name"])) >= DEDUP_SIM:
            return b["id"]
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def generate_blocks(engine: Engine, llm, limit: int = MAX_BLOCKS) -> dict:
    existing = _existing_blocks(engine)
    written = blocked = reused = 0
    ids: list[str] = []
    clusters = _clusters(engine)
    remaining = sum(len(c) for c in clusters[limit:])
    for cluster in clusters[:limit]:
        live_ids = {o["id"] for o in cluster}
        prompt = (
            "Design ONE reusable compliance building block (a process, control, record, role, system or policy an "
            "organisation puts in place) that satisfies these obligations. Name it as a concrete measure. "
            "satisfies MUST be a list of {\"source_key\", \"refs\"} drawn ONLY from the obligations. "
            "Do not invent sources or refs.\n"
            'JSON: {"name": "", "kind": "System|Document|Role|Configuration|Process|Workflow|Asset|Body", '
            '"purpose": "", "description": "", "capability": "", '
            '"evidence_artifacts": ["..."], "satisfies": [{"source_key": "", "refs": [""]}]}\n\n'
            + "\n".join(
                f"- {o['id']} [{o['source_key']} #{o['clause_ref']}] {o.get('determination') or o['title']}"
                for o in cluster
            )
        )
        try:
            result = complete(
                llm, "l3.block_generate",
                prompt=prompt,
                system="You design controls. Closed-world references only. JSON only.",
                required_keys=["name", "description", "satisfies"],
                max_tokens=800,
            )
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("L3 generation failed")
            blocked += 1
            continue
        name = str(parsed["name"])[:160]
        satisfies = []
        for sel in parsed.get("satisfies") or []:
            if not isinstance(sel, dict):
                continue
            key = sel.get("source_key")
            refs = [str(r) for r in (sel.get("refs") or [])]
            allowed_refs = {o["clause_ref"] for o in cluster if o["source_key"] == key}
            refs = [r for r in refs if r in allowed_refs]
            if key and refs:
                satisfies.append({"source_key": key, "refs": refs})
        if not satisfies:
            blocked += 1
            continue
        duplicate = _duplicate_of(name, existing)
        if duplicate:
            # Same measure under another name: these obligations need it too.
            _link_cluster(engine, cluster, satisfies, duplicate, name, result.model)
            reused += 1
            continue
        from app.clhear.l3.kinds import KINDS, infer_kind

        kind = str(parsed.get("kind", "")).strip().capitalize()
        if kind not in KINDS:
            kind = infer_kind(f"{name} {parsed.get('description', '')} {parsed.get('capability', '')}")
        bid = f"BLK-AI-{_slug(name)}"
        values = dict(
            name=name,
            kind=kind,
            purpose=str(parsed.get("purpose") or parsed.get("description", ""))[:400],
            description=str(parsed.get("description", ""))[:800],
            capability=str(parsed.get("capability", ""))[:200],
            evidence_artifacts=[
                {"artifact": str(a), "origin": "ai-designed"}
                if not isinstance(a, dict)
                else {**a, "origin": "ai-designed"}
                for a in (parsed.get("evidence_artifacts") or [])
            ],
            satisfies=satisfies,
            implements_controls=[],
            status="ai_generated",
        )
        with engine.begin() as conn:
            exists = conn.execute(sa.select(blocks_t.c.id).where(blocks_t.c.id == bid)).first()
            if not exists:
                conn.execute(blocks_t.insert().values(id=bid, **values))
        # HLD v2 §4.3: the closed-world satisfies anchors become explicit requires edges.
        _link_cluster(engine, cluster, satisfies, bid, name, result.model)
        from app.clhear.governance import mark_generated

        mark_generated(
            engine, layer="L3", subject_ref=bid, generated_by=result.model,
            routing_reason="L3 synthesis with closed-world satisfies",
            detail={"satisfies": satisfies, "cluster_size": len(cluster)},
        )
        existing.append({"id": bid, "name": name})
        written += 1
        ids.append(bid)
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L3", fleet="l3.generate",
            reasoning=f"Mason: {written} building blocks generated, {reused} reused; {blocked} blocked by grounding",
            detail={"written": written, "reused": reused, "blocked": blocked, "ids": ids, "remaining": remaining},
        )
    except Exception:
        log.exception("L3 ai_ops failed")
    return {"written": written, "reused": reused, "blocked": blocked, "ids": ids, "remaining_obligations": remaining}


def _link_cluster(engine: Engine, cluster: list[dict], satisfies: list[dict], block_id: str, name: str, model: str) -> None:
    from app.clhear.l3.decompose import link, why_for

    with engine.begin() as conn:
        for o in cluster:
            if any(sel["source_key"] == o["source_key"] and o["clause_ref"] in sel["refs"] for sel in satisfies):
                why = why_for(o["stable_id"] or o["id"], method="llm", confidence=0.7,
                              summary=f"l3.block_generate proposed block '{name}' for this obligation (closed-world refs)",
                              evidence_refs=[o["id"]], model_manifest={"model": model, "task": "l3.block_generate"})
                link(conn, obligation=o, block_id=block_id, method="llm", why=why)
