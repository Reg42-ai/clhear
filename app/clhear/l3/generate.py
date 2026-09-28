# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 block generation: a model groups duties under one measure, in the text's words.

The model sees a batch of duties that no measure satisfies yet and proposes
one measure for them. What it returns is kept only when it holds against the
texts: ``satisfies`` must cite duties it was shown, every content word of the
name must occur in the clauses of those duties, and a kind is kept only with
a verbatim quote that shows it. A name that fails is dropped and recorded as
an evidence gap; the deterministic decomposer then names the measure from the
duty itself, or reports that the text names none.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear import evidence
from app.clhear.derived_models import blocks as blocks_t
from app.clhear.derived_models import obligations
from app.clhear.platform import record
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
    """Unlinked obligations in document order, per source, in batches the model reads together."""
    by_source: dict[str, list[dict]] = defaultdict(list)
    for r in _unlinked(engine):
        by_source[r["source_key"]].append(r)
    clusters = []
    for key in sorted(by_source):
        group = by_source[key]
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


def _cluster_clauses(engine: Engine, cluster: list[dict]) -> dict[str, list[dict]]:
    from app.clhear.l2.registry import obligation_clauses

    with engine.connect() as conn:
        return {o["id"]: obligation_clauses(conn, o) for o in cluster}


def _kind(parsed: dict, name: str, clauses: list[dict], cited: list[dict], read: dict) -> tuple[str, list[dict]]:
    """A kind the text shows: the model's, with a verbatim quote; else read from
    the name's own nouns; else the kind the cited duties' own words give, when
    they agree."""
    from app.clhear.l3.decompose import propose_block
    from app.clhear.l3.kinds import KINDS, kind_from_words

    kind = str(parsed.get("kind", "")).strip().capitalize()
    quote = str(parsed.get("kind_quote") or "").strip()
    if kind in KINDS and kind != "Unspecified" and quote:
        found = evidence.quote_first(clauses, quote)
        if found is not None:
            return kind, [found]
    kind, word = kind_from_words("", name)
    found = evidence.quote_first(clauses, word) if word else None
    if found is not None:
        return kind, [found]
    proposals = [propose_block(o, read[o["id"]]) for o in cited]
    kinds = {p["kind"] for p in proposals if p}
    if len(kinds) == 1 and all(proposals) and "Unspecified" not in kinds:
        return kinds.pop(), [q for p in proposals for q in p["evidence"]["kind"]]
    return "Unspecified", []


def _accept(engine: Engine, proposal: dict, cluster: list[dict], taken: set[str], existing: list[dict],
            scope: str, model: str) -> str | None:
    """Keep one proposed measure if it holds against the texts. Returns the new
    block id, "reused:<id>", "rejected" (name not in the text) or None (cites nothing usable)."""
    name = " ".join(str(proposal.get("name") or "").split())[:160]
    satisfies = []
    for sel in proposal.get("satisfies") or []:
        if not isinstance(sel, dict):
            continue
        key = sel.get("source_key")
        refs = [str(r) for r in (sel.get("refs") or [])]
        allowed = {o["clause_ref"] for o in cluster if o["source_key"] == key and o["id"] not in taken}
        refs = [r for r in refs if r in allowed]
        if key and refs:
            satisfies.append({"source_key": key, "refs": refs})
    if not satisfies:
        return None
    cited = [o for o in cluster if any(sel["source_key"] == o["source_key"] and o["clause_ref"] in sel["refs"]
                                       for sel in satisfies)]
    read = _cluster_clauses(engine, cited)
    clauses = [c for o in cited for c in read[o["id"]]]
    missing = evidence.missing_words(name, [c["text"] for c in clauses])
    if not evidence.content_words(name) or missing:
        with engine.begin() as conn:
            for o in cited:
                evidence.record_gap(conn, scope=scope, layer="L3", kind="measure_name_rejected", subject=o["id"],
                                    source_key=o["source_key"], clause_ref=o["clause_ref"],
                                    missing="a measure name in the text's own words",
                                    detail={"proposed": name, "words_not_in_text": missing})
        return "rejected"
    taken.update(o["id"] for o in cited)
    duplicate = _duplicate_of(name, existing)
    if duplicate:
        # Same measure under another name: these obligations need it too.
        _link_cluster(engine, cited, satisfies, duplicate, name, model)
        return f"reused:{duplicate}"
    kind, kind_quotes = _kind(proposal, name, clauses, cited, read)
    bid = f"BLK-AI-{_slug(name)}"
    duties = [(o.get("evidence") or {}).get("duty") for o in cited if isinstance(o.get("evidence"), dict)]
    values = dict(
        name=name, kind=kind, purpose=next((d["quote"] for d in duties if d), "")[:400], description="",
        capability="", evidence_artifacts=[], satisfies=satisfies, implements_controls=[], status="ai_generated",
        evidence={"name_words_in": [evidence.whole(c) for c in clauses], "kind": kind_quotes},
    )
    with engine.begin() as conn:
        if conn.execute(sa.select(blocks_t.c.id).where(blocks_t.c.id == bid)).first() is None:
            from app.clhear.l3.decompose import why_for

            why = why_for(bid, method="llm", confidence=0.7,
                          summary=f"l3.block_generate named '{name}'; every word is in the cited clauses",
                          evidence_refs=[o["id"] for o in cited],
                          model_manifest={"model": model, "task": "l3.block_generate"})
            record.write(conn, blocks_t, {"id": bid, **values}, why=why, valid_from=datetime.now(timezone.utc).date())
    # HLD v2 §4.3: the closed-world satisfies anchors become explicit requires edges.
    _link_cluster(engine, cited, satisfies, bid, name, model)
    from app.clhear.governance import mark_generated

    mark_generated(engine, layer="L3", subject_ref=bid, generated_by=model,
                   routing_reason="L3 synthesis with closed-world satisfies and words from the text",
                   detail={"satisfies": satisfies, "cluster_size": len(cluster)})
    existing.append({"id": bid, "name": name})
    return bid


