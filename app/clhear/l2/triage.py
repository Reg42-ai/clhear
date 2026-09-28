# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 duty triage — LLM only for weak-modality clauses, evidence-span contract.

The deterministic extractor owns high-precision must/shall. This fleet looks at
clauses the rules left behind (should / may / ought) and asks for a verdict
PLUS a quoted span. No span that is a literal substring of the clause → no
verdict. The model sees only the clause text.
"""
from __future__ import annotations

import logging
import re

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import obligations
from app.clhear.l1.models import clauses, family_members, source_versions, sources
from app.clhear.l2 import registry
from app.clhear.l2.extract import (ADDRESSEE, MAX_STATEMENT, _title_from, clause_contexts, container_clause_ids,
                                   detect_duty, duty_text, not_a_duty, obligation_id, why_id)
from app.clhear.platform import record
from app.clhear.platform.gateway import parse_json_object
from app.clhear.platform.ids import next_id
from app.clhear.platform.router import complete

log = logging.getLogger("clhear.l2.triage")

WEAK_MODAL = re.compile(r"\b(?:should|ought to|may|is expected to|are expected to)\b", re.I)
MAX_PER_RUN = 200


def _weak_candidates(engine: Engine, limit: int = MAX_PER_RUN) -> list[dict]:
    existing: set[str] = set()
    with engine.connect() as conn:
        existing = {r.id for r in conn.execute(sa.select(obligations.c.id))}
        binding = {
            row.source_id
            for row in conn.execute(sa.select(family_members).where(family_members.c.tier == "binding"))
        }
        versions = {
            v.source_id: v
            for v in conn.execute(
                sa.select(source_versions).where(source_versions.c.status == "in_force")
            )
        }
        srcs = {s.id: s for s in conn.execute(sa.select(sources).where(sources.c.license == "open"))}
        out = []
        from app.clhear.l1.scopes import in_scope

        for sid, src in srcs.items():
            if sid not in binding or sid not in versions or not in_scope(src.key):
                continue
            containers = container_clause_ids(conn, versions[sid].id)
            contexts = clause_contexts(conn, versions[sid].id)
            for row in conn.execute(
                sa.select(clauses).where(clauses.c.source_version_id == versions[sid].id)
                .where(clauses.c.public_ok.is_(True)).order_by(clauses.c.ordering)
            ):
                if row.id in containers:
                    continue
                ref = row.ref or f"clause-{row.ordering}"
                context = contexts.get(row.id) or {}
                text = duty_text(row.text or "", context)
                heading = context.get("heading", "")
                if obligation_id(src.key, ref) in existing:
                    continue
                if detect_duty(text, ref, heading) is not None:
                    continue
                # The rules' "not a duty" (procedure, construction, penalties) is final;
                # a model is never asked to overrule it.
                if not_a_duty(text, ref, heading):
                    continue
                if not WEAK_MODAL.search(text):
                    continue
                if len(text.strip()) < 40:
                    continue
                out.append({
                    "source_key": src.key,
                    "ref": ref,
                    "text": text,
                    "text_hash": row.text_hash,
                    "clause_id": row.id,
                    "jurisdiction": src.jurisdiction,
                    "regulator": src.issuer,
                    "themes": src.topics if isinstance(src.topics, list) else [],
                    "version_label": versions[sid].version_label,
                    "as_of_date": versions[sid].as_of_date,
                })
                if len(out) >= limit:
                    return out
    return out


def span_is_grounded(span: str, clause_text: str) -> bool:
    needle = " ".join((span or "").split())
    hay = " ".join((clause_text or "").split())
    return bool(needle) and len(needle) >= 12 and needle.lower() in hay.lower()


def triage_duties(engine: Engine, llm, limit: int = MAX_PER_RUN) -> dict:
    inserted = rejected = 0
    for cand in _weak_candidates(engine, limit=limit):
        prompt = (
            "Does this regulatory clause impose a duty (someone must/should do something)? "
            "You MUST quote an evidence_span that is a verbatim substring of the clause. "
            'JSON: {"is_duty": true|false, "modality": "should"|"may"|"ought"|null, '
            '"evidence_span": "verbatim quote", "addressee": ""}.\n\nCLAUSE:\n'
            + cand["text"][:2000]
        )
        try:
            result = complete(
                llm, "l2.duty_triage",
                prompt=prompt,
                system="You classify duties. Quote only. Never paraphrase the evidence_span. JSON only.",
                required_keys=["is_duty", "evidence_span"],
                max_tokens=400,
            )
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("duty triage failed for %s#%s", cand["source_key"], cand["ref"])
            rejected += 1
            continue
        span = str(parsed.get("evidence_span") or "")
        if not span_is_grounded(span, cand["text"]):
            rejected += 1
            continue
        verdict = parsed.get("is_duty")
        if not (verdict is True or str(verdict).strip().lower() == "true"):
            rejected += 1
            continue
        oid = obligation_id(cand["source_key"], cand["ref"])
        statement = re.sub(r"\s+", " ", cand["text"]).strip()
        if len(statement) > MAX_STATEMENT:
            statement = statement[: MAX_STATEMENT - 1].rsplit(" ", 1)[0] + "…"
        addressee_match = ADDRESSEE.search(cand["text"])
        structured = registry.structured_fields(cand["text"], str(parsed.get("modality") or "should"))
        with engine.begin() as conn:
            why = registry.why_for(
                oid, clause_id=cand["clause_id"], text_hash=cand["text_hash"], method="duty-triage-v1",
                confidence=0.7,
                summary=f"weak-modality duty accepted by {result.model}; evidence span quoted verbatim",
                model_manifest={"model": result.model, "task": "l2.duty_triage"},
            )
            record.write(
                conn,
                obligations,
                dict(
                    id=oid,
                    stable_id=next_id(conn, "OBL"),
                    source_key=cand["source_key"],
                    clause_ref=cand["ref"],
                    title=_title_from(cand["text"], cand["ref"]),
                    statement=statement,
                    addressee=addressee_match.group(1).strip() if addressee_match else "",
                    modality=str(parsed.get("modality") or "should"),
                    jurisdiction=cand["jurisdiction"] or "",
                    jurisdictions=[cand["jurisdiction"]] if cand["jurisdiction"] else [],
                    regulator=cand.get("regulator") or "",
                    themes=cand["themes"],
                    confidence=0.7,
                    status="derived",
                    method="duty-triage-v1",
                    text_hash=cand["text_hash"],
                    source_version_label=cand["version_label"] or "",
                    effective_from=cand.get("as_of_date"),
                    **structured,
                ),
                why=why,
                valid_from=cand.get("as_of_date"),
            )
            trail = why_id(conn, oid)
            registry.upsert_assert(
                conn, obligation_id=oid, clause_id=cand["clause_id"], source_key=cand["source_key"],
                clause_ref=cand["ref"], text=cand["text"], text_hash=cand["text_hash"],
                strength="implied", why=trail,
            )
            registry.record_change(
                conn, obligation_id=oid, kind="added", cause_clause_ids=[cand["clause_id"]],
                source_key=cand["source_key"], new_text_hash=cand["text_hash"],
                effective_date=cand.get("as_of_date"), effective_date_basis="publisher" if cand.get("as_of_date") else "none",
                detail={"evidence_span": span[:200]}, why=trail,
            )
        from app.clhear.governance import mark_generated

        mark_generated(
            engine, layer="L2", subject_ref=oid, generated_by=result.model,
            routing_reason="duty-triage evidence-span contract",
            detail={"span": span[:200]},
        )
        inserted += 1
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L2", fleet="l2.triage",
            reasoning=f"Miner/Weaver triage: {inserted} weak-modality duties accepted, {rejected} discarded (no span or not a duty)",
            detail={"inserted": inserted, "rejected": rejected},
        )
    except Exception:
        log.exception("triage ai_ops failed")
    return {"inserted": inserted, "rejected": rejected, "examined": inserted + rejected}
