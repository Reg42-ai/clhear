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

MAX_BLOCKS = 8
MIN_CLUSTER = 2
DEDUP_SIM = 0.55


def _clusters(engine: Engine) -> list[list[dict]]:
    with engine.connect() as conn:
        from app.clhear.l1.scopes import in_scope

        rows = [
            dict(r)
            for r in conn.execute(
                sa.select(obligations).where(obligations.c.status.in_(("derived", "validated")))
            ).mappings()
            if in_scope(r["source_key"])
        ]
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        themes = r["themes"] if isinstance(r["themes"], list) else []
        key = themes[0] if themes else "general"
        by_theme[key].append(r)
    clusters = []
    for theme, group in by_theme.items():
        if len(group) >= MIN_CLUSTER:
            clusters.append(sorted(group, key=lambda r: r["id"])[:12])
    return clusters[:MAX_BLOCKS]


def _existing_blocks(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(sa.select(blocks_t)).mappings()]


def _is_duplicate(name: str, existing: list[dict]) -> bool:
    tok = _tokens(name)
    for b in existing:
        if _jaccard(tok, _tokens(b["name"])) >= DEDUP_SIM:
            return True
    return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def generate_blocks(engine: Engine, llm, limit: int = MAX_BLOCKS) -> dict:
    existing = _existing_blocks(engine)
    written = blocked = 0
    ids: list[str] = []
    for cluster in _clusters(engine)[:limit]:
        live_ids = {o["id"] for o in cluster}
        prompt = (
            "Design ONE reusable compliance building block that satisfies these obligations. "
            "satisfies MUST be a list of {\"source_key\", \"refs\"} drawn ONLY from the obligations. "
            "Do not invent sources or refs.\n"
            'JSON: {"name": "", "kind": "System|Document|Role|Configuration|Process|Workflow|Asset|Body", '
            '"purpose": "", "description": "", "capability": "", '
            '"evidence_artifacts": ["..."], "satisfies": [{"source_key": "", "refs": [""]}]}\n\n'
            + "\n".join(
                f"- {o['id']} [{o['source_key']} #{o['clause_ref']}] {o['title']}"
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
        if _is_duplicate(name, existing):
            blocked += 1
            continue
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
            if exists:
                blocked += 1
                continue
            conn.execute(blocks_t.insert().values(id=bid, **values))
            # HLD v2 §4.3: the closed-world satisfies anchors become explicit requires edges.
            from app.clhear.l3.decompose import link, why_for

            for o in cluster:
                if any(sel["source_key"] == o["source_key"] and o["clause_ref"] in sel["refs"] for sel in satisfies):
                    why = why_for(o["stable_id"] or o["id"], method="llm", confidence=0.7,
                                  summary=f"l3.block_generate proposed block '{name}' for this obligation (closed-world refs)",
                                  evidence_refs=[o["id"]], model_manifest={"model": result.model, "task": "l3.block_generate"})
                    link(conn, obligation=o, block_id=bid, method="llm", why=why)
        from app.clhear.governance import mark_generated

        mark_generated(
            engine, layer="L3", subject_ref=bid, generated_by=result.model,
            routing_reason="L3 synthesis with closed-world satisfies",
            detail={"satisfies": satisfies, "cluster_size": len(cluster)},
        )
        existing.append({"name": name})
        written += 1
        ids.append(bid)
        live_ids  # closed-world already enforced via cluster refs
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L3", fleet="l3.generate",
            reasoning=f"Mason: {written} building blocks generated; {blocked} blocked by eval/dedupe/grounding",
            detail={"written": written, "blocked": blocked, "ids": ids},
        )
    except Exception:
        log.exception("L3 ai_ops failed")
    return {"written": written, "blocked": blocked, "ids": ids}