def generate_blocks(engine: Engine, llm, limit: int = MAX_BLOCKS) -> dict:
    from app.clhear.l1.scopes import active_name

    scope = active_name() or ""
    existing = _existing_blocks(engine)
    written = blocked = reused = rejected = 0
    ids: list[str] = []
    clusters = _clusters(engine)
    remaining = sum(len(c) for c in clusters[limit:])
    for cluster in clusters[:limit]:
        prompt = (
            "Group these obligations into the measures an organisation puts in place to meet them (a process, "
            "record, role, system, policy ...). One measure may meet several obligations only when it is truly the "
            "same thing. Name each measure using ONLY words that appear in its obligations. satisfies MUST cite "
            "only these obligations as {\"source_key\", \"refs\"}. kind_quote is the verbatim words of an "
            "obligation that show what kind of thing the measure is, or empty.\n"
            'JSON: {"measures": [{"name": "", "kind": "System|Document|Role|Configuration|Process|Workflow|Asset|Body", '
            '"kind_quote": "", "satisfies": [{"source_key": "", "refs": [""]}]}]}\n\n'
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
                required_keys=["measures"],
                max_tokens=1600,
            )
            parsed = parse_json_object(result.text)
        except Exception:
            log.exception("L3 generation failed")
            blocked += 1
            continue
        proposals = parsed.get("measures") if isinstance(parsed.get("measures"), list) else []
        taken: set[str] = set()
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            outcome = _accept(engine, proposal, cluster, taken, existing, scope, result.model)
            if outcome is None:
                blocked += 1
            elif outcome == "rejected":
                rejected += 1
            elif outcome.startswith("reused:"):
                reused += 1
            else:
                written += 1
                ids.append(outcome)
    try:
        from app.clhear import ai_ops

        ai_ops.record(
            engine, kind="fleet_generation", layer="L3", fleet="l3.generate",
            reasoning=f"{written} measures named, {reused} reused; {blocked} blocked by grounding, "
                      f"{rejected} names not in the text",
            detail={"written": written, "reused": reused, "blocked": blocked, "rejected": rejected, "ids": ids,
                    "remaining": remaining},
        )
    except Exception:
        log.exception("L3 ai_ops failed")
    return {"written": written, "reused": reused, "blocked": blocked, "rejected_names": rejected, "ids": ids,
            "remaining_obligations": remaining}


def _link_cluster(engine: Engine, cluster: list[dict], satisfies: list[dict], block_id: str, name: str, model: str) -> None:
    from app.clhear.l3.decompose import link, why_for

    with engine.begin() as conn:
        for o in cluster:
            if any(sel["source_key"] == o["source_key"] and o["clause_ref"] in sel["refs"] for sel in satisfies):
                why = why_for(o["stable_id"] or o["id"], method="llm", confidence=0.7,
                              summary=f"l3.block_generate proposed block '{name}' for this obligation (closed-world refs)",
                              evidence_refs=[o["id"]], model_manifest={"model": model, "task": "l3.block_generate"})
                link(conn, obligation=o, block_id=block_id, method="llm", why=why)
