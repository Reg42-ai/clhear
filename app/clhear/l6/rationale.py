# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L6 program rationale — LLM as citing narrator only.

Every claim must reference ids present in that blueprint (coverage rows,
blocks, gaps). Narratives citing anything outside the blueprint are rejected.
"""
from __future__ import annotations

import logging
import re

from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l6.rationale")

_ID_RE = re.compile(r"\b(?:OBL:[A-Za-z0-9_./#-]+|BLK:[A-Za-z0-9_-]+|ACT:[A-Za-z0-9_-]+|CON:[A-Za-z0-9_-]+)\b")


def allowed_ids(blueprint: dict) -> set[str]:
    ids: set[str] = set()
    for c in blueprint.get("coverage") or []:
        if c.get("obligation_id"):
            ids.add(c["obligation_id"])
        ids.update(c.get("covered_by") or [])
    for b in blueprint.get("blocks") or []:
        if b.get("id"):
            ids.add(b["id"])
    ids.update(blueprint.get("activities_evaluated") or [])
    return ids


def citations_ok(narrative: str, blueprint: dict) -> tuple[bool, list[str]]:
    allowed = allowed_ids(blueprint)
    cited = set(_ID_RE.findall(narrative or ""))
    extra = sorted(cited - allowed)
    return (not extra and bool(cited)), extra


def narrate_blueprint(engine, llm, blueprint: dict) -> dict:
    ids = sorted(allowed_ids(blueprint))
    if not ids:
        return {"written": False, "reason": "empty blueprint"}
    summary = blueprint.get("coverage_summary") or {}
    prompt = (
        "Write a 4-8 sentence program rationale. Every factual claim MUST cite an id "
        f"from this list, using the id verbatim: {ids[:40]}. "
        "Do not mention any other obligation, block, or activity.\n"
        'JSON: {"rationale": ""}\n\n'
        f"Coverage: {summary.get('covered')} covered / {summary.get('gaps')} gaps / {summary.get('total')} total.\n"
        f"Blocks: {[b.get('id') for b in blueprint.get('blocks') or []]}\n"
    )
    try:
        result = complete(
            llm, "l6.rationale",
            prompt=prompt,
            system="Citing narrator only. JSON only. Never invent ids.",
            required_keys=["rationale"],
            max_tokens=700,
        )
        parsed = parse_json_object(result.text)
    except Exception as exc:
        log.exception("L6 rationale failed")
        return {"written": False, "reason": str(exc)[:200]}
    text = str(parsed.get("rationale") or "")
    ok, extra = citations_ok(text, blueprint)
    if not ok:
        try:
            from app.clhear import ai_ops

            ai_ops.record(
                engine, kind="eval_gate", layer="L6", fleet="l6.narrate",
                reasoning=f"Composer rationale rejected — cited outside blueprint: {extra[:8]}",
                detail={"extra": extra},
            )
        except Exception:
            pass
        return {"written": False, "reason": "citation_check_failed", "extra": extra}
    blueprint["rationale"] = text
    blueprint["rationale_model"] = result.model
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L6", fleet="l6.narrate",
            reasoning="Composer: citation-checked program rationale accepted",
            detail={"model": result.model, "ids": ids[:20]},
        )
    except Exception:
        pass
    return {"written": True, "rationale": text, "model": result.model}
