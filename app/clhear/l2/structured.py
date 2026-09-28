# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""Structured extractor (task ``l2.extract``, HLD v2 §4.2).

The deterministic parser fills subject / action / condition / object for the
common "<subject> must <action> [<condition>]" shape. Clauses it cannot
split (empty subject or action) go to the model with a strict JSON schema.
Grounding contract: every returned field must be the clause's own words,
quoted (a field may continue from the lead-in into the list item). Answers
that are not quotes are discarded and the row keeps its deterministic fields.
"""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import obligations
from app.clhear.l2 import registry
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l2.structured")

MAX_PER_RUN = 200
GROUNDING_MIN = 0.8
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "by", "with", "that", "which",
                   "its", "their", "such", "any", "all", "as", "at", "be", "is", "are", "it", "this"})

FIELDS = ("subject", "action", "condition", "object")


def grounded(field_text: str, clause_text: str, minimum: float = GROUNDING_MIN) -> bool:
    words = [w for w in _WORD.findall((field_text or "").lower()) if w not in _STOP]
    if not words:
        return True  # empty field is allowed (e.g. no condition)
    hay = set(_WORD.findall((clause_text or "").lower()))
    return sum(1 for w in words if w in hay) / len(words) >= minimum


def _candidates(conn, limit: int) -> list[dict]:
    from app.clhear.l1.scopes import limiting

    query = (sa.select(obligations)
             .where(obligations.c.status.in_(("derived", "validated")))
             .where(sa.or_(obligations.c.subject == "", obligations.c.action == ""))
             .where(obligations.c.statement != ""))
    limit_to = limiting(obligations.c.source_key)
    if limit_to is not None:
        query = query.where(limit_to)
    out = []
    for ob in conn.execute(query.order_by(obligations.c.id).limit(limit)).mappings():
        found = registry.obligation_clauses(conn, dict(ob))
        text = "\n".join(c["text"] for c in reversed(found)) or ob["statement"]
        out.append({**dict(ob), "clause_text": text, "clauses": found})
    return out


def refine_structured(engine: Engine, llm, limit: int = MAX_PER_RUN) -> dict:
    refined = rejected = 0
    with engine.connect() as conn:
        batch = _candidates(conn, limit)
    for ob in batch:
        prompt = (
            "Split this regulatory clause into an atomic obligation. Use ONLY words from the clause. JSON only: "
            '{"subject": "who is bound", "action": "what they must do", "condition": "when/if (or empty)", '
            '"object": "what the action is about (or empty)"}. Every value must be copied verbatim from the '
            "clause.\n\nCLAUSE:\n" + (ob["clause_text"] or "")[:3000]
        )
        try:
            result = complete(
                llm, "l2.extract", prompt=prompt,
                system="You restructure legal text without adding to it. JSON only.",
                required_keys=["subject", "action"], max_tokens=400,
            )
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("structured extraction failed for %s", ob["id"])
            rejected += 1
            continue
        fields = {k: " ".join(str(parsed.get(k) or "").split())[:400] for k in FIELDS}
        if not fields["subject"] or not fields["action"]:
            rejected += 1
            continue
        quoted = {k: registry.field_quotes(fields[k], ob["clauses"]) for k in FIELDS}
        if any(v is None for v in quoted.values()):
            rejected += 1
            continue
        structure = {**fields, "modal": ob["modality"].replace("-", " ") if ob["modality"] else "must"}
        otype = registry.duty_verb(structure, ob["modality"] or "")
        determination = registry.determination_text(structure, fallback=ob["statement"])
        previous = ob.get("evidence") if isinstance(ob.get("evidence"), dict) else {}
        evidence = {**previous, **registry.structure_evidence({**fields, "obligation_type": otype}, ob["clauses"])}
        with engine.begin() as conn:
            why = registry.why_for(
                ob["id"], clause_id=None, text_hash=ob["text_hash"], method="l2.extract.structured",
                confidence=float(ob["confidence"] or 0) or None,
                summary=f"structured split by {result.model}; every field quoted from the clause",
                model_manifest={"model": result.model, "task": "l2.extract"},
            )
            trail = why.write(conn)
            conn.execute(
                obligations.update().where(obligations.c.id == ob["id"]).values(
                    **fields, obligation_type=otype, determination=determination, why_trail_id=trail, evidence=evidence,
                    model_manifest={"model": result.model, "task": "l2.extract"},
                )
            )
        refined += 1
    return {"refined": refined, "rejected": rejected, "examined": len(batch)}


__all__ = ["grounded", "refine_structured"]
