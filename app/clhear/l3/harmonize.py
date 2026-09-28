# Copyright (C) 2026 Reg42 AI
# This file is part of CLHEAR. See LICENSE (AGPL-3.0-only).
"""L3 harmonizers — one canonical block reused across many obligations
(HLD v2 §4.3: "an AML Policy Document is one block cited by hundreds").

Blocks of the same kind whose names are near-identical are merged: the
earliest keeps its id, the later one gets ``canonical_id`` and its live
``requires`` edges are re-issued against the canonical block (I2: the old
edges are invalidated, never deleted). The reuse ratio — live requires edges
per canonical block — is published; a block count that outgrows the
obligations it serves is a defect ("block explosion").
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from itertools import combinations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import blocks, characteristics, obligations, requires
from app.clhear.l3.decompose import name_similarity, why_for
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l3.harmonize")

MERGE_THRESHOLD = 0.8
AGENT = "l3.harmonize"


def _rank(block: dict) -> tuple:
    # Curated first, then earliest id — the human-authored block is the canonical one.
    return (0 if block["status"] == "curated" else 1, block["id"])


def blocks_in_scope(conn) -> set[str] | None:
    """Block ids a scoped build may rewrite, or None when no scope is active.

    A block is in scope only when every live requires edge and every satisfies
    selector names a source in the scope. A block shared with another source
    is left untouched.
    """
    from app.clhear.l1.scopes import keys

    chosen = keys()
    if chosen is None:
        return None
    linked: dict[str, set[str]] = {}
    for bid, source_key in conn.execute(
            sa.select(requires.c.block_id, obligations.c.source_key)
            .join(obligations, obligations.c.id == requires.c.obligation_id)
            .where(requires.c.valid_to.is_(None))):
        linked.setdefault(bid, set()).add(source_key)
    allowed = {bid for bid, srcs in linked.items() if srcs and srcs <= chosen}
    for row in conn.execute(sa.select(blocks.c.id, blocks.c.satisfies).where(blocks.c.canonical_id.is_(None))
                            .where(blocks.c.valid_to.is_(None))):
        if row.id in linked:
            continue
        selectors = row.satisfies or []
        srcs = {s.get("source_key") for s in selectors if isinstance(s, dict) and s.get("source_key")}
        if srcs and srcs <= chosen:
            allowed.add(row.id)
    return allowed


def harmonize(engine: Engine, threshold: float = MERGE_THRESHOLD) -> dict:
    merged = edges_moved = 0
    with engine.begin() as conn:
        # Closed blocks (a 0.1 measure without quotes, say) are never merged into or kept.
        rows = [dict(r) for r in conn.execute(sa.select(blocks).where(blocks.c.canonical_id.is_(None))
                                              .where(blocks.c.valid_to.is_(None))).mappings()]
        allowed = blocks_in_scope(conn)
        if allowed is not None:
            rows = [r for r in rows if r["id"] in allowed]
        by_kind: dict[str, list[dict]] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        absorbed: set[str] = set()
        for group in by_kind.values():
            for a, b in combinations(sorted(group, key=_rank), 2):
                if a["id"] in absorbed or b["id"] in absorbed:
                    continue
                score = name_similarity(a["name"], b["name"])
                if score < threshold:
                    continue
                keep, drop = a, b  # sorted by rank: a is canonical
                why = why_for(drop["id"], method=AGENT, confidence=round(score, 3),
                              summary=f"harmonised into {keep['id']} ('{keep['name']}', similarity {score:.2f})",
                              evidence_refs=[keep["id"]])
                trail = why.write(conn)
                conn.execute(blocks.update().where(blocks.c.id == drop["id"]).values(canonical_id=keep["id"], why_trail_id=trail))
                for edge in conn.execute(sa.select(requires).where(requires.c.block_id == drop["id"]).where(requires.c.valid_to.is_(None))).mappings().all():
                    record.invalidate(conn, requires, requires.c.id == edge["id"], why=trail, reason=f"block harmonised into {keep['id']}")
                    exists = conn.execute(
                        sa.select(requires.c.id).where(requires.c.obligation_id == edge["obligation_id"])
                        .where(requires.c.block_id == keep["id"]).where(requires.c.valid_to.is_(None))
                    ).first()
                    if exists is None:
                        record.write(
                            conn, requires,
                            {"id": next_id(conn, "REQ"), "obligation_id": edge["obligation_id"], "block_id": keep["id"],
                             "rationale": edge["rationale"], "rationale_start": edge["rationale_start"],
                             "rationale_end": edge["rationale_end"], "method": "harmonized",
                             "obligation_text_hash": edge["obligation_text_hash"], "evidence": edge["evidence"]},
                            why=trail, valid_from=datetime.now(timezone.utc).date(),
                        )
                    edges_moved += 1
                for c in conn.execute(sa.select(characteristics).where(characteristics.c.block_id == drop["id"]).where(characteristics.c.valid_to.is_(None))).mappings().all():
                    record.invalidate(conn, characteristics, characteristics.c.id == c["id"], why=trail, reason=f"block harmonised into {keep['id']}")
                absorbed.add(drop["id"])
                merged += 1
    out = {"merged": merged, "edges_moved": edges_moved}
    log.info("L3 harmonize: %s", out)
    return out


def reuse_ratio(engine: Engine) -> dict:
    """Live requires edges per canonical block, plus the explosion check:
    more canonical blocks than linked obligations means decomposition is
    inventing a block per clause instead of harmonising."""
    with engine.connect() as conn:
        canonical = conn.execute(sa.select(sa.func.count()).select_from(blocks).where(blocks.c.canonical_id.is_(None))).scalar_one()
        edges = conn.execute(
            sa.select(requires.c.obligation_id, requires.c.block_id)
            .join(obligations, obligations.c.id == requires.c.obligation_id)
            .where(requires.c.valid_to.is_(None))
            .where(obligations.c.status.in_(("derived", "validated")))
        ).all()
        by_kind = {r[0]: r[1] for r in conn.execute(
            sa.select(blocks.c.kind, sa.func.count()).where(blocks.c.canonical_id.is_(None)).group_by(blocks.c.kind))}
        derived_canonical = conn.execute(
            sa.select(sa.func.count()).select_from(blocks).where(blocks.c.canonical_id.is_(None)).where(blocks.c.status != "curated")
        ).scalar_one()
    linked_obligations = {e.obligation_id for e in edges}
    used_blocks = {e.block_id for e in edges}
    ratio = len(edges) / canonical if canonical else 0.0
    return {
        "canonical_blocks": canonical,
        "blocks_in_use": len(used_blocks),
        "live_edges": len(edges),
        "obligations_linked": len(linked_obligations),
        "reuse_ratio": round(ratio, 3),
        "by_kind": by_kind,
        "derived_canonical_blocks": derived_canonical,
        # Curated blocks may anchor instruments not yet ingested; only machine-
        # derived blocks can explode relative to the obligations they serve.
        "explosion": derived_canonical > len(linked_obligations),
    }
