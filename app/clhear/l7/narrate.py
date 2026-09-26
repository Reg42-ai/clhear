# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L7 risk narratives — formula stays deterministic; commentary is number-echoed.

Every figure in the narrative must equal a figure in the score's input vector.
External context comes only from the versioned benchmark-facts file.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.clhear.models import risk_narratives
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l7.narrate")

FACTS_PATH = Path(__file__).resolve().parent.parent / "curated" / "l7_benchmark_facts.json"
_NUM = re.compile(r"(?<![A-Za-z])(\d+(?:\.\d+)?)(?![A-Za-z])")


def load_facts() -> list[dict]:
    return json.loads(FACTS_PATH.read_text())


def figures_in(score: dict) -> set[str]:
    found: set[str] = set()

    def walk(obj):
        if isinstance(obj, bool):
            return
        if isinstance(obj, int):
            found.add(str(obj))
        elif isinstance(obj, float):
            found.add(str(obj))
            found.add(f"{obj:.1f}")
            found.add(f"{obj:.3f}".rstrip("0").rstrip("."))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(score)
    return found


def number_echo_ok(narrative: str, score: dict) -> tuple[bool, list[str]]:
    allowed = figures_in(score)
    extras = []
    for m in _NUM.findall(narrative or ""):
        if m in allowed:
            continue
        # Allow the facts-file year-less percentages already in the vector.
        extras.append(m)
    return not extras, extras


def narrate_risk(engine, llm, item: dict) -> dict:
    result_block = item.get("result") or {}
    inputs = {
        **(item.get("inputs") or {}),
        **(item.get("live_inputs") or {}),
        "score": result_block.get("score"),
        "band": result_block.get("band"),
        "components": result_block.get("components") or {},
    }
    facts = load_facts()
    fact_lines = "\n".join(f"- [{f['id']}] {f['text']}" for f in facts)
    prompt = (
        "Write a 3-5 sentence risk narrative. Every numeric figure MUST appear in the "
        "input vector below (copy the number exactly). External context only from the "
        "facts list (cite FACT: ids). JSON: {\"narrative\": \"\", \"facts_used\": [\"FACT:...\"]}\n\n"
        f"INPUT VECTOR: {json.dumps(inputs, default=str)}\n\nFACTS:\n{fact_lines}\n"
        f"AREA: {item.get('title') or item.get('name') or item.get('area')}"
    )
    try:
        result = complete(
            llm, "l7.narrative",
            prompt=prompt,
            system="Number-echo commentator. Never invent figures. JSON only.",
            required_keys=["narrative"],
            max_tokens=500,
        )
        parsed = parse_json_object(result.text)
    except Exception as exc:
        log.exception("L7 narrative failed")
        return {"written": False, "reason": str(exc)[:200]}
    text = str(parsed.get("narrative") or "")
    ok, extras = number_echo_ok(text, inputs)
    if not ok:
        return {"written": False, "reason": "number_echo_failed", "extras": extras}
    allowed_facts = {f["id"] for f in facts}
    used = [f for f in (parsed.get("facts_used") or []) if f in allowed_facts]
    nid = f"NAR:{item.get('id') or item.get('area')}"
    import sqlalchemy as sa

    with engine.begin() as conn:
        exists = conn.execute(
            sa.select(risk_narratives.c.id).where(risk_narratives.c.id == nid)
        ).first()
        values = dict(
            score_id=str(item.get("id") or ""),
            narrative=text,
            echoed_figures=sorted(figures_in(inputs)),
            facts_used=used,
            generated_by=result.model,
        )
        if exists:
            conn.execute(risk_narratives.update().where(risk_narratives.c.id == nid).values(**values))
        else:
            conn.execute(risk_narratives.insert().values(id=nid, **values))
    from app.clhear.governance import mark_generated

    mark_generated(
        engine, layer="L7", subject_ref=nid, generated_by=result.model,
        routing_reason="number-echo risk narrative", detail={"facts_used": used},
    )
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L7", fleet="l7.narrate",
            reasoning=f"Actuary: narrative for {item.get('id')} passed number-echo",
            detail={"id": nid, "facts_used": used},
        )
    except Exception:
        pass
    return {"written": True, "id": nid, "narrative": text, "facts_used": used}
