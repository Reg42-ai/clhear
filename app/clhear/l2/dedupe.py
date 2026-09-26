# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L2 consolidators (HLD v2 §4.2): dedupe into canonical obligations and
detect cross-jurisdiction equivalences.

Dedupe is *within* a jurisdiction: two live obligations whose normalised
determination text is identical, or whose content-word sets overlap ≥ 0.92,
describe one duty asserted by several clauses. The earliest stable id stays
canonical; the others get ``canonical_id`` and their clause edges are
reported under the canonical on the obligation page. Nothing is deleted.

Equivalence is *across* jurisdictions: obligations that share a curated /
consolidated concept (``concept_members``) are equivalent by construction;
lexically similar determinations (≥ 0.6) across jurisdictions are proposed
with basis ``lexical`` and their similarity as confidence.
"""
from __future__ import annotations

import logging
import re
from itertools import combinations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import concept_members, equivalences, obligations
from app.clhear.l2 import registry
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l2.dedupe")

DUPLICATE_THRESHOLD = 0.92
EQUIVALENCE_THRESHOLD = 0.60
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset({
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "by", "with", "that", "which", "its",
    "their", "such", "any", "all", "as", "at", "be", "is", "are", "must", "shall", "should", "may", "not",
    "firm", "person", "entity", "it", "this", "these", "those", "where", "when", "if",
})


def normalise(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def similarity(a: str, b: str) -> float:
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _live(conn) -> list[dict]:
    from app.clhear.l1.scopes import in_scope

    return [
        dict(r)
        for r in conn.execute(
            sa.select(
                obligations.c.id, obligations.c.stable_id, obligations.c.jurisdiction, obligations.c.determination,
                obligations.c.statement, obligations.c.canonical_id, obligations.c.source_key, obligations.c.text_hash,
            ).where(obligations.c.status.in_(("derived", "validated")))
        ).mappings()
        if in_scope(r["source_key"])
    ]


def duplicate_pairs(rows: list[dict], threshold: float = DUPLICATE_THRESHOLD) -> list[tuple[dict, dict, float]]:
    """Within-jurisdiction near-duplicates among canonical rows."""
    out: list[tuple[dict, dict, float]] = []
    by_jur: dict[str, list[dict]] = {}
    for r in rows:
        if r["canonical_id"]:
            continue
        by_jur.setdefault(r["jurisdiction"] or "", []).append(r)
    for group in by_jur.values():
        by_norm: dict[str, dict] = {}
        for r in group:
            key = normalise(r["determination"] or r["statement"])
            if key in by_norm:
                out.append((by_norm[key], r, 1.0))
            else:
                by_norm[key] = r
        seen = {(a["id"], b["id"]) for a, b, _ in out}
        for a, b in combinations(group, 2):
            if (a["id"], b["id"]) in seen or (b["id"], a["id"]) in seen:
                continue
            s = similarity(a["determination"] or a["statement"], b["determination"] or b["statement"])
            if s >= threshold:
                out.append((a, b, s))
    return out


def dedupe(engine: Engine) -> dict:
    """Mark near-duplicates with ``canonical_id`` = the earliest duplicate's
    public stable id (the canonical row itself keeps ``canonical_id`` NULL)."""
    merged = 0
    with engine.begin() as conn:
        rows = _live(conn)
        pairs = duplicate_pairs(rows)
        by_id = {r["id"]: r for r in rows}
        canonical_of: dict[str, str] = {}
        for a, b, score in pairs:
            first, second = sorted((a, b), key=lambda r: r["stable_id"] or r["id"])
            root = first["id"]
            while root in canonical_of:
                root = canonical_of[root]
            if second["id"] in canonical_of or second["id"] == root:
                continue
            canonical_of[second["id"]] = root
            root_public = by_id[root]["stable_id"] or root
            why = registry.why_for(
                second["id"], clause_id=None, text_hash=second["text_hash"], method="l2.dedupe",
                confidence=round(score, 3),
                summary=f"duplicate of {root_public} within {second['jurisdiction']} (similarity {score:.2f}); clause edges kept",
            )
            trail = why.write(conn)
            conn.execute(
                obligations.update().where(obligations.c.id == second["id"])
                .values(canonical_id=root_public, why_trail_id=trail)
            )
            merged += 1
        live = [r for r in rows]
    canonical = sum(1 for r in live if not r["canonical_id"]) - merged
    return {"live": len(live), "merged": merged, "canonical": max(canonical, 0), "pairs": len(pairs)}


def duplicate_rate(engine: Engine) -> dict:
    """Share of live canonical obligations that still have an unmerged near-duplicate."""
    with engine.connect() as conn:
        rows = _live(conn)
    canonical = [r for r in rows if not r["canonical_id"]]
    pairs = duplicate_pairs(canonical)
    dup_ids = {b["id"] for _, b, _ in pairs}
    rate = len(dup_ids) / len(canonical) if canonical else 0.0
    return {"canonical": len(canonical), "unmerged_duplicates": len(dup_ids), "rate": round(rate, 4),
            "examples": [(a["stable_id"], b["stable_id"], round(s, 2)) for a, b, s in pairs[:10]]}


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def detect_equivalences(engine: Engine, threshold: float = EQUIVALENCE_THRESHOLD) -> dict:
    written = 0
    with engine.begin() as conn:
        rows = [r for r in _live(conn) if not r["canonical_id"]]
        existing = {
            _pair_key(r.obligation_a, r.obligation_b)
            for r in conn.execute(sa.select(equivalences.c.obligation_a, equivalences.c.obligation_b))
        }
        by_id = {r["id"]: r for r in rows}

        def _write(a: str, b: str, *, basis: str, sim: float | None, concept_id: str | None, method: str, summary: str):
            nonlocal written
            key = _pair_key(a, b)
            if key in existing or a == b:
                return
            existing.add(key)
            why = registry.why_for(
                key[0], clause_id=None, text_hash="", method=method, confidence=sim,
                summary=summary,
            )
            record.write(
                conn, equivalences,
                {"id": next_id(conn, "EQV"), "obligation_a": key[0], "obligation_b": key[1], "basis": basis,
                 "concept_id": concept_id, "similarity": sim, "method": method},
                why=why,
            )
            written += 1

        # 1. Shared concept -> equivalent by construction.
        members: dict[str, list[tuple[str, str]]] = {}
        for m in conn.execute(
            sa.select(concept_members.c.concept_id, concept_members.c.obligation_id, concept_members.c.jurisdiction)
            .where(concept_members.c.valid_to.is_(None))
        ):
            members.setdefault(m.concept_id, []).append((m.obligation_id, m.jurisdiction))
        for concept_id, ms in members.items():
            for (a, ja), (b, jb) in combinations(ms, 2):
                if ja == jb or a not in by_id or b not in by_id:
                    continue
                _write(a, b, basis="concept", sim=1.0, concept_id=concept_id, method="l2.consolidate.concept",
                       summary=f"both members of concept {concept_id} ({ja} ≈ {jb})")
        # 2. Lexical similarity across jurisdictions.
        for a, b in combinations(rows, 2):
            if (a["jurisdiction"] or "") == (b["jurisdiction"] or ""):
                continue
            s = similarity(a["determination"] or a["statement"], b["determination"] or b["statement"])
            if s >= threshold:
                _write(a["id"], b["id"], basis="lexical", sim=round(s, 3), concept_id=None, method="l2.consolidate.lexical",
                       summary=f"determinations overlap {s:.2f} across {a['jurisdiction']} / {b['jurisdiction']}")
    return {"written": written, "total": len(existing)}


def consolidate(engine: Engine) -> dict:
    return {"dedupe": dedupe(engine), "equivalences": detect_equivalences(engine)}


__all__ = ["consolidate", "dedupe", "detect_equivalences", "duplicate_pairs", "duplicate_rate", "similarity"]
